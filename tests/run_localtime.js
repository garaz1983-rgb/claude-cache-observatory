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
 *
 * M12.1 additions — three things the module alone cannot answer:
 *   page_payload  the submission payload check.html / ko/check.html actually
 *                 build, by lifting their SUBMIT-PAYLOAD block verbatim and
 *                 running it against a LAST and a VIEW that deliberately
 *                 disagree. A payload built from the localised view is then a
 *                 failed assertion instead of a silently accepted KST-cut
 *                 submission.
 *   page_labels   the strings those pages PRINT on a heatmap cell, in a
 *                 popover header and in its time column, by lifting their
 *                 LOCALTIME-LABELS block the same way. `ny` drives it with a
 *                 pinned 2026 America/New_York DST function rather than the
 *                 host zone, so a February row is asserted to say UTC-5 on a
 *                 machine that is anywhere at all.
 *   unit          direct probes of localizeCensus / offsetAtLocal for the
 *                 branches no fixture reaches (an all-zero census row, a local
 *                 cell on either side of a DST change).
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

/* `offsetMinutes: 0` is not a field the pre-M12 page carried per row — it
   printed one detected offset for every row on the page. Under this baseline
   (offset 0 / TZ=UTC) that one offset IS UTC, so stating it per row here is the
   same claim written per row: at offset 0 nothing on this screen is labelled
   anything but UTC. */
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
      tokens: e.cache_creation_tokens, sub: e.is_subagent,
      offsetMinutes: 0
    });
  });
  m.forEach(function (list) { list.sort(function (a, b) { return a.time < b.time ? -1 : 1; }); });
  return m;
}

/* ---- page code under test, lifted between its own markers ----
   The block is evaluated as-is: no re-implementation here can drift from the
   page, and a mutation applied to the page is a mutation applied to this test.
   Everything the block reads is passed in, so it can neither touch the DOM nor
   reach a global the page happens to have. */
function pageBlock(rel, name) {
  var abs = path.join(__dirname, "..", rel.split("/").join(path.sep));
  var src = fs.readFileSync(abs, "utf8");
  var begin = "/* " + name + ":BEGIN */";
  var end = "/* " + name + ":END */";
  var a = src.indexOf(begin), b = src.indexOf(end);
  if (a === -1 || b === -1 || b < a) {
    throw new Error("marker " + name + " missing or out of order in " + rel);
  }
  if (src.indexOf(begin, a + 1) !== -1 || src.indexOf(end, b + 1) !== -1) {
    throw new Error("marker " + name + " occurs more than once in " + rel);
  }
  return src.slice(a + begin.length, b);
}

function hh2(h) { return String(h).padStart(2, "0"); }

function labelsOf(rel, view, zone) {
  var body = pageBlock(rel, "LOCALTIME-LABELS");
  var make = new Function("ObservatoryLocalTime", "VIEW", "ZONE", "hh", body +
    "\nreturn { cellOffsetOf: cellOffsetOf, cellClockLabel: cellClockLabel," +
    " evtOffsets: evtOffsets, evtTimeHeader: evtTimeHeader," +
    " evtTimeText: evtTimeText, heatmapNoteText: heatmapNoteText };");
  return make(lt, view, zone, hh2);
}

/* M16: the local-save default, lifted out of the page and driven against a
   stub storage. It is one boolean, and it decides whether a stranger's
   diagnosis is left behind on a shared computer, so it gets a test rather than
   a screenshot. */
function autosaveOf(rel) {
  var body = pageBlock(rel, "AUTOSAVE");
  function withStore(store) {
    var make = new Function("window",
      body + "\nreturn { autoSaveOn: autoSaveOn, setAutoSave: setAutoSave, KEY: AUTOSAVE_KEY };");
    return make({ localStorage: store });
  }
  function memStore() {
    var m = Object.create(null);
    return {
      _m: m,
      getItem: function (k) { return k in m ? m[k] : null; },
      setItem: function (k, v) { m[k] = String(v); },
      removeItem: function (k) { delete m[k]; }
    };
  }
  var fresh = memStore();
  var a = withStore(fresh);
  var out = { key: a.KEY, fresh_is_on: a.autoSaveOn() };

  var off = memStore();
  off.setItem(a.KEY, "off");
  out.off_is_off = withStore(off).autoSaveOn() === false;

  var w = memStore();
  var b = withStore(w);
  b.setAutoSave(false);
  out.set_false_writes_off = w.getItem(a.KEY) === "off" && b.autoSaveOn() === false;
  b.setAutoSave(true);
  out.set_true_clears = w.getItem(a.KEY) === null && b.autoSaveOn() === true;

  // A browser that refuses storage cannot keep anything, so claiming saving is
  // on would be the one lie this card must not tell.
  var throwing = {
    getItem: function () { throw new Error("blocked"); },
    setItem: function () { throw new Error("blocked"); },
    removeItem: function () { throw new Error("blocked"); }
  };
  var c = withStore(throwing);
  out.blocked_is_off = c.autoSaveOn() === false;
  var threw = false;
  try { c.setAutoSave(false); } catch (e) { threw = true; }
  out.blocked_set_survives = !threw;

  // Anything other than the exact string "off" means on: a half-written or
  // hand-edited value must not silently disable a documented default.
  var junk = memStore();
  junk.setItem(a.KEY, "OFF");
  out.junk_is_on = withStore(junk).autoSaveOn() === true;
  return out;
}

