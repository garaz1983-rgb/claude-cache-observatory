/*
 * store.js — optional, opt-in local persistence for the diagnosis page (M10).
 *
 * Why this exists
 *   Until now a diagnosis lived only in the open tab: closing it threw the
 *   result away, and the page had no way to tell that a later submission
 *   overlapped an earlier one. Re-submitting an overlapping period double
 *   counts on the observatory (totals are summed across submissions), so the
 *   page needs a memory of what this browser has already sent.
 *
 * What this is NOT
 *   Not a server, not a sync layer, not a profile. Everything here writes to
 *   window.localStorage of the machine the page is open on, and only after an
 *   explicit click. There is no automatic save.
 *
 * Hard rules encoded below
 *   1. Every localStorage touch is wrapped in try/catch. Reading the property
 *      itself throws in a private window or with site data blocked, so the
 *      probe cannot assume `window.localStorage` is even reachable. A storage
 *      failure disables the feature and never breaks the diagnosis.
 *   2. Whitelist-only serialisation. buildRun() constructs a fresh object
 *      field by field; nothing is copied wholesale from the parse result.
 *      File paths, requestIds, session ids, raw timestamps and conversation
 *      text can therefore not reach storage even if a future caller hands the
 *      whole event record over. tests/storage_test.py asserts this.
 *   3. Quota degradation instead of failure: a payload that does not fit is
 *      retried without per-event detail, then without the hourly census.
 *
 * UMD: Node -> module.exports, browser -> window.CacheObservatoryStore.
 * No dependencies, no network, no DOM.
 */
