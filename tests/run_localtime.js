#!/usr/bin/env node
/*
 * run_localtime.js — test helper for assets/localtime.js (M12 local-time view).
 *
 * Usage: node run_localtime.js <fixture_dir> <offsets_json>
 *        offsets_json = [0, 540, 330, -300, ...]  (minutes east of UTC)
 *
 * Feeds a fixture directory through the real engine (assets/parse.js), builds
 * the same hourly census check.html builds, and prints — for every requested
 * offset — the localised view assets/localtime.js produces, alongside two
 * independent baselines the checker compares against:
 *
 *   legacy   what the page drew BEFORE M12: parse.js's own daily rows, the UTC
 *            census, and the "date#hour" event map rebuilt here with a literal
 *            copy of check.html's old tsDateHour(). Nothing in this baseline
 *            calls localtime.js, so "offset 0 changes nothing" is a comparison
 *            between two separate implementations, not a tautology.
 *   restored the same scan saved through assets/store.js, read back and
 *            hydrated, then localised. A stored run keeps UTC-anchored values,
 *            so it must localise to the same screen as the live scan.
 *
 * `host` is the view with no offset argument at all — the browser path, where
 * the offset comes from this machine per instant. Run the process under TZ=UTC
 * and it must match the legacy baseline byte for byte.
 *
 * JSON strings (…_json) are emitted so the checker can assert byte identity
 * rather than structural equality.
 */
"use strict";

var fs = require("fs");
var path = require("path");
var engine = require(path.join(__dirname, "..", "assets", "parse.js"));
var store = require(path.join(__dirname, "..", "assets", "store.js"));
var lt = require(path.join(__dirname, "..", "assets", "localtime.js"));

function collect(dir, prefix, out) {
  var entries = fs.readdirSync(dir, { withFileTypes: true }).sort(function (a, b) {
    return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0);
  });
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    var abs = path.join(dir, e.name);
    var rel = prefix ? prefix + "/" + e.name : e.name;
    if (e.isDirectory()) {
      collect(abs, rel, out);
    } else if (e.isFile() && e.name.slice(-6) === ".jsonl") {
      out.push({ name: rel, text: fs.readFileSync(abs, "utf8") });
    }
  }
  return out;
}

function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }

/* ---- check.html's pre-M12 timestamp handling, copied literally ----
   Kept as a hand copy on purpose: it is the baseline the offset-0 claim is
   measured against, so it must not go through the module under test. */
function legacyTsParse(ts) {
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
  return { epochMs: epochMs, offsetMinutes: offsetMinutes };
}
function legacyTsDateHour(ts) {
  var p = legacyTsParse(ts);
  if (!p) return null;
  var local = new Date(p.epochMs + p.offsetMinutes * 60000);
  return {
    date: local.toISOString().slice(0, 10),
    hour: local.getUTCHours(),
    timeStr: local.toISOString().slice(11, 19)
  };
}

/* ---- the hourly census, mirroring check.html usageCensus() ----
   This is a copy of page code, so the checker also asserts that its total
   equals the engine's totals.requests: a drift between the two shows up as a
   failed count rather than as a quietly different heatmap. */
function usageCensus(files) {
  var seen = new Set();
  var byDate = new Map();
  files.forEach(function (f) {
    var lines = f.text.split("\n");
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf('"usage"') === -1) continue;
      var o;
      try { o = JSON.parse(line); } catch (e) { continue; }
      if (!isObj(o)) continue;
      var msg = isObj(o.message) ? o.message : {};
      if (!isObj(msg.usage)) continue;
      var rid = o.requestId || msg.id || null;
      if (seen.has(rid)) continue;
      seen.add(rid);
      var dh = legacyTsDateHour(typeof o.timestamp === "string" ? o.timestamp : "");
      if (!dh) continue;
      var arr = byDate.get(dh.date);
      if (!arr) { arr = new Array(24).fill(0); byDate.set(dh.date, arr); }
      arr[dh.hour] += 1;
    }
  });
  return byDate;
}

function censusRowsOf(map) {
  var out = [];
  if (!map) return null;
  map.forEach(function (hours, date) { out.push({ date: date, hours: hours.slice() }); });
  out.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });
  return out;
}

function evtRowsOf(map) {
  var keys = Array.from(map.keys()).sort();
  return keys.map(function (k) { return [k, map.get(k)]; });
}

