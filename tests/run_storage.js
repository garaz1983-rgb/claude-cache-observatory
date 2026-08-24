#!/usr/bin/env node
/*
 * run_storage.js — test helper for assets/store.js (M10 local save).
 *
 * Usage: node run_storage.js <fixture_dir> <job_json>
 *
 *   job_json = {
 *     "increments": [{"dates": [...], "submissions": [...], "today": "...", "maxDays": n}],
 *     "overlaps":   [{"start": "...", "end": "...", "submissions": [...]}],
 *     "nextDays":   ["2026-02-28", ...]
 *   }
 *
 * There is no browser here, so localStorage is faked three ways:
 *   ok      — a working Map-backed store
 *   throwing— every accessor throws (private window / blocked site data)
 *   quota   — setItem throws only for large payloads (quota exhausted)
 *
 * Prints one JSON object on stdout with:
 *   run             the record buildRun() produces from the real fixtures
 *   run_json        that record serialised, so the checker can grep it for
 *                   forbidden substrings taken from the fixture files
 *   raw_events      a sample of the ENGINE's own event records (the ones that
 *                   do carry file paths and requestIds), for the same grep
 *   roundtrip       what load() returns after save() of that record
 *   throwing/quota  behaviour under the hostile storages
 *   increments/overlaps/next_days  pure date arithmetic results
 */
"use strict";

var fs = require("fs");
var path = require("path");
var nodeCrypto = require("crypto");
var engine = require(path.join(__dirname, "..", "assets", "parse.js"));
var store = require(path.join(__dirname, "..", "assets", "store.js"));
var identity = require(path.join(__dirname, "..", "assets", "identity.js"));

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

/* ---- fake storages ---- */
function okStorage() {
  var m = new Map();
  return {
    getItem: function (k) { return m.has(k) ? m.get(k) : null; },
    setItem: function (k, v) { m.set(k, String(v)); },
    removeItem: function (k) { m.delete(k); },
    _size: function () { return m.size; }
  };
}
function throwingStorage() {
  return {
    getItem: function () { throw new Error("SecurityError: storage is blocked"); },
    setItem: function () { throw new Error("SecurityError: storage is blocked"); },
    removeItem: function () { throw new Error("SecurityError: storage is blocked"); }
  };
}
// Rejects anything above `limit` bytes, like a full origin quota.
function quotaStorage(limit) {
  var m = new Map();
  return {
    getItem: function (k) { return m.has(k) ? m.get(k) : null; },
    setItem: function (k, v) {
      if (String(v).length > limit) {
        var err = new Error("QuotaExceededError");
        err.name = "QuotaExceededError";
        throw err;
      }
      m.set(k, String(v));
    },
    removeItem: function (k) { m.delete(k); }
  };
}

/* ---- hourly census, mirroring what check.html hands to buildRun ---- */
function censusFromDaily(daily) {
  return daily.map(function (d, i) {
    var usage = new Array(24).fill(0);
    usage[(i * 3) % 24] = d.requests;
    return { date: d.date, usage: usage };
  });
}

var root = process.argv[2];
var jobRaw = process.argv[3];
if (!root || !jobRaw) {
  process.stderr.write("usage: node run_storage.js <fixture_dir> <job_json>\n");
  process.exit(2);
}
var job = JSON.parse(jobRaw);
var files = collect(root, "", []);
var result = engine.parseFiles(files);
var census = censusFromDaily(result.daily);

/* M13: the fingerprint the check page hands to buildRun. Hashed here with the
   same prefix assets/identity.js uses, synchronously, so the storage runner
   stays a plain script — identity.js's own async path is exercised by
   tests/identity_test.py. What matters here is that the DIGESTS reach storage
   and the requestIds they came from do not, which the checker greps for. */
var sampledIds = identity.sample(identity.collect(files));
var anchors = sampledIds.map(function (id) {
  return nodeCrypto.createHash("sha256")
    .update(identity.ANCHOR_PREFIX + id, "utf8").digest("hex");
});

var run = store.buildRun(result, census, {
  saved_at: "2026-08-24T09:00:00.000Z",
  source: "folder",
  script_version: engine.SCRIPT_VERSION,
  anchors: anchors
});

/* round trip through a working store */
var s1 = okStorage();
var savedOk = store.save(store.addRun(store.emptyState(), run), s1);
var reloaded = store.load(s1);

/* a second run + a submission, for the comparison and the overlap default */
var run2 = store.buildRun(result, census, {
  saved_at: "2026-08-25T09:00:00.000Z",
  source: "folder",
  script_version: engine.SCRIPT_VERSION,
  anchors: anchors
});
var state2 = store.addSubmission(store.addRun(reloaded, run2), {
  period_start: run.period_start,
  period_end: run.period_end,
  submitted_at: "2026-08-25T09:05:00.000Z",
  id: "sub-20260825-abcd",
  // Fields the store must refuse to carry, smuggled in on purpose.
  nickname: "someone",
  file: "/home/me/.claude/projects/secret/session.jsonl"
});
store.save(state2, s1);
var reloaded2 = store.load(s1);

