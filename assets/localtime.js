/*
 * localtime.js — the reader's own clock, applied to the personal screens (M12).
 *
 * Why this file exists
 *   Both engines bucket a request by the calendar date of its timestamp in the
 *   record's own UTC offset — which, for the "...Z" stamps Claude Code writes,
 *   is UTC. That is the right cut for the public dataset: submissions from many
 *   countries are summed day by day, and cutting each one by its own local date
 *   would scramble the fleet's daily totals. It is the wrong cut for the person
 *   reading their own diagnosis: a loss at 08:12 in Seoul is stamped
 *   2026-08-23T23:12:11Z and lands in the 8/23 row, one row above the day the
 *   reader means.
 *
 * What this file is allowed to do
 *   Move a request from one display bucket to another. Nothing here decides
 *   whether a request is a loss — that is the idle gap to the previous request,
 *   which no timezone can change. assets/parse.js and scripts/check_cache_loss.py
 *   are untouched and stay in parity (tests/parity_check.py), and the submission
 *   payload keeps their UTC dates. This module only re-labels what is drawn.
 *
 * The invariant this module owes
 *   Re-bucketing moves whole records between days; it never creates, drops or
 *   splits one. So for any offset:
 *     sum(local daily.requests)      == sum(utc daily.requests)
 *     sum(local daily.losses)        == sum(utc daily.losses)
 *     sum(local daily.wasted_tokens) == sum(utc daily.wasted_tokens)
 *   and at offset 0 the output is identical to the UTC input, byte for byte.
 *   tests/localtime_test.py holds all four.
 *
 * One grid, one attribution rule (M12.1)
 *   The hourly census carries whole UTC hour cells and nothing finer: there are
 *   no per-request timestamps for the requests that did NOT lose, so a UTC hour
 *   that straddles a local half-hour boundary cannot be split and has to be
 *   attributed whole to one local hour. A cell is therefore placed by the local
 *   time of the instant it BEGINS.
 *   Events carry an exact instant — but they are drawn in the cell their OWN
 *   UTC hour was attributed to, not in the cell their instant falls in. M12
 *   used the instant, and in a :30/:45 zone that tore a loss away from the very
 *   request it was: at UTC+5:30 the hour beginning 18:00Z is drawn at local
 *   23:00 on 8/23 while a loss at 18:45Z is 00:15 on 8/24 — a date the census
 *   does not have, so the loss was drawn NOWHERE and the daily row read
 *   losses:1 against requests:0. Following the cell means a loss is always
 *   drawn on top of the usage it came from and the daily rows cannot disagree.
 *   The price is paid in the label, not in the number: `time` stays the event's
 *   exact local clock, so in a :30/:45 zone it can fall just outside the round
 *   hour the cell is drawn under. check.html prints the span the cell really
 *   covers rather than a round hour it does not. Every whole-hour zone (which
 *   is nearly every reader, Seoul included) has the two rules agree exactly.
 *
 * Offsets are read per instant, never once at load
 *   hostOffsetAt() is asked at the moment being placed OR labelled, so a scan
 *   spanning a daylight-saving change gets each side's own offset. offsetAtLocal()
 *   answers the same question from the other end — "what offset was in force at
 *   this local cell" — which is what a row LABEL needs: a February row in New
 *   York is UTC-5 even when the page was opened in August at UTC-4.
 *
 * What the engines' "instant" means here
 *   parse.js buckets by epoch + the record's own offset, so that shifted value
 *   — not the raw epoch — is the thing to re-label. It is also the only thing a
 *   saved run can offer: store.js deliberately does not keep raw timestamps, so
 *   a restored run is localised from its stored date + time. For the "...Z"
 *   stamps Claude Code writes the two are the same number, which is what keeps
 *   a live scan and the same scan read back from storage identical on screen.
 *
 * UMD: Node -> module.exports, browser -> window.ObservatoryLocalTime.
 * No dependencies, no network, no DOM.
 */
