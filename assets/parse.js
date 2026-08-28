/*
 * parse.js — claude-cache-observatory diagnosis engine (Milestone 1).
 *
 * Judgment-rule parity with scripts/check_cache_loss.py v3 (the SSOT).
 * Any change to the rules below must be mirrored there (and vice versa).
 *
 * Rules (v3, billing-shape judgment — M18):
 *   - Judgment reads the BILL, not the annotation. cache_read (cr) and
 *     cache_creation (cc) cannot be missing or misdescribed — they are what
 *     was charged. The reason field describes the state of the diagnostic
 *     (Anthropic support, 2026-08-28: PMNF means "could not compare", not
 *     "the cache was lost"), so no reason NAME is ever required to count a
 *     loss. Names have exactly one power: the four EXCUSED_REASONS declare
 *     the prompt prefix really changed, which makes re-creating it correct.
 *   - Dedup by requestId (falling back to message.id). A later record of an
 *     already-seen requestId may back-fill a missing reason (same file only).
 *   - Idle gap to the previous request in the same file, then the shape:
 *       confirmed: gap < TTL, cr == 0, cc > 0, reason not excused — the whole
 *                  cache gone minutes after use (Anthropic's own definition
 *                  of a real miss).
 *       probable : gap < TTL, cr > 0, cc >= 100,000, reason not excused —
 *                  prefix read back, conversation body re-created at a scale
 *                  outside the normal distribution (fleet p99 measured
 *                  83,844; judged-loss median 205,288).
 *       excused  : either shape, reason in EXCUSED_REASONS. Counted in
 *                  totals.excused_rebuilds, never as a loss.
 *       iron     : a counted loss with gap < 5 min (subset flag).
 *     TTL windows unchanged: 30 min main / 5 min subagent.
 *   - wasted_tokens = cache_creation_input_tokens of each counted loss.
 *   - The v2.1 series (PMNF && gap < TTL) is computed alongside, rule
 *     unchanged, as totals.pmnf_losses and per-day `pmnf`: it tracked real
 *     degradation for months, and a series measured with the same ruler is
 *     worth more than a corrected ruler with no history.
 *
 * Detector census (M15) — counting, never judgment. v3 no longer keys on any
 * reason NAME, which shrinks the renamed-reason failure mode, but the census
 * stays: a brand-new annotation should read as "a name I have not seen",
 * on screen, instead of silently joining the unexcused bucket.
 * It changes no total and no daily row.
 *
 * Input : array of {name, text} — one entry per *.jsonl file (name may carry
 *         a relative path; a "subagents" path segment marks a subagent file —
 *         path-only classification, same as the CLI SSOT).
 * Output: {totals, daily, events} — totals/daily follow 04_DATA_MODEL.md;
 *         events is render-only detail that never leaves the browser.
 *
 * Incremental scanning (M16). parseFiles() wants every file's text in memory
 * at once, and on a real log folder that is ~100 MB of strings held while
 * three separate passes walk them. createScan() is the same engine driven one
 * file at a time, so a caller can read a file, feed it, and drop the text
 * before reading the next. parseFiles() is now a thin loop over createScan(),
 * which is what keeps the refactor honest: every existing test and every
 * mutant still runs the code the page runs.
 *
 * Two optional jobs ride along on the SAME line walk rather than paying for
 * their own pass:
 *   census : the day x hour request tally the heatmap shades with. It used to
 *            be a second JSON.parse pass living in check.html.
 *   onLine : called with every non-empty line. assets/identity.js uses it to
 *            collect request ids by its OWN rules. This file must never derive
 *            the fingerprint itself: the two disagree on which id to take
 *            (identity.js requires an `msg_` prefix on the fallback, the engine
 *            takes any id), and a fingerprint that shifts is a returning
 *            submitter who stops matching their own row.
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

  var SCRIPT_VERSION = "web-2.0";
  var PMNF_REASON = "previous_message_not_found";
  var MAIN_TTL_SECONDS = 1800;
  var SUBAGENT_TTL_SECONDS = 300;
  var IRON_SECONDS = 300;
  /* The only power a reason name has: excusing a rebuild. An UNKNOWN name
     does not excuse — judgment must keep working when the server invents a
     new one, and the census is where a human meets it. */
  var EXCUSED_REASONS = ["messages_changed", "model_changed",
                         "system_changed", "tools_changed"];
  /* Fixed absolute threshold, never a per-scan percentile: the same request
     must classify the same way whatever else is in the folder, or nothing is
     reproducible. Value rationale measured 2026-08-28: fleet p99 = 83,844
     (outside the top 1% of normal writes), judged-loss median = 205,288. */
  var PROBABLE_CC_MIN = 100000;
  /* Census bookkeeping only, never judgment: the names whose appearance is
     ORDINARY. PMNF and unavailable are diagnostic-state annotations; the four
     excuses declare a prompt change. Anything else is a name the maintainers
     have not seen, and under v3 the risk it carries is inverted from v2.1:
     it cannot hide a loss (judgment reads the bill), but it COULD be a new
     excuse-type declaration whose rebuilds are being counted instead of
     excused. detector.unknown_reasons counts these. */
  var KNOWN_REASONS = [PMNF_REASON, "messages_changed", "model_changed",
                       "system_changed", "tools_changed", "unavailable"];

  /* Census keys reach a public JSON file, so a log file must never be able to
     put free text there: a value is kept verbatim only if it matches this
     charset, and anything else is counted under a fixed literal. The three
     literals below contain parentheses, which the charset excludes, so no
     real reason or version string can ever collide with one. */
  var CENSUS_KEY_RE = /^[A-Za-z0-9._-]{1,64}$/;
  var CENSUS_MAX_KEYS = 12;
  var KEY_INVALID = "(invalid)";
  var KEY_MISSING = "(none)";
  var KEY_OTHER = "(other)";

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

  function censusAdd(map, raw, missingIsKey) {
    var key;
    if (raw === null || raw === undefined) {
      if (!missingIsKey) return;
      key = KEY_MISSING;
    } else {
      key = (typeof raw === "string" && CENSUS_KEY_RE.test(raw)) ? raw : KEY_INVALID;
    }
    map.set(key, (map.get(key) || 0) + 1);
  }

  // Deterministic top-N: count desc, then key asc. Sorting rather than
  // truncating on insertion order is what lets the two engines agree — they
  // walk the same files in different orders, and only the tally is shared.
  function censusOut(map) {
    var pairs = [];
    map.forEach(function (count, key) { pairs.push([key, count]); });
    pairs.sort(function (a, b) {
      return (b[1] - a[1]) || (a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0));
    });
    var out = {};
    var other = 0;
    for (var i = 0; i < pairs.length; i++) {
      if (i < CENSUS_MAX_KEYS) out[pairs[i][0]] = pairs[i][1];
      else other += pairs[i][1];
    }
    if (other > 0) out[KEY_OTHER] = (out[KEY_OTHER] || 0) + other;
    return out;
  }

  /* The engine as a stream. Feed it one file at a time, then call finish()
     once. The state below is exactly what parseFiles() used to build in one
     go — the requestId dedup set in particular stays global across files,
     matching the CLI's single `seen`.

     options.census : also build the day x hour request tally (a Map of
                      date -> 24 counts) on this same walk.
     options.onLine : called with every non-empty line, BEFORE this engine's
                      own prefilter, so a caller's collector can never be
                      starved by a filter it did not ask for. */
  function createScan(options) {
    var opts = isPlainObject(options) ? options : {};
    var hourly = opts.census === true ? new Map() : null;
    var onLine = typeof opts.onLine === "function" ? opts.onLine : null;

    // requestId dedup is global across files (CLI parity: one `seen` set).
    var seen = new Set();
    var totals = { requests: 0, confirmed_losses: 0, probable_losses: 0,
                   iron_losses: 0, wasted_tokens: 0, pmnf_losses: 0,
                   excused_rebuilds: 0 };
    var dailyMap = new Map();
    var events = [];
    var reasonCensus = new Map();
    var versionCensus = new Map();
    // M19: the loss half of the hourly decomposition, on the same grid as
    // dateKeyOf()/the census (the record's own stamp). The request half IS
    // the census. Only counted losses land here — excused rebuilds and the
    // pmnf series are not losses, so they have no hour.
    var lossHourly = new Map();
    var detector = { diagnosed_requests: 0, unknown_reasons: 0, cold_writes: 0 };
    var fileCount = 0;

    function addFile(fileName, fileText) {
      var name = typeof fileName === "string" ? fileName : "";
      var text = typeof fileText === "string" ? fileText : "";
      fileCount += 1;
      var isSubByName = /(^|[\\/])subagents[\\/]/.test(name);
      // Per-file map (CLI parity: reason back-fill is same-file only).
      var byrid = new Map();

      forEachLine(text, function (line) {
        if (onLine) onLine(line);
        // Cheap prefilter, same as the CLI: only usage-bearing lines matter.
        if (line.indexOf('"usage"') === -1) return;
        var o;
        try {
          o = JSON.parse(line);
        } catch (e) {
          return;
        }
        if (!isPlainObject(o)) return;
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
        /* Volume counting for the heatmap's shading, never loss judgment. Cut
           on the record's own offset, exactly like dateKeyOf() below, so a
           cell and the daily row above it sit on the same grid. */
        if (hourly) {
          var localDate = new Date(ts.epochMs + ts.offsetMinutes * 60000);
          var hourKey = localDate.toISOString().slice(0, 10);
          var hourRow = hourly.get(hourKey);
          if (!hourRow) {
            hourRow = new Array(24);
            for (var h = 0; h < 24; h++) hourRow[h] = 0;
            hourly.set(hourKey, hourRow);
          }
          hourRow[localDate.getUTCHours()] += 1;
        }
        // Coerce to a finite non-negative number — a hostile JSONL could
        // smuggle a string here and ride it into the DOM (XSS vector).
        var cc = u.cache_creation_input_tokens;
        if (typeof cc !== "number" || !isFinite(cc) || cc < 0) cc = 0;
        var cr = u.cache_read_input_tokens;
        if (typeof cr !== "number" || !isFinite(cr) || cr < 0) cr = 0;
        byrid.set(rid, {
          rid: rid,
          epochMs: ts.epochMs,
          offsetMinutes: ts.offsetMinutes,
          cc: cc,
          cr: cr,
          version: o.version === undefined ? null : o.version,
          rtype: rtype,
          timestamp: o.timestamp
        });
      });

      /* Census pass. Deliberately its own loop over the same deduped records:
         the judgment loop below is the SSOT-mirrored code and stays untouched,
         so a future reader can see at a glance that counting never feeds back
         into classification. Runs after the line walk, so rtype here is the
         back-filled final value, not whatever the first record happened to
         carry. Order does not matter — censusOut() sorts. */
      byrid.forEach(function (r) {
        censusAdd(versionCensus, r.version, true);
        if (r.rtype !== null && r.rtype !== undefined) {
          censusAdd(reasonCensus, r.rtype, false);
          detector.diagnosed_requests += 1;
          if (KNOWN_REASONS.indexOf(r.rtype) === -1) detector.unknown_reasons += 1;
        }
        // A full context write with nothing read back. Normal at session
        // start; it is only ever context for the reader, never a loss.
        if (r.cc > 0 && r.cr === 0) detector.cold_writes += 1;
      });

      var isSub = isSubByName;
      var reqs = Array.from(byrid.values()).sort(function (a, b) {
        return (a.epochMs - b.epochMs) || (a.cc - b.cc);
      });

      for (var i = 0; i < reqs.length; i++) {
        var r = reqs[i];
        var date = dateKeyOf(r);
        var day = dailyMap.get(date);
        if (!day) {
          day = { date: date, requests: 0, losses: 0, pmnf: 0, wasted_tokens: 0 };
          dailyMap.set(date, day);
        }
        totals.requests += 1;
        day.requests += 1;
        var gap = i > 0 ? (r.epochMs - reqs[i - 1].epochMs) / 1000 : null;
        var ttlSeconds = isSub ? SUBAGENT_TTL_SECONDS : MAIN_TTL_SECONDS;

        // v2.1 legacy series, rule unchanged: PMNF with the gap under the
        // TTL. Kept for continuity — it is a different question from the v3
        // tiers below and the two overlap, so it is never added to them.
        if (r.rtype === PMNF_REASON && gap !== null && gap < ttlSeconds) {
          totals.pmnf_losses += 1;
          day.pmnf += 1;
        }

        // v3 judgment: the billing shape decides; a reason name only excuses.
        // No prior request = a session-start cold write, not a loss; a gap
        // over the TTL = legitimate expiry, not a loss.
        if (gap === null || gap >= ttlSeconds) continue;
        var lossShape = (r.cr === 0 && r.cc > 0) ||
                        (r.cr > 0 && r.cc >= PROBABLE_CC_MIN);
        if (!lossShape) continue;
        if (EXCUSED_REASONS.indexOf(r.rtype) !== -1) {
          // The prompt prefix really changed; re-creating it is correct.
          // Counted so the page can SAY how much it excused — an exclusion
          // the reader cannot see is indistinguishable from a blind spot.
          totals.excused_rebuilds += 1;
          events.push({
            file: name,
            requestId: r.rid,
            timestamp: r.timestamp,
            date: date,
            is_subagent: isSub,
            gap_seconds: gap,
            cache_creation_tokens: r.cc,
            cache_read_tokens: r.cr,
            reason: r.rtype,
            classification: "excused",
            iron: false
          });
          continue;
        }
        var tier = r.cr === 0 ? "confirmed" : "probable";
        var iron = gap < IRON_SECONDS;
        if (hourly) {
          var lh = lossHourly.get(date);
          if (!lh) { lh = new Array(24); for (var z = 0; z < 24; z++) lh[z] = 0; lossHourly.set(date, lh); }
          lh[new Date(r.epochMs + r.offsetMinutes * 60000).getUTCHours()] += 1;
        }
        if (tier === "confirmed") totals.confirmed_losses += 1;
        else totals.probable_losses += 1;
        if (iron) totals.iron_losses += 1;
        totals.wasted_tokens += r.cc;
        day.losses += 1;
        day.wasted_tokens += r.cc;
        events.push({
          file: name,
          requestId: r.rid,
          timestamp: r.timestamp,
          date: date,
          is_subagent: isSub,
          gap_seconds: gap,
          cache_creation_tokens: r.cc,
          cache_read_tokens: r.cr,
          reason: r.rtype,
          classification: tier,
          iron: iron
        });
      }
    }

    function finish() {
      var daily = Array.from(dailyMap.values()).sort(function (a, b) {
        return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0);
      });

      detector.reasons = censusOut(reasonCensus);
      detector.versions = censusOut(versionCensus);
      var out = { totals: totals, daily: daily, events: events,
                  detector: detector, files: fileCount };
      // Only when asked for, so parseFiles()'s result shape is unchanged and
      // nothing that consumed it before has a new key to reason about.
      if (hourly) {
        out.census = hourly;
        // M19: the submission-shaped hourly rows — the census IS the request
        // half (same grid as the daily rows), the loss half was tallied in
        // the judgment loop. Same date set as daily; zero rows for dates the
        // loss map never touched.
        var wire = [];
        for (var wd = 0; wd < daily.length; wd++) {
          var wdate = daily[wd].date;
          var reqRow = hourly.get(wdate);
          var lossRow = lossHourly.get(wdate);
          var zeros = null;
          if (!reqRow || !lossRow) {
            zeros = new Array(24);
            for (var wz = 0; wz < 24; wz++) zeros[wz] = 0;
          }
          wire.push({
            date: wdate,
            requests: (reqRow || zeros).slice(),
            losses: (lossRow || zeros).slice()
          });
        }
        out.wire_hourly = wire;
      }
      return out;
    }

    /* A running tally, for a caller that wants to show progress that means
       something. Deliberately a copy: a page holding a live reference to the
       engine's own totals object could mutate the result it is about to
       render. Reading it never changes the scan. */
    function stats() {
      return {
        files: fileCount,
        requests: totals.requests,
        confirmed_losses: totals.confirmed_losses,
        probable_losses: totals.probable_losses,
        iron_losses: totals.iron_losses,
        wasted_tokens: totals.wasted_tokens
      };
    }

    return { addFile: addFile, finish: finish, stats: stats };
  }

  /* The whole-array form, kept because the CLI parity harness, the mutation
     harness and every test drive the engine this way. It is a loop over
     createScan() and nothing else, so "the tests exercise what the page runs"
     stays true now that M16 has put the page on the streaming path. */
  function parseFiles(files) {
    if (!Array.isArray(files)) {
      throw new TypeError("parseFiles expects an array of {name, text}");
    }
    var scan = createScan();
    for (var fi = 0; fi < files.length; fi++) {
      var file = isPlainObject(files[fi]) ? files[fi] : {};
      scan.addFile(file.name, file.text);
    }
    var result = scan.finish();
    delete result.files;
    return result;
  }

  /* ---------- submission range helpers (payload shaping, not judgment) -----
   * The observatory caps one submission at 92 days (functions/api/submit.js:
   * MAX_PERIOD_DAYS / MAX_DAILY_ENTRIES). A longer scan is not an error: the
   * page lets the user pick a window. The payload must then be recomputed for
   * that window, because the API re-derives every total from daily and
   * rejects sum(daily) != totals (04_DATA_MODEL.md / 06_FUNCTIONAL_SPEC.md).
   */

  var MAX_PERIOD_DAYS = 92;

  function isDateKey(v) {
    return typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);
  }

  function isCountLike(v) {
    return typeof v === "number" && isFinite(v) && v >= 0;
  }

  // Inclusive calendar-day span, matching the API's (end-start)/86400000+1.
  function daySpan(startIso, endIso) {
    if (!isDateKey(startIso) || !isDateKey(endIso)) return null;
    var ms = Date.parse(endIso + "T00:00:00Z") - Date.parse(startIso + "T00:00:00Z");
    if (isNaN(ms)) return null;
    return Math.round(ms / 86400000) + 1;
  }

  // Latest window of at most maxDays calendar days over a list of date keys.
  // Returns the whole list when it already fits. null when there is no date.
  function clampRange(dates, maxDays) {
    if (!Array.isArray(dates)) return null;
    var keys = dates.filter(isDateKey).sort();
    if (!keys.length) return null;
    var cap = (typeof maxDays === "number" && maxDays > 0) ? maxDays : MAX_PERIOD_DAYS;
    var end = keys[keys.length - 1];
    var start = end;
    for (var i = 0; i < keys.length; i++) {
      if (daySpan(keys[i], end) <= cap) { start = keys[i]; break; }
    }
    return { start: start, end: end };
  }

  // Submission-shaped aggregate over [startIso, endIso] (inclusive).
  // totals are re-derived from the filtered daily rows so sum(daily)==totals
  // holds; iron_losses has no daily column and is recounted from events.
  // Returns null when the window holds no day with traffic.
  function filterRange(result, startIso, endIso) {
    if (!isPlainObject(result) || !Array.isArray(result.daily)) return null;
    var lo = isDateKey(startIso) ? startIso : null;
    var hi = isDateKey(endIso) ? endIso : null;
    if (lo !== null && hi !== null && lo > hi) {
      var swap = lo; lo = hi; hi = swap;
    }
    var daily = [];
    var i, d;
    for (i = 0; i < result.daily.length; i++) {
      d = result.daily[i];
      if (!isPlainObject(d) || !isDateKey(d.date)) continue;
      if (lo !== null && d.date < lo) continue;
      if (hi !== null && d.date > hi) continue;
      daily.push({
        date: d.date,
        requests: d.requests,
        losses: d.losses,
        pmnf: isCountLike(d.pmnf) ? d.pmnf : 0,
        wasted_tokens: d.wasted_tokens
      });
    }
    if (!daily.length) return null;
    daily.sort(function (a, b) {
      return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0);
    });
    var totals = { requests: 0, confirmed_losses: 0, probable_losses: 0,
                   iron_losses: 0, wasted_tokens: 0, pmnf_losses: 0 };
    for (i = 0; i < daily.length; i++) {
      totals.requests += daily[i].requests;
      totals.wasted_tokens += daily[i].wasted_tokens;
      totals.pmnf_losses += daily[i].pmnf;
    }
    var first = daily[0].date;
    var last = daily[daily.length - 1].date;
    // The daily rows carry only the combined loss count, so the tier split is
    // recounted from the events over the same days — which also keeps the
    // API's sum(daily.losses) == confirmed+probable invariant automatic.
    // Never estimate: a source without events (pasted CLI JSON) must not use
    // this path at all.
    var events = Array.isArray(result.events) ? result.events : [];
    for (i = 0; i < events.length; i++) {
      var e = events[i];
      if (!isPlainObject(e)) continue;
      if (e.classification !== "confirmed" && e.classification !== "probable") continue;
      if (!isDateKey(e.date) || e.date < first || e.date > last) continue;
      if (e.classification === "confirmed") totals.confirmed_losses += 1;
      else totals.probable_losses += 1;
      if (e.iron === true) totals.iron_losses += 1;
    }
    return { period_start: first, period_end: last, totals: totals, daily: daily };
  }

  return {
    SCRIPT_VERSION: SCRIPT_VERSION,
    MAX_PERIOD_DAYS: MAX_PERIOD_DAYS,
    parseFiles: parseFiles,
    createScan: createScan,
    daySpan: daySpan,
    clampRange: clampRange,
    filterRange: filterRange
  };
});