function legacyEvtMap(events) {
  var m = new Map();
  events.forEach(function (e) {
    if (e.classification !== "in_ttl" && e.classification !== "iron") return;
    var dh = legacyTsDateHour(e.timestamp);
    if (!dh) return;
    var key = dh.date + "#" + dh.hour;
    if (!m.has(key)) m.set(key, []);
    m.get(key).push({
      time: dh.timeStr, gapMin: e.gap_seconds / 60,
      tokens: e.cache_creation_tokens, sub: e.is_subagent
    });
  });
  m.forEach(function (list) { list.sort(function (a, b) { return a.time < b.time ? -1 : 1; }); });
  return m;
}

function viewOut(v) {
  var census = v.census ? censusRowsOf(v.census) : null;
  var evt = evtRowsOf(v.evtMap);
  return {
    localized: v.localized,
    daily: v.daily,
    daily_json: JSON.stringify(v.daily),
    census_json: JSON.stringify(census),
    evt_keys: evt.map(function (p) { return p[0]; }),
    evt_json: JSON.stringify(evt),
    events: v.events,
    period: v.period,
    utc_period: v.utcPeriod,
    sums: lt.sumsOf(v.daily),
    census_total: census
      ? census.reduce(function (a, r) {
          return a + r.hours.reduce(function (x, y) { return x + y; }, 0);
        }, 0)
      : null
  };
}

function okStorage() {
  var m = new Map();
  return {
    getItem: function (k) { return m.has(k) ? m.get(k) : null; },
    setItem: function (k, v) { m.set(k, String(v)); },
    removeItem: function (k) { m.delete(k); }
  };
}

var root = process.argv[2];
if (!root) {
  process.stderr.write("usage: node run_localtime.js <fixture_dir> <offsets_json>\n");
  process.exit(2);
}
var offsets = JSON.parse(process.argv[3] || "[0]");

var files = collect(root, "", []);
var result = engine.parseFiles(files);
var census = usageCensus(files);

/* live views, one per requested offset */
var views = {};
offsets.forEach(function (off) {
  views[String(off)] = viewOut(lt.localize(result, census, off));
});

/* the same scan, saved and read back, then localised at display time */
var s = okStorage();
var run = store.buildRun(result, census, {
  saved_at: "2026-08-24T09:00:00.000Z",
  source: "folder",
  script_version: engine.SCRIPT_VERSION
});
store.save(store.addRun(store.emptyState(), run), s);
var back = store.load(s);
var hyd = back && back.runs.length ? store.hydrate(back.runs[back.runs.length - 1]) : null;
var restored = {};
if (hyd) {
  offsets.forEach(function (off) {
    restored[String(off)] = viewOut(lt.localize(hyd.result, hyd.census, off));
  });
}

/* the pasted-CLI shape: aggregates only, nothing left to re-cut */
var aggregateOnly = viewOut(lt.localize(
  { totals: result.totals, daily: result.daily, events: [] }, null, 540));

/* baselines that never touch localtime.js */
var legacyCensus = censusRowsOf(census);
var legacy = {
  daily_json: JSON.stringify(result.daily),
  census_json: JSON.stringify(legacyCensus),
  evt_json: JSON.stringify(evtRowsOf(legacyEvtMap(result.events))),
  evt_keys: Array.from(legacyEvtMap(result.events).keys()).sort()
};

/* the browser path: no offset argument, so the host machine answers */
var hostView = viewOut(lt.localize(result, census));

process.stdout.write(JSON.stringify({
  fixture: root,
  totals: result.totals,
  utc_daily: result.daily,
  utc_events: result.events.map(function (e) {
    return { timestamp: e.timestamp, date: e.date, gap_seconds: e.gap_seconds,
             tokens: e.cache_creation_tokens, classification: e.classification };
  }),
  legacy: legacy,
  views: views,
  restored: restored,
  aggregate_only: aggregateOnly,
  host: {
    tz_env: process.env.TZ || null,
    zone: (function () {
      try { return new Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return null; }
    })(),
    offset_now: lt.hostOffsetAt(Date.now()),
    offset_at_boundary: lt.hostOffsetAt(Date.parse("2026-08-23T23:12:11Z")),
    detect: lt.detect("en-US"),
    view: hostView
  },
  labels: [0, 540, 330, -300, 825, -210, 60].reduce(function (a, o) {
    a[String(o)] = lt.offsetLabel(o);
    return a;
  }, {})
}));