/* hostile storage: nothing may throw out of store.js */
var st = throwingStorage();
var throwing = {};
try {
  throwing = {
    available: store.available(st),
    load: store.load(st),
    save: store.save(store.addRun(store.emptyState(), run), st),
    clear: store.clear(st),
    threw: false
  };
} catch (e) {
  throwing = { threw: true, message: String(e && e.message) };
}

/* quota: the save has to degrade rather than fail outright. The limit is set
   one byte under the full record so the first attempt is guaranteed to be
   rejected whatever the fixtures happen to weigh. */
var fullLen = JSON.stringify(store.addRun(store.emptyState(), run)).length;
var qs = quotaStorage(fullLen - 1);
var quotaSaved = store.save(store.addRun(store.emptyState(), run), qs);
var quotaBack = store.load(qs);

/* a quota nothing fits into must report failure, not pretend to have saved */
var tiny = quotaStorage(8);
var tinySaved = store.save(store.addRun(store.emptyState(), run), tiny);
var tinyBack = store.load(tiny);

/* M13 link token: the one value the page stores without being asked. It has to
   survive addRun/addSubmission, refuse anything that is not a 32-hex string,
   and disappear with clear() like everything else in this key. */
var s4 = okStorage();
var linkState = store.withLinkToken(store.emptyState(), "0123456789abcdef0123456789abcdef");
store.save(store.addSubmission(store.addRun(linkState, run), {
  period_start: run.period_start, period_end: run.period_end,
  submitted_at: "2026-08-25T09:05:00.000Z", id: "sub-20260825-abcd"
}), s4);
var linkBack = store.load(s4);
var linkCleared = (store.clear(s4), store.load(s4));
var link = {
  token: store.linkTokenOf(linkBack),
  survives_runs: !!(linkBack && linkBack.runs.length === 1),
  survives_submissions: !!(linkBack && linkBack.submissions.length === 1),
  rejects_short: store.linkTokenOf(store.withLinkToken(store.emptyState(), "abc")),
  rejects_upper: store.linkTokenOf(
    store.withLinkToken(store.emptyState(), "0123456789ABCDEF0123456789ABCDEF")),
  rejects_nonstring: store.linkTokenOf(store.withLinkToken(store.emptyState(), 12345)),
  cleared: linkCleared === null,
  // Anchors the store refuses: wrong length, uppercase, non-string, over the cap.
  anchor_filter: store.sanitizeAnchors(
    [anchors[0], "zz", anchors[0].toUpperCase(), 7, anchors[1]])
};

/* clearing must actually empty the store */
var s3 = okStorage();
store.save(store.addRun(store.emptyState(), run), s3);
var beforeClear = store.load(s3) !== null;
var cleared = store.clear(s3);
var afterClear = store.load(s3);

var out = {
  script_version: engine.SCRIPT_VERSION,
  totals: result.totals,
  daily: result.daily,
  raw_events: result.events.slice(0, 40),
  run: run,
  run_json: JSON.stringify(run),
  saved_ok: savedOk,
  roundtrip: reloaded,
  state2_json: JSON.stringify(reloaded2),
  state2_submissions: reloaded2 ? reloaded2.submissions : null,
  compare: reloaded2 && reloaded2.runs.length >= 2
    ? store.compareRuns(reloaded2.runs[reloaded2.runs.length - 2],
                        reloaded2.runs[reloaded2.runs.length - 1])
    : null,
  hydrated_iron: (function () {
    var h = store.hydrate(run);
    var n = 0;
    h.result.events.forEach(function (e) { if (e.classification === "iron") n += 1; });
    return { iron_events: n, daily_len: h.result.daily.length, census: h.census !== null };
  })(),
  throwing: throwing,
  quota: { saved: quotaSaved, events_saved: quotaBack && quotaBack.runs[0]
    ? quotaBack.runs[0].events_saved : null,
    has_daily: !!(quotaBack && quotaBack.runs[0] && quotaBack.runs[0].daily.length),
    tiny_saved: tinySaved, tiny_back: tinyBack },
  clear: { before: beforeClear, returned: cleared, after: afterClear },
  anchors: anchors,
  sampled_ids: sampledIds,
  link: link,
  increments: (job.increments || []).map(function (c) {
    return store.incrementalRange(c.dates, c.submissions, c.today, c.maxDays);
  }),
  overlaps: (job.overlaps || []).map(function (c) {
    return store.overlapWith(c.start, c.end, c.submissions);
  }),
  next_days: (job.nextDays || []).map(function (d) { return store.nextDay(d); })
};
process.stdout.write(JSON.stringify(out));