(function (root, factory) {
  if (typeof module === "object" && module !== null && module.exports) {
    module.exports = factory();
  } else {
    root.ObservatoryLocalTime = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  var TIME_RE = /^\d{2}:\d{2}:\d{2}$/;

  function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }
  function isDateKey(v) { return typeof v === "string" && DATE_RE.test(v); }
  function pad2(n) { return n < 10 ? "0" + n : String(n); }
  function intOf(v) {
    if (typeof v !== "number" || !isFinite(v) || v < 0) return 0;
    return Math.floor(v);
  }
  function numOf(v) {
    if (typeof v !== "number" || !isFinite(v) || v < 0) return 0;
    return v;
  }
  function byDate(a, b) {
    return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0);
  }
  function zeros24() {
    var a = new Array(24);
    for (var i = 0; i < 24; i++) a[i] = 0;
    return a;
  }

  /* ---------------- the offset ---------------- */

  // Minutes east of UTC the host is on AT that instant, so a scan spanning a
  // DST change gets each side's own offset instead of today's.
  function hostOffsetAt(ms) { return -new Date(ms).getTimezoneOffset(); }

  // A number pins the offset (tests, and any caller that must be deterministic);
  // a function is asked per instant; anything else means "this machine".
  function resolver(offsetAt) {
    if (typeof offsetAt === "number" && isFinite(offsetAt)) {
      var fixed = Math.round(offsetAt);
      return function () { return fixed; };
    }
    if (typeof offsetAt === "function") return offsetAt;
    return hostOffsetAt;
  }

  function offsetLabel(minutes) {
    var m = (typeof minutes === "number" && isFinite(minutes)) ? Math.round(minutes) : 0;
    if (m === 0) return "UTC";
    var abs = Math.abs(m);
    var h = Math.floor(abs / 60), mm = abs % 60;
    return "UTC" + (m < 0 ? "-" : "+") + h + (mm ? ":" + pad2(mm) : "");
  }

  /* What timezone this browser is actually in — read from the machine, never
     guessed from a locale. `label` is what the page prints; `name` is the
     zone's own name in the caller's language when the platform has one
     ("Korean Standard Time", "한국 표준시"), falling back to the IANA id and
     then to the bare offset. Anything that merely repeats the number below it
     ("GMT+9", "UTC+05:45") is dropped, since the number is printed anyway. */
  function detect(locale, nowMs) {
    var when = (typeof nowMs === "number" && isFinite(nowMs)) ? nowMs : Date.now();
    var off;
    try { off = hostOffsetAt(when); } catch (e) { off = 0; }
    if (typeof off !== "number" || !isFinite(off)) off = 0;
    off = Math.round(off);
    var zone = "";
    try {
      zone = (new Intl.DateTimeFormat().resolvedOptions().timeZone) || "";
    } catch (e) { zone = ""; }
    var named = "";
    try {
      var parts = new Intl.DateTimeFormat(locale || undefined, { timeZoneName: "long" })
        .formatToParts(new Date(when));
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === "timeZoneName") named = String(parts[i].value || "");
      }
    } catch (e) { named = ""; }
    if (/\d/.test(named) || /^(GMT|UTC)\b/i.test(named)) named = "";
    var num = offsetLabel(off);
    var human = named || zone;
    // Korean, Japanese and Chinese set a parenthetical tight against the word
    // it qualifies; an English label keeps the space.
    var tight = /^(ko|ja|zh)\b/i.test(String(locale || ""));
    return {
      offsetMinutes: off,
      offset: num,
      zone: zone,
      name: named,
      label: human ? human + (tight ? "(" : " (") + num + ")" : num,
      isUtc: off === 0
    };
  }

  /* ---------------- instants ---------------- */

  function stampMs(dateKey, time) {
    if (!isDateKey(dateKey) || typeof time !== "string" || !TIME_RE.test(time)) return null;
    var ms = Date.parse(dateKey + "T" + time + "Z");
    return isNaN(ms) ? null : ms;
  }

  function cellMs(dateKey, hour) {
    var h = intOf(hour);
    if (h > 23) return null;
    return stampMs(dateKey, pad2(h) + ":00:00");
  }

  // Mirrors parse.js parseTimestamp and folds in its dateKeyOf shift, so the
  // number returned is exactly the instant the engine bucketed by.
  function bucketMsOf(ts) {
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
    return epochMs + offsetMinutes * 60000;
  }

  // Calendar parts of an instant in the reader's zone.
  function partsAt(ms, offsetAt) {
    if (typeof ms !== "number" || !isFinite(ms)) return null;
    var off = resolver(offsetAt)(ms);
    if (typeof off !== "number" || !isFinite(off)) off = 0;
    off = Math.round(off);
    var iso;
    try { iso = new Date(ms + off * 60000).toISOString(); } catch (e) { return null; }
    return {
      date: iso.slice(0, 10),
      hour: parseInt(iso.slice(11, 13), 10),
      time: iso.slice(11, 19),
      offsetMinutes: off
    };
  }

  /* The offset in force at a LOCAL wall-clock cell. The instant is not known
     until the offset is, so it is solved the usual way: read the offset at the
     same numbers taken as UTC, then re-read it at the instant that produces.
     This is what a label needs — partsAt() answers "which cell does this
     instant belong to", offsetAtLocal() answers "what offset does that cell
     carry", and printing today's offset over a February row instead was the
     hour-out mislabel M12.1 fixes. A pinned numeric offset answers itself,
     which is what keeps the tests deterministic. */
  function offsetAtLocal(dateKey, hour, offsetAt) {
    var res = resolver(offsetAt);
    var guess = cellMs(dateKey, hour);
    if (guess === null) return null;
    var off = res(guess);
    if (typeof off !== "number" || !isFinite(off)) off = 0;
    off = Math.round(off);
    var settled = res(guess - off * 60000);
    if (typeof settled !== "number" || !isFinite(settled)) return off;
    return Math.round(settled);
  }

  // The engines' own bucket for an instant, for printing next to the local one.
  function utcPartsOf(ms) {
    if (typeof ms !== "number" || !isFinite(ms)) return null;
    var iso;
    try { iso = new Date(ms).toISOString(); } catch (e) { return null; }
    return { date: iso.slice(0, 10), hour: parseInt(iso.slice(11, 13), 10), time: iso.slice(11, 19) };
  }

  /* ---------------- census ---------------- */

  // Accepts the page's Map(date -> hours[24]), the stored [{date, hours}] rows,
  // or the [{date, usage}] shape the page hands to store.js.
  function censusRows(census) {
    if (!census) return null;
    var rows = [];
    if (typeof census.forEach === "function" && !Array.isArray(census)) {
      census.forEach(function (hours, date) { rows.push({ date: date, hours: hours }); });
    } else if (Array.isArray(census)) {
      for (var i = 0; i < census.length; i++) {
        var r = census[i];
        if (!isPlainObject(r)) continue;
        rows.push({ date: r.date, hours: Array.isArray(r.hours) ? r.hours : r.usage });
      }
    } else {
      return null;
    }
    var out = [];
    for (var j = 0; j < rows.length; j++) {
      if (!isDateKey(rows[j].date) || !Array.isArray(rows[j].hours)) continue;
      out.push(rows[j]);
    }
    return out;
  }

  /* Re-cut the hourly request census into the reader's calendar. A cell moves
     whole, so the total is carried across unchanged; a cell whose instant
     cannot be formed stays where it was rather than being dropped, because
     losing it would break the sum this module exists to preserve.
     `seen` (optional) collects the distinct offsets the move used, so a caller
     can tell a scan that crossed a daylight-saving change from one that did
     not without walking the census a second time. */
  function placeCensus(census, offsetAt, seen) {
    var rows = censusRows(census);
    if (rows === null) return null;
    var out = new Map();
    function bucketOf(date) {
      var a = out.get(date);
      if (!a) { a = zeros24(); out.set(date, a); }
      return a;
    }
    function note(p) {
      if (!seen || !p) return;
      if (seen.indexOf(p.offsetMinutes) === -1) seen.push(p.offsetMinutes);
    }
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var total = 0;
      for (var h = 0; h < 24; h++) {
        var n = intOf(row.hours[h]);
        if (!n) continue;
        total += n;
        var p = partsAt(cellMs(row.date, h), offsetAt);
        note(p);
        if (p) bucketOf(p.date)[p.hour] += n;
        else bucketOf(row.date)[h] += n;
      }
      // An all-zero row still names a day the scan covered; keep the row.
      if (total === 0) {
        var p0 = partsAt(cellMs(row.date, 0), offsetAt);
        note(p0);
        bucketOf(p0 ? p0.date : row.date);
      }
    }
    return out;
  }

  function localizeCensus(census, offsetAt) {
    return placeCensus(census, offsetAt, null);
  }

  /* ---------------- events ---------------- */

  // A live engine record carries `timestamp`; a run read back from storage
  // carries the date + clock time store.js derived from it. Either resolves to
  // the same instant for the stamps Claude Code writes.
  function eventMs(e) {
    if (typeof e.timestamp === "string" && e.timestamp !== "") {
      var ms = bucketMsOf(e.timestamp);
      if (ms !== null) return ms;
    }
    return stampMs(e.date, e.time);
  }

  /* Per-event detail in the reader's calendar. Only in-TTL and iron events are
     drawn, exactly as before; the classification is read, never re-decided.

     `date`/`hour` are the CELL — the local cell this event's own UTC hour was
     attributed to, so it is always a cell the census has and always the cell
     holding the request this loss was. `time` is the event's exact local clock,
     and `offsetMinutes` is the offset in force at that instant, so a row can be
     labelled with its own offset instead of the page's load-time one. In a
     whole-hour zone cell and instant agree and the two are the same thing; in a
     :30/:45 zone `time` can sit just outside the round hour of `hour`, which is
     what the page has to say on screen (see the module header). */
  function localizeEvents(events, offsetAt) {
    if (!Array.isArray(events)) return [];
    var out = [];
    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      if (!isPlainObject(e)) continue;
      var cls = e.classification;
      // v3 (M18): the counted losses are confirmed/probable; excused events
      // exist in the stream but are not losses and must not shade the heatmap.
      if (cls !== "confirmed" && cls !== "probable") continue;
      var ms = eventMs(e);
      if (ms === null) continue;
      var p = partsAt(ms, offsetAt);
      if (!p) continue;
      var u = utcPartsOf(ms);
      var cell = u ? partsAt(cellMs(u.date, u.hour), offsetAt) : null;
      if (!cell) cell = p;
      out.push({
        ms: ms,
        date: cell.date,
        hour: cell.hour,
        time: p.time,
        offsetMinutes: p.offsetMinutes,
        cellOffsetMinutes: cell.offsetMinutes,
        gapMin: (typeof e.gap_min === "number") ? numOf(e.gap_min) : numOf(e.gap_seconds) / 60,
        tokens: intOf(e.cache_creation_tokens !== undefined ? e.cache_creation_tokens : e.tokens),
        sub: (e.is_subagent === true) || (e.main === false),
        utc_date: u ? u.date : null,
        utc_time: u ? u.time : null
      });
    }
    // By instant, not by (date, time): a :30/:45 cell can span local midnight,
    // where 00:15 is LATER than 23:45 and sorting on the clock would reverse
    // the pair inside the popover.
    out.sort(function (a, b) { return a.ms - b.ms; });
    return out;
  }

  function evtMapOf(localEvents) {
    var map = new Map();
    (localEvents || []).forEach(function (e) {
      var key = e.date + "#" + e.hour;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push({
        time: e.time, gapMin: e.gapMin, tokens: e.tokens, sub: e.sub,
        offsetMinutes: e.offsetMinutes
      });
    });
    return map;
  }

  /* ---------------- daily rows ---------------- */

  /* Requests come from the census (one cell = one whole bucket), losses and
     re-billed tokens from the events. Both sources are re-cut copies of what
     the engine already counted, so every column sums to the engine's total. */
  function localDaily(localCensus, localEvents) {
    var map = new Map();
    function row(date) {
      var r = map.get(date);
      if (!r) { r = { date: date, requests: 0, losses: 0, wasted_tokens: 0 }; map.set(date, r); }
      return r;
    }
    if (localCensus && typeof localCensus.forEach === "function") {
      localCensus.forEach(function (hours, date) {
        var r = row(date);
        for (var h = 0; h < 24; h++) r.requests += intOf(hours[h]);
      });
    }
    (localEvents || []).forEach(function (e) {
      var r = row(e.date);
      r.losses += 1;
      r.wasted_tokens += intOf(e.tokens);
    });
    var out = [];
    map.forEach(function (r) { out.push(r); });
    out.sort(byDate);
    return out;
  }

  function copyDaily(d) {
    return {
      date: d.date,
      requests: intOf(d.requests),
      losses: intOf(d.losses),
      wasted_tokens: intOf(d.wasted_tokens)
    };
  }

  function periodOf(daily) {
    if (!Array.isArray(daily) || !daily.length) return null;
    return { start: daily[0].date, end: daily[daily.length - 1].date };
  }

  function sumsOf(daily) {
    var t = { requests: 0, losses: 0, wasted_tokens: 0 };
    (Array.isArray(daily) ? daily : []).forEach(function (d) {
      t.requests += intOf(d.requests);
      t.losses += intOf(d.losses);
      t.wasted_tokens += intOf(d.wasted_tokens);
    });
    return t;
  }

  /* ---------------- the one call the pages make ---------------- */

  /* Everything the personal screens draw, in the reader's calendar.
     `localized:false` means the source carries no per-request detail — pasted
     CLI aggregates, or a saved run whose detail did not survive the storage
     quota fallback. There is then nothing to move, so the UTC rows are handed
     back untouched and the page has to keep saying so on screen. */
  function localize(result, census, offsetAt) {
    var utcDaily = (isPlainObject(result) && Array.isArray(result.daily)) ? result.daily : [];
    var utc = utcDaily.map(copyDaily);
    var utcPeriod = periodOf(utc);
    if (!census) {
      return {
        localized: false,
        daily: utc,
        census: null,
        evtMap: new Map(),
        events: [],
        period: utcPeriod,
        utcPeriod: utcPeriod,
        utcDaily: utc,
        offsetAt: offsetAt,
        offsets: []
      };
    }
    var seen = [];
    var lc = placeCensus(census, offsetAt, seen);
    var le = localizeEvents(isPlainObject(result) ? result.events : [], offsetAt);
    var daily = localDaily(lc, le);
    le.forEach(function (e) {
      if (seen.indexOf(e.offsetMinutes) === -1) seen.push(e.offsetMinutes);
    });
    seen.sort(function (a, b) { return a - b; });
    return {
      localized: true,
      daily: daily,
      census: lc,
      evtMap: evtMapOf(le),
      events: le,
      period: periodOf(daily),
      utcPeriod: utcPeriod,
      utcDaily: utc,
      // The resolver as given, so a caller labelling a cell asks the same
      // clock the placement asked instead of re-detecting one of its own.
      offsetAt: offsetAt,
      // Every distinct offset this view is drawn on. More than one means the
      // scan crossed a daylight-saving change and the page has to say so.
      offsets: seen
    };
  }

  return {
    hostOffsetAt: hostOffsetAt,
    offsetLabel: offsetLabel,
    detect: detect,
    stampMs: stampMs,
    cellMs: cellMs,
    bucketMsOf: bucketMsOf,
    partsAt: partsAt,
    offsetAtLocal: offsetAtLocal,
    utcPartsOf: utcPartsOf,
    localizeCensus: localizeCensus,
    localizeEvents: localizeEvents,
    evtMapOf: evtMapOf,
    localDaily: localDaily,
    periodOf: periodOf,
    sumsOf: sumsOf,
    localize: localize
  };
});
