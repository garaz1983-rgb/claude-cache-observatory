/*
 * parse.js — claude-cache-observatory diagnosis engine (Milestone 1).
 *
 * Judgment-rule parity with scripts/check_cache_loss.py v2.1 (the SSOT).
 * Any change to the rules below must be mirrored there (and vice versa).
 *
 * Rules (v2.1, TTL-strict):
 *   - Only records whose message.diagnostics.cache_miss_reason resolves to
 *     the PMNF reason (previous_message_not_found) are loss candidates.
 *   - Dedup by requestId (falling back to message.id). A later record of an
 *     already-seen requestId may back-fill a missing reason (same file only).
 *   - Idle gap to the previous request in the same file decides the class:
 *       in-TTL : gap < 30 min (main session) / gap < 5 min (subagent)
 *       iron   : gap < 5 min (counted within in-TTL)
 *       expired: everything else — legitimate expiry, NOT a loss.
 *   - wasted_tokens = cache_creation_input_tokens of each in-TTL-lost request.
 *
 * Input : array of {name, text} — one entry per *.jsonl file (name may carry
 *         a relative path; a "subagents" path segment marks a subagent file,
 *         as does any record with isSidechain === true).
 * Output: {totals, daily, events} — totals/daily follow 04_DATA_MODEL.md;
 *         events is render-only detail that never leaves the browser.
 *
 * UMD: Node -> module.exports, browser -> window.CacheObservatory.
 * No dependencies, no network, no DOM.
 */