(function (root, factory) {
  if (typeof module === "object" && module !== null && module.exports) {
    module.exports = factory();
  } else {
    root.CacheObservatoryStore = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var STORAGE_KEY = "cco.local.v1";
  var STATE_VERSION = 1;
  var MAX_RUNS = 8;          // keep a short history; a comparison needs 2
  var MAX_SUBMISSIONS = 24;  // overlap checks only need the recent ones
  var IRON_MINUTES = 5;      // gap < 5 min == iron (parse.js IRON_SECONDS)

  /* ---------------- primitives ---------------- */

  function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }
  function isDateKey(v) {
    return typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);
  }
  // Non-negative integer or 0. Hostile/absent input must not poison the store.
  function intOf(v) {
    if (typeof v !== "number" || !isFinite(v) || v < 0) return 0;
    return Math.floor(v);
  }
  function numOf(v) {
    if (typeof v !== "number" || !isFinite(v) || v < 0) return 0;
    return v;
  }
  function strOf(v, max) {
    if (typeof v !== "string") return "";
    return v.length > max ? v.slice(0, max) : v;
  }

  // Inclusive calendar-day span, same arithmetic as parse.js daySpan().
  function daySpan(startIso, endIso) {
    if (!isDateKey(startIso) || !isDateKey(endIso)) return null;
    var ms = Date.parse(endIso + "T00:00:00Z") - Date.parse(startIso + "T00:00:00Z");
    if (isNaN(ms)) return null;
    return Math.round(ms / 86400000) + 1;
  }

  function shiftDate(iso, days) {
    if (!isDateKey(iso)) return null;
    var ms = Date.parse(iso + "T00:00:00Z");
    if (isNaN(ms)) return null;
    return new Date(ms + days * 86400000).toISOString().slice(0, 10);
  }

  function nextDay(iso) {
    return shiftDate(iso, 1);
  }

  /* Timestamp -> {date, hour, time}. Counting only, never judgment; mirrors
     parse.js parseTimestamp (record's own UTC offset, offset-less anchored to
     UTC) so buckets line up with the daily rows. */
  function tsDateHour(ts) {
    if (typeof ts !== "string" || ts === "") return null;
    var offsetMinutes = 0, hasOffset = false;
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
    var local = new Date(epochMs + offsetMinutes * 60000);
    return {
      date: local.toISOString().slice(0, 10),
      hour: local.getUTCHours(),
      time: local.toISOString().slice(11, 19)
    };
  }

  /* ---------------- storage access (every touch guarded) ---------------- */

  // Resolving the property can throw on its own (private window, blocked site
  // data), so even the lookup sits inside the try.
  function resolveStorage(storage) {
    if (storage) return storage;
    try {
      var g = (typeof globalThis !== "undefined") ? globalThis :
              (typeof self !== "undefined" ? self : null);
      if (!g) return null;
      return g.localStorage || null;
    } catch (e) {
      return null;
    }
  }

  // True only if a real write+read+delete round trip succeeds. Safari's
  // private mode used to expose localStorage but throw on setItem.
  function available(storage) {
    var s = resolveStorage(storage);
    if (!s) return false;
    var probe = STORAGE_KEY + ".probe";
    try {
      s.setItem(probe, "1");
      var back = s.getItem(probe);
      s.removeItem(probe);
      return back === "1";
    } catch (e) {
      return false;
    }
  }

  function emptyState() {
    return { v: STATE_VERSION, runs: [], submissions: [] };
  }

  function readRaw(storage) {
    var s = resolveStorage(storage);
    if (!s) return null;
    try {
      return s.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function writeRaw(storage, text) {
    var s = resolveStorage(storage);
    if (!s) return false;
    try {
      s.setItem(STORAGE_KEY, text);
      return true;
    } catch (e) {
      return false;
    }
  }

  /* Read + revalidate. Anything that does not match the schema is dropped
     rather than trusted: the value is user-editable by definition. */
  function load(storage) {
    var raw = readRaw(storage);
    if (!raw) return null;
    var o;
    try {
      o = JSON.parse(raw);
    } catch (e) {
      return null;
    }
    if (!isPlainObject(o) || o.v !== STATE_VERSION) return null;
    var runs = Array.isArray(o.runs) ? o.runs : [];
    var subs = Array.isArray(o.submissions) ? o.submissions : [];
    return {
      v: STATE_VERSION,
      runs: runs.map(reviveRun).filter(Boolean).slice(-MAX_RUNS),
      submissions: subs.map(sanitizeSubmission).filter(Boolean).slice(-MAX_SUBMISSIONS)
    };
  }

  function save(state, storage) {
    if (!isPlainObject(state)) return false;
    var trimmed = {
      v: STATE_VERSION,
      runs: (Array.isArray(state.runs) ? state.runs : []).slice(-MAX_RUNS),
      submissions: (Array.isArray(state.submissions) ? state.submissions : [])
        .slice(-MAX_SUBMISSIONS)
    };
    // Quota degradation: full -> drop per-event detail -> drop hourly census.
    // A run that lost its events can no longer recount iron losses for a
    // sub-window, which is why reviveRun marks it (events_saved:false) and the
    // page falls back to submitting the stored aggregate whole.
    var attempts = [
      trimmed,
      withoutEvents(trimmed),
      withoutCensus(withoutEvents(trimmed))
    ];
    for (var i = 0; i < attempts.length; i++) {
      var text;
      try {
        text = JSON.stringify(attempts[i]);
      } catch (e) {
        return false;
      }
      if (writeRaw(storage, text)) return true;
    }
    return false;
  }

  function withoutEvents(state) {
    return {
      v: state.v,
      runs: state.runs.map(function (r) {
        var copy = {};
        Object.keys(r).forEach(function (k) { if (k !== "events") copy[k] = r[k]; });
        copy.events = null;
        copy.events_saved = false;
        return copy;
      }),
      submissions: state.submissions
    };
  }

  function withoutCensus(state) {
    return {
      v: state.v,
      runs: state.runs.map(function (r) {
        var copy = {};
        Object.keys(r).forEach(function (k) { if (k !== "census") copy[k] = r[k]; });
        copy.census = null;
        return copy;
      }),
      submissions: state.submissions
    };
  }

  function clear(storage) {
    var s = resolveStorage(storage);
    if (!s) return false;
    try {
      s.removeItem(STORAGE_KEY);
      // Prove the removal instead of assuming it.
      return !s.getItem(STORAGE_KEY);
    } catch (e) {
      return false;
    }
  }

  /* ---------------- record building (whitelist only) ---------------- */

  function sanitizeTotals(t) {
    var o = isPlainObject(t) ? t : {};
    return {
      requests: intOf(o.requests),
      in_ttl_losses: intOf(o.in_ttl_losses),
      iron_losses: intOf(o.iron_losses),
      wasted_tokens: intOf(o.wasted_tokens)
    };
  }

  function sanitizeDaily(rows) {
    if (!Array.isArray(rows)) return [];
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var d = rows[i];
      if (!isPlainObject(d) || !isDateKey(d.date)) continue;
      out.push({
        date: d.date,
        requests: intOf(d.requests),
        losses: intOf(d.losses),
        wasted_tokens: intOf(d.wasted_tokens)
      });
    }
    out.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });
    return out;
  }

  /* Hourly request census for the heatmap background: {date, hours:[24]}.
     Accepts a Map (browser: date -> hour counts), an array of {date, usage}
     (what the page derives from that Map) or {date, hours} (a stored row).
     The stored key is `hours`, never `usage`: `usage` is the name of the
     engine's per-message token block, and the two must not be confusable in a
     record that is grepped for leaked fields. */
  function sanitizeCensus(census) {
    var rows = [];
    if (census && typeof census.forEach === "function" && !Array.isArray(census)) {
      census.forEach(function (hours, date) { rows.push({ date: date, hours: hours }); });
    } else if (Array.isArray(census)) {
      rows = census;
    } else {
      return null;
    }
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!isPlainObject(r) || !isDateKey(r.date)) continue;
      var src = Array.isArray(r.hours) ? r.hours : (Array.isArray(r.usage) ? r.usage : []);
      var hours = new Array(24);
      for (var h = 0; h < 24; h++) hours[h] = intOf(src[h]);
      out.push({ date: r.date, hours: hours });
    }
    if (!out.length) return null;
    out.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });
    return out;
  }

  /* Per-event detail, rebuilt from scratch: date, hour, time-of-day, idle gap,
     re-billed tokens, main-vs-subagent. Nothing else is carried over — the
     source record's `file`, `requestId` and raw `timestamp` are read but never
     stored, and only in-TTL/iron events are kept at all. */
  function sanitizeEvents(events) {
    if (!Array.isArray(events)) return null;
    var out = [];
    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      if (!isPlainObject(e)) continue;
      // Raw engine records carry a classification and only in-TTL/iron ones
      // are kept. A record read back from storage has none (it is derived from
      // gap_min on hydrate), so the absence of the field is not a rejection.
      var cls = e.classification;
      if (cls !== undefined && cls !== "in_ttl" && cls !== "iron") continue;
      var date = null, hour = null, time = null;
      if (isDateKey(e.date) && typeof e.hour === "number" && typeof e.time === "string") {
        date = e.date;
        hour = intOf(e.hour);
        time = /^\d{2}:\d{2}:\d{2}$/.test(e.time) ? e.time : null;
      }
      if (date === null || time === null) {
        var dh = tsDateHour(typeof e.timestamp === "string" ? e.timestamp : "");
        if (!dh) continue;
        date = dh.date;
        hour = dh.hour;
        time = dh.time;
      }
      var gapMin;
      if (typeof e.gap_min === "number") gapMin = numOf(e.gap_min);
      else gapMin = numOf(e.gap_seconds) / 60;
      out.push({
        date: date,
        hour: hour,
        time: time,
        gap_min: Math.round(gapMin * 1000) / 1000,
        tokens: intOf(e.cache_creation_tokens !== undefined ? e.cache_creation_tokens : e.tokens),
        main: !(e.is_subagent === true || e.main === false)
      });
    }
    out.sort(function (a, b) {
      if (a.date !== b.date) return a.date < b.date ? -1 : 1;
      return a.time < b.time ? -1 : (a.time > b.time ? 1 : 0);
    });
    return out;
  }

  /* Build one storable run from a parse.js result. `result.events` may be the
     raw engine output; only the six whitelisted per-event fields survive. */
  function buildRun(result, census, opts) {
    if (!isPlainObject(result)) return null;
    var o = isPlainObject(opts) ? opts : {};
    var daily = sanitizeDaily(result.daily);
    if (!daily.length) return null;
    var events = sanitizeEvents(result.events);
    return {
      saved_at: strOf(o.saved_at, 32),
      source: o.source === "cli-paste" ? "cli-paste" : "folder",
      script_version: strOf(o.script_version, 32),
      period_start: daily[0].date,
      period_end: daily[daily.length - 1].date,
      totals: sanitizeTotals(result.totals),
      daily: daily,
      census: sanitizeCensus(census),
      events: events,
      events_saved: events !== null
    };
  }

  // A run read back from storage goes through the same whitelist.
  function reviveRun(r) {
    if (!isPlainObject(r)) return null;
    var built = buildRun(
      { totals: r.totals, daily: r.daily, events: r.events },
      r.census,
      { saved_at: r.saved_at, source: r.source, script_version: r.script_version }
    );
    if (!built) return null;
    if (r.events_saved === false) {
      built.events = null;
      built.events_saved = false;
    }
    return built;
  }

  function sanitizeSubmission(s) {
    if (!isPlainObject(s)) return null;
    if (!isDateKey(s.period_start) || !isDateKey(s.period_end)) return null;
    var a = s.period_start, b = s.period_end;
    if (a > b) { var t = a; a = b; b = t; }
    return {
      period_start: a,
      period_end: b,
      submitted_at: strOf(s.submitted_at, 32),
      id: strOf(s.id, 64)
    };
  }

  function addRun(state, run) {
    var st = isPlainObject(state) ? state : emptyState();
    var runs = (Array.isArray(st.runs) ? st.runs : []).slice();
    if (run) runs.push(run);
    return {
      v: STATE_VERSION,
      runs: runs.slice(-MAX_RUNS),
      submissions: Array.isArray(st.submissions) ? st.submissions.slice() : []
    };
  }

  function addSubmission(state, sub) {
    var st = isPlainObject(state) ? state : emptyState();
    var clean = sanitizeSubmission(sub);
    var subs = (Array.isArray(st.submissions) ? st.submissions : []).slice();
    if (clean) subs.push(clean);
    return {
      v: STATE_VERSION,
      runs: Array.isArray(st.runs) ? st.runs.slice() : [],
      submissions: subs.slice(-MAX_SUBMISSIONS)
    };
  }

  /* ---------------- incremental period ---------------- */

  function lastSubmission(submissions) {
    if (!Array.isArray(submissions) || !submissions.length) return null;
    var best = null;
    for (var i = 0; i < submissions.length; i++) {
      var s = sanitizeSubmission(submissions[i]);
      if (!s) continue;
      if (!best || s.period_end > best.period_end) best = s;
    }
    return best;
  }

  /* Default share window after a previous submission: the day AFTER the last
     submitted period_end through the newest scanned day, clamped to the API's
     calendar cap. Returns null when there is nothing submitted yet.
     hasNewData=false means the scan holds no day past the last submission. */
  function incrementalRange(dates, submissions, today, maxDays) {
    var keys = (Array.isArray(dates) ? dates : []).filter(isDateKey).sort();
    var last = lastSubmission(submissions);
    if (!last) return null;
    var cap = (typeof maxDays === "number" && maxDays > 0) ? maxDays : 92;
    var from = nextDay(last.period_end);
    var since = isDateKey(today) ? Math.max(0, daySpan(last.period_end, today) - 1) : null;
    if (!keys.length) {
      return { start: null, end: null, last_end: last.period_end,
               since_days: since, has_new_data: false, clamped: false };
    }
    var fresh = keys.filter(function (d) { return d >= from; });
    if (!fresh.length) {
      return { start: null, end: null, last_end: last.period_end,
               since_days: since, has_new_data: false, clamped: false };
    }
    var end = fresh[fresh.length - 1];
    var start = fresh[0];
    var clamped = false;
    if (daySpan(start, end) > cap) {
      // Keep the newest cap days; the older tail stays available manually.
      for (var i = 0; i < fresh.length; i++) {
        if (daySpan(fresh[i], end) <= cap) { start = fresh[i]; clamped = true; break; }
      }
    }
    return { start: start, end: end, last_end: last.period_end,
             since_days: since, has_new_data: true, clamped: clamped };
  }

  /* ---------------- overlap ---------------- */

  /* Day-level intersection of [startIso, endIso] with every stored submission.
     Boundaries: a submission ending on the selected start day overlaps by one
     day; a selection starting the day after it overlaps by zero. Overlapping
     spans are merged so a day counted twice is still one day. */
  function overlapWith(startIso, endIso, submissions) {
    var empty = { days: 0, spans: [] };
    if (!isDateKey(startIso) || !isDateKey(endIso)) return empty;
    var lo = startIso, hi = endIso;
    if (lo > hi) { var t = lo; lo = hi; hi = t; }
    var list = Array.isArray(submissions) ? submissions : [];
    var spans = [];
    for (var i = 0; i < list.length; i++) {
      var s = sanitizeSubmission(list[i]);
      if (!s) continue;
      var a = s.period_start > lo ? s.period_start : lo;
      var b = s.period_end < hi ? s.period_end : hi;
      if (a <= b) spans.push({ start: a, end: b });
    }
    if (!spans.length) return empty;
    spans.sort(function (x, y) { return x.start < y.start ? -1 : (x.start > y.start ? 1 : 0); });
    var merged = [spans[0]];
    for (var j = 1; j < spans.length; j++) {
      var cur = merged[merged.length - 1];
      var nxt = spans[j];
      // Touching spans (next starts the day after cur ends) stay separate:
      // merging them would misreport two distinct submissions as one window.
      if (nxt.start <= cur.end) {
        if (nxt.end > cur.end) cur.end = nxt.end;
      } else {
        merged.push({ start: nxt.start, end: nxt.end });
      }
    }
    var days = 0;
    for (var k = 0; k < merged.length; k++) days += daySpan(merged[k].start, merged[k].end);
    return { days: days, spans: merged };
  }

  /* ---------------- history comparison ---------------- */

  function lossRate(totals) {
    var t = sanitizeTotals(totals);
    if (t.requests <= 0) return null;
    return 100 * t.in_ttl_losses / t.requests;
  }

  function compareRuns(prev, cur) {
    if (!isPlainObject(prev) || !isPlainObject(cur)) return null;
    var rp = lossRate(prev.totals), rc = lossRate(cur.totals);
    return {
      prev_saved_at: strOf(prev.saved_at, 32),
      cur_saved_at: strOf(cur.saved_at, 32),
      prev_period: prev.period_start + ".." + prev.period_end,
      cur_period: cur.period_start + ".." + cur.period_end,
      losses_prev: intOf(prev.totals && prev.totals.in_ttl_losses),
      losses_cur: intOf(cur.totals && cur.totals.in_ttl_losses),
      losses_delta: intOf(cur.totals && cur.totals.in_ttl_losses) -
                    intOf(prev.totals && prev.totals.in_ttl_losses),
      rate_prev: rp,
      rate_cur: rc,
      rate_delta: (rp === null || rc === null) ? null : rc - rp
    };
  }

  /* Rebuild the render-side shapes from a stored run.
     - census: Map(date -> usage[24]), what drawCharts() expects
     - evtMap: Map("date#hour" -> [{time, gapMin, tokens, sub}])
     - events: the minimal {date, classification} rows filterRange() reads, so
       a restored run can still recount iron losses for a narrowed window. */
  function hydrate(run) {
    if (!isPlainObject(run)) return null;
    var census = null;
    if (Array.isArray(run.census)) {
      census = new Map();
      run.census.forEach(function (r) { census.set(r.date, r.hours.slice()); });
    }
    var evtMap = new Map();
    var events = [];
    if (Array.isArray(run.events)) {
      run.events.forEach(function (e) {
        var key = e.date + "#" + e.hour;
        if (!evtMap.has(key)) evtMap.set(key, []);
        evtMap.get(key).push({
          time: e.time, gapMin: e.gap_min, tokens: e.tokens, sub: !e.main
        });
        events.push({
          date: e.date,
          classification: e.gap_min < IRON_MINUTES ? "iron" : "in_ttl"
        });
      });
    }
    return {
      result: {
        totals: sanitizeTotals(run.totals),
        daily: sanitizeDaily(run.daily),
        events: events
      },
      census: census,
      evtMap: evtMap,
      events_saved: run.events_saved !== false
    };
  }

  return {
    STORAGE_KEY: STORAGE_KEY,
    STATE_VERSION: STATE_VERSION,
    MAX_RUNS: MAX_RUNS,
    MAX_SUBMISSIONS: MAX_SUBMISSIONS,
    available: available,
    emptyState: emptyState,
    load: load,
    save: save,
    clear: clear,
    buildRun: buildRun,
    reviveRun: reviveRun,
    sanitizeSubmission: sanitizeSubmission,
    addRun: addRun,
    addSubmission: addSubmission,
    lastSubmission: lastSubmission,
    incrementalRange: incrementalRange,
    overlapWith: overlapWith,
    compareRuns: compareRuns,
    hydrate: hydrate,
    nextDay: nextDay,
    shiftDate: shiftDate,
    daySpan: daySpan,
    lossRate: lossRate
  };
});