function payloadOf(rel, last, view, fields, rangeReady) {
  var body = pageBlock(rel, "SUBMIT-PAYLOAD");
  var doc = {
    getElementById: function (id) { return fields[id] || { value: "" }; }
  };
  var make = new Function("LAST", "VIEW", "RANGE_READY", "document",
    "CacheObservatory", body + "\nreturn buildSubmitPayload();");
  return make(last, view, rangeReady, doc, engine);
}

/* 2026 America/New_York, pinned. A function resolver rather than the host zone,
   so the DST assertions hold on a machine sitting anywhere. */
var NY_DST_ON = Date.parse("2026-03-08T07:00:00Z");
var NY_DST_OFF = Date.parse("2026-11-01T06:00:00Z");
function nyOffset(ms) { return (ms >= NY_DST_ON && ms < NY_DST_OFF) ? -240 : -300; }

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

/* M16: the hourly census moved into assets/parse.js so the folder scan walks
   each file once instead of three times. usageCensus() above stays here as an
   INDEPENDENT implementation of the same rule — keeping it is the only reason
   this comparison means anything. */
var engineCensus = (function () {
  var scan = engine.createScan({ census: true });
  for (var i = 0; i < files.length; i++) scan.addFile(files[i].name, files[i].text);
  return scan.finish().census;
})();
function censusEqual(a, b) {
  var ka = Array.from(a.keys()).sort();
  var kb = Array.from(b.keys()).sort();
  if (JSON.stringify(ka) !== JSON.stringify(kb)) return false;
  for (var i = 0; i < ka.length; i++) {
    if (JSON.stringify(a.get(ka[i])) !== JSON.stringify(b.get(ka[i]))) return false;
  }
  return true;
}

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

/* ---- D3: the payload the PAGES build ----
   LAST is the engine's own UTC output; VIEW is the same scan on a Seoul clock,
   which cuts fixtures_tz into different days. The payload must be the first and
   never the second, on both the whole-scan path (no period picker) and the
   ranged path (picker open on the full UTC range). */
var pagePayloadView = lt.localize(result, census, 540);
var utcFirst = result.daily[0].date;
var utcLast = result.daily[result.daily.length - 1].date;
/* M13: the same probe also carries a fingerprint and a link token, so "the page
   still sends what keeps a submitter to one row" is a test rather than a habit.
   A page that quietly stops attaching them would double count again, and the
   API cannot notice: a payload without anchors is perfectly valid. */
var PROBE_ANCHORS = ["a1", "a2", "a3"].map(function (x) {
  return require("crypto").createHash("sha256").update(x).digest("hex");
});
var PROBE_TOKEN = "0123456789abcdef0123456789abcdef";
var pagePayload = {};
["check.html", "ko/check.html"].forEach(function (rel) {
  var last = { result: result, census: census, source_version: null,
               detail_dropped: false, anchors: PROBE_ANCHORS,
               link_token: PROBE_TOKEN };
  var blank = {
    subNickname: { value: "" }, subPlan: { value: "unknown" },
    subClient: { value: "unknown" }, subSessions: { value: "unknown" },
    subFrom: { value: "" }, subTo: { value: "" }
  };
  var ranged = {
    subNickname: { value: "" }, subPlan: { value: "unknown" },
    subClient: { value: "unknown" }, subSessions: { value: "unknown" },
    subFrom: { value: utcFirst }, subTo: { value: utcLast }
  };
  // The same page with no fingerprint at all: the fields must then be ABSENT,
  // never invented, guessed or defaulted.
  var bare = { result: result, census: census, source_version: null,
               detail_dropped: false, anchors: [], link_token: "" };
  pagePayload[rel] = {
    whole: payloadOf(rel, last, pagePayloadView, blank, false),
    ranged: payloadOf(rel, last, pagePayloadView, ranged, true),
    bare: payloadOf(rel, bare, pagePayloadView, ranged, true),
    view_daily: pagePayloadView.daily,
    view_period: pagePayloadView.period
  };
});
var probeIdentity = { anchors: PROBE_ANCHORS, token: PROBE_TOKEN };