(function (root, factory) {
  if (typeof module === "object" && module !== null && module.exports) {
    module.exports = factory();
  } else {
    root.CacheObservatory = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var SCRIPT_VERSION = "web-1.0";
  var PMNF_REASON = "previous_message_not_found";
  var MAIN_TTL_SECONDS = 1800;
  var SUBAGENT_TTL_SECONDS = 300;
  var IRON_SECONDS = 300;

  function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }

  // Mirrors the CLI's datetime.fromisoformat(ts.replace("Z", "+00:00")):
  // keeps the record's own UTC offset so date buckets match strftime output.
  // Timestamps without an offset are anchored to UTC (all-naive inputs shift
  // uniformly, so gaps and date buckets stay consistent).
  function parseTimestamp(ts) {
    if (typeof ts !== "string" || ts === "") return null;
    var offsetMinutes = 0;
    var hasOffset = false;
    var m = /(?:Z|([+-])(\d{2}):?(\d{2}))$/.exec(ts);
    if (m) {
      hasOffset = true;
      if (m[1]) {
        offsetMinutes = (m[1] === "-" ? -1 : 1) *
          (parseInt(m[2], 10) * 60 + parseInt(m[3], 10));
      }
    }
    var epochMs = Date.parse(hasOffset ? ts : ts + "Z");
    if (isNaN(epochMs)) return null;
    return { epochMs: epochMs, offsetMinutes: offsetMinutes };
  }

  // Calendar date (YYYY-MM-DD) in the record's own UTC offset.
  function dateKeyOf(req) {
    return new Date(req.epochMs + req.offsetMinutes * 60000)
      .toISOString().slice(0, 10);
  }

  // Streaming line walk over an in-memory string: records are processed one
  // line at a time, no intermediate array of all lines is built.
  function forEachLine(text, fn) {
    var start = 0;
    var len = text.length;
    while (start <= len) {
      var nl = text.indexOf("\n", start);
      var end = nl === -1 ? len : nl;
      var line = text.slice(start, end);
      if (line.charCodeAt(line.length - 1) === 13) {
        line = line.slice(0, -1);
      }
      if (line.length > 0) fn(line);
      if (nl === -1) break;
      start = nl + 1;
    }
  }

  function parseFiles(files) {
    if (!Array.isArray(files)) {
      throw new TypeError("parseFiles expects an array of {name, text}");
    }

    // requestId dedup is global across files (CLI parity: one `seen` set).
    var seen = new Set();
    var totals = { requests: 0, in_ttl_losses: 0, iron_losses: 0, wasted_tokens: 0 };
    var dailyMap = new Map();
    var events = [];

    for (var fi = 0; fi < files.length; fi++) {
      var file = isPlainObject(files[fi]) ? files[fi] : {};
      var name = typeof file.name === "string" ? file.name : "";
      var text = typeof file.text === "string" ? file.text : "";
      var isSubByName = /(^|[\\/])subagents[\\/]/.test(name);
      var sawSidechain = false;
      // Per-file map (CLI parity: reason back-fill is same-file only).
      var byrid = new Map();

      forEachLine(text, function (line) {
        // Cheap prefilter, same as the CLI: only usage-bearing lines matter.
        if (line.indexOf('"usage"') === -1) return;
        var o;
        try {
          o = JSON.parse(line);
        } catch (e) {
          return;
        }
        if (!isPlainObject(o)) return;
        if (o.isSidechain === true) sawSidechain = true;
        var msg = isPlainObject(o.message) ? o.message : {};
        var u = msg.usage;
        if (!isPlainObject(u)) return;
        var rid = o.requestId;
        if (!rid) rid = msg.id;
        if (!rid) rid = null;
        var diagBox = isPlainObject(msg.diagnostics) ? msg.diagnostics : {};
        var diag = diagBox.cache_miss_reason;
        var rtype = isPlainObject(diag) ? diag.type : diag;
        if (rtype === undefined) rtype = null;
        if (seen.has(rid)) {
          // v2.1: the reason may sit on a later record of the same requestId
          // — keep the first record, fill in a missing reason only.
          var prev = byrid.get(rid);
          if (prev && prev.rtype === null && rtype) {
            prev.rtype = rtype;
          }
          return;
        }
        seen.add(rid);
        var ts = parseTimestamp(typeof o.timestamp === "string" ? o.timestamp : "");
        if (!ts) return;
        var cc = u.cache_creation_input_tokens || 0;
        byrid.set(rid, {
          rid: rid,
          epochMs: ts.epochMs,
          offsetMinutes: ts.offsetMinutes,
          cc: cc,
          rtype: rtype,
          timestamp: o.timestamp
        });
      });

      var isSub = isSubByName || sawSidechain;
      var reqs = Array.from(byrid.values()).sort(function (a, b) {
        return (a.epochMs - b.epochMs) || (a.cc - b.cc);
      });

      for (var i = 0; i < reqs.length; i++) {
        var r = reqs[i];
        var date = dateKeyOf(r);
        var day = dailyMap.get(date);
        if (!day) {
          day = { date: date, requests: 0, losses: 0, wasted_tokens: 0 };
          dailyMap.set(date, day);
        }
        totals.requests += 1;
        day.requests += 1;
        if (r.rtype !== PMNF_REASON) continue;
        var gap = i > 0 ? (r.epochMs - reqs[i - 1].epochMs) / 1000 : null;
        if (gap === null) {
          // PMNF with no prior request in the file: unclassifiable, not a loss.
          events.push({
            file: name,
            requestId: r.rid,
            timestamp: r.timestamp,
            date: date,
            is_subagent: isSub,
            gap_seconds: null,
            cache_creation_tokens: r.cc,
            classification: "no_prior_request"
          });
          continue;
        }
        var ttlSeconds = isSub ? SUBAGENT_TTL_SECONDS : MAIN_TTL_SECONDS;
        var inTtl = gap < ttlSeconds;
        var classification = "expired";
        if (inTtl) {
          classification = "in_ttl";
          totals.in_ttl_losses += 1;
          totals.wasted_tokens += r.cc;
          day.losses += 1;
          day.wasted_tokens += r.cc;
          if (gap < IRON_SECONDS) {
            classification = "iron";
            totals.iron_losses += 1;
          }
        }
        events.push({
          file: name,
          requestId: r.rid,
          timestamp: r.timestamp,
          date: date,
          is_subagent: isSub,
          gap_seconds: gap,
          cache_creation_tokens: r.cc,
          classification: classification
        });
      }
    }

    var daily = Array.from(dailyMap.values()).sort(function (a, b) {
      return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0);
    });

    return { totals: totals, daily: daily, events: events };
  }

  return {
    SCRIPT_VERSION: SCRIPT_VERSION,
    parseFiles: parseFiles
  };
});