/* ---- D2: the strings the PAGES print ---- */
var ZONE_STUB = {
  ny: { offsetMinutes: -240, offset: "UTC-4", isUtc: false },
  kolkata: { offsetMinutes: 330, offset: "UTC+5:30", isUtc: false },
  seoul: { offsetMinutes: 540, offset: "UTC+9", isUtc: false },
  utc: { offsetMinutes: 0, offset: "UTC", isUtc: true }
};
// A page opened in August (EDT) reading a scan that also covers February.
var VIEW_NY = { offsetAt: nyOffset, offsets: [-300, -240], census: true };
var VIEW_KOLKATA = { offsetAt: 330, offsets: [330], census: true };
var VIEW_SEOUL = { offsetAt: 540, offsets: [540], census: true };
var VIEW_UTC = { offsetAt: 0, offsets: [0], census: true };
var pageLabels = {};
["check.html", "ko/check.html"].forEach(function (rel) {
  var ny = labelsOf(rel, VIEW_NY, ZONE_STUB.ny);
  var kol = labelsOf(rel, VIEW_KOLKATA, ZONE_STUB.kolkata);
  var seo = labelsOf(rel, VIEW_SEOUL, ZONE_STUB.seoul);
  var utc = labelsOf(rel, VIEW_UTC, ZONE_STUB.utc);
  var febRows = [{ time: "13:02:00", gapMin: 2, tokens: 44000, sub: false, offsetMinutes: -300 }];
  var augRows = [{ time: "14:02:00", gapMin: 2, tokens: 33000, sub: false, offsetMinutes: -240 }];
  var splitRows = febRows.concat(augRows);   // the mid-UTC-hour DST case
  pageLabels[rel] = {
    ny_cell_feb: ny.cellClockLabel("2026-02-15", 13),
    ny_cell_aug: ny.cellClockLabel("2026-08-15", 14),
    ny_offset_feb: ny.cellOffsetOf("2026-02-15", 13),
    ny_offset_aug: ny.cellOffsetOf("2026-08-15", 14),
    ny_head_feb: ny.evtTimeHeader(febRows),
    ny_head_aug: ny.evtTimeHeader(augRows),
    ny_head_split: ny.evtTimeHeader(splitRows),
    ny_time_uniform: ny.evtTimeText(febRows[0], true),
    ny_time_split: ny.evtTimeText(febRows[0], false),
    ny_note: ny.heatmapNoteText(VIEW_NY.offsets, -240),
    kolkata_cell: kol.cellClockLabel("2026-08-23", 23),
    kolkata_note: kol.heatmapNoteText(VIEW_KOLKATA.offsets, 330),
    seoul_cell: seo.cellClockLabel("2026-08-24", 8),
    seoul_note: seo.heatmapNoteText(VIEW_SEOUL.offsets, 540),
    utc_cell: utc.cellClockLabel("2026-08-23", 23),
    utc_note: utc.heatmapNoteText(VIEW_UTC.offsets, 0)
  };
  if (rel.indexOf("ko/") === 0) {
    // The KO popover header writes the hour with 시, the cell title with :00.
    pageLabels[rel].seoul_cell_han = seo.cellClockLabel("2026-08-24", 8, true);
    pageLabels[rel].kolkata_cell_han = kol.cellClockLabel("2026-08-23", 23, true);
  }
});

/* ---- unit probes for branches no fixture reaches ---- */
function zeros24() { return new Array(24).fill(0); }
var allZeroIn = [
  { date: "2026-08-20", hours: zeros24() },          // a day the scan covered,
  { date: "2026-08-21", hours: (function () {         // with no request in it
      var a = zeros24(); a[23] = 1; return a;
    })() }
];
var unit = {
  all_zero_row_utc: Array.from(lt.localizeCensus(allZeroIn, 0).keys()).sort(),
  all_zero_row_seoul: Array.from(lt.localizeCensus(allZeroIn, 540).keys()).sort(),
  offset_at_local_feb: lt.offsetAtLocal("2026-02-15", 13, nyOffset),
  offset_at_local_aug: lt.offsetAtLocal("2026-08-15", 14, nyOffset),
  offset_at_local_pinned: lt.offsetAtLocal("2026-02-15", 13, 330),
  // The instant side, for contrast: placement was already per-instant.
  offset_at_instant_feb: nyOffset(Date.parse("2026-02-15T18:02:00Z")),
  offset_at_instant_aug: nyOffset(Date.parse("2026-08-15T18:02:00Z"))
};

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
  page_payload: pagePayload,
  probe_identity: probeIdentity,
  autosave: ["check.html", "ko/check.html"].reduce(function (a, rel) {
    a[rel] = autosaveOf(rel);
    return a;
  }, {}),
  // M16: page-independent census vs the engine's, on the same files.
  census_move: {
    equal: censusEqual(census, engineCensus),
    days_page: census.size,
    days_engine: engineCensus.size,
    total_page: Array.from(census.values()).reduce(function (a, r) {
      return a + r.reduce(function (x, y) { return x + y; }, 0);
    }, 0),
    total_engine: Array.from(engineCensus.values()).reduce(function (a, r) {
      return a + r.reduce(function (x, y) { return x + y; }, 0);
    }, 0)
  },
  // M15: what the page's payload block must derive from the engine's census.
  // Sorted key lists, no counts — the same shape /api/submit accepts.
  detector_vocab: {
    reasons: Object.keys((result.detector || {}).reasons || {}).sort(),
    versions: Object.keys((result.detector || {}).versions || {}).sort()
  },
  page_labels: pageLabels,
  unit: unit,
  labels: [0, 540, 330, 345, -300, 825, -210, 60].reduce(function (a, o) {
    a[String(o)] = lt.offsetLabel(o);
    return a;
  }, {})
}));
