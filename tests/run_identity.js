#!/usr/bin/env node
/*
 * run_identity.js — test helper for assets/identity.js (M13 machine fingerprint).
 *
 * Usage: node run_identity.js <fixture_dir>
 *
 * The fingerprint's whole job is to answer one question — "is this the same
 * machine as last time?" — from nothing but the logs. That question only has
 * meaning across two DIFFERENT scans of a log folder that has moved on since,
 * so the interesting inputs are not a fixture but a pair of synthetic log sets
 * whose relationship is controlled exactly:
 *
 *   base     500 records, the reference scan
 *   append   the same 500 plus 100 newer ones (a normal week of use)
 *   trimmed  the same 500 minus its 3 oldest (a small cleanup)
 *   churned  the oldest 150 deleted AND 150 new appended (a real rotation)
 *   other    500 records from a different machine entirely
 *   shuffled the base scan, files handed over in a different order
 *
 * Everything here is generated from a fixed seed, so a run on any machine at
 * any time samples the same records and produces the same digests.
 *
 * The real fixture directory is scanned too, to prove collect() reads the same
 * request ids the engine dedupes on rather than a private notion of one.
 *
 * Prints one JSON object on stdout. Judgement lives in tests/identity_test.py.
 */
"use strict";

var fs = require("fs");
var path = require("path");
var nodeCrypto = require("crypto");
var identity = require(path.join(__dirname, "..", "assets", "identity.js"));
var engine = require(path.join(__dirname, "..", "assets", "parse.js"));

/* ---- deterministic pseudo-random source (mulberry32) ----
   Seeded, so "the same logs sample the same records" is reproducible and a
   failure is reproducible with it. */
function rng(seed) {
  var a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    var t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function b36(n, len) {
  var s = n.toString(36);
  while (s.length < len) s = "0" + s;
  return s;
}

/* One synthetic record per line, in the shape Claude Code writes: a requestId,
   a timestamp and a usage block (the prefilter identity.js shares with the
   engine). `tag` keeps two machines' ids disjoint by construction. */
function makeRecords(tag, count, startMs, stepMs, seed) {
  var r = rng(seed);
  var out = [];
  for (var i = 0; i < count; i++) {
    var rid = "req_" + tag + "_" + b36(i, 4) + b36(Math.floor(r() * 1e9), 6);
    var ts = new Date(startMs + i * stepMs).toISOString();
    out.push({
      rid: rid,
      ts: ts,
      line: JSON.stringify({
        timestamp: ts,
        requestId: rid,
        type: "assistant",
        message: { id: "msg_" + b36(i, 6), usage: { input_tokens: 10, cache_creation_input_tokens: 0 } }
      })
    });
  }
  return out;
}

/* Split records across several files, so file order is a variable the sample
   has to be independent of. */
function filesOf(records, fileCount, name) {
  var buckets = [];
  for (var i = 0; i < fileCount; i++) buckets.push([]);
  for (var j = 0; j < records.length; j++) buckets[j % fileCount].push(records[j].line);
  return buckets.map(function (lines, i) {
    return { name: name + "-" + i + ".jsonl", text: lines.join("\n") + "\n" };
  });
}

function anchorsSync(ids) {
  return ids.map(function (id) {
    return nodeCrypto.createHash("sha256")
      .update(identity.ANCHOR_PREFIX + id, "utf8").digest("hex");
  });
}

function fingerprintOf(files) {
  var ids = identity.collect(files);
  var picked = identity.sample(ids);
  return { count: ids.length, sampled: picked, anchors: anchorsSync(picked) };
}

function overlap(a, b) {
  var set = new Set(a);
  var n = 0;
  b.forEach(function (x) { if (set.has(x)) n += 1; });
  return n;
}

/* ---- the scans ---- */
var DAY = 86400000;
var T0 = Date.parse("2026-05-01T00:00:00.000Z");
var STEP = 3 * 3600000;                       // one record every 3 hours

var base = makeRecords("A", 500, T0, STEP, 12345);
var extra = makeRecords("A2", 100, T0 + 500 * STEP, STEP, 999);
var otherRecs = makeRecords("B", 500, T0, STEP, 777);

var scans = {
  base: filesOf(base, 5, "base"),
  append: filesOf(base.concat(extra), 5, "append"),
  trimmed: filesOf(base.slice(3), 5, "trimmed"),
  churned: filesOf(base.slice(150).concat(makeRecords("A3", 150, T0 + 500 * STEP, STEP, 4242)),
                   5, "churned"),
  other: filesOf(otherRecs, 5, "other")
};
// The same scan, files handed over in a different order and split differently.
var shuffledFiles = filesOf(base, 7, "shuf").slice().reverse();

var fp = {};
Object.keys(scans).forEach(function (k) { fp[k] = fingerprintOf(scans[k]); });
var fpShuffled = fingerprintOf(shuffledFiles);

/* ---- the real fixtures: collect() must see the engine's own request set ---- */
function collectDir(dir, prefix, out) {
  var entries = fs.readdirSync(dir, { withFileTypes: true }).sort(function (a, b) {
    return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0);
  });
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    var abs = path.join(dir, e.name);
    var rel = prefix ? prefix + "/" + e.name : e.name;
    if (e.isDirectory()) collectDir(abs, rel, out);
    else if (e.isFile() && e.name.slice(-6) === ".jsonl") {
      out.push({ name: rel, text: fs.readFileSync(abs, "utf8") });
    }
  }
  return out;
}

var root = process.argv[2];
var fixtureFiles = root ? collectDir(root, "", []) : [];
var fixtureResult = fixtureFiles.length ? engine.parseFiles(fixtureFiles) : null;
var fixtureIds = identity.collect(fixtureFiles);

/* ---- edge inputs ---- */
var tiny = makeRecords("T", 5, T0, STEP, 5);
var noTimestamp = [{ name: "x.jsonl", text: JSON.stringify(
  { requestId: "req_no_ts", message: { usage: {} } }) + "\n" }];
var msgIdOnly = [{ name: "y.jsonl", text: JSON.stringify(
  { timestamp: "2026-05-01T00:00:00.000Z",
    message: { id: "msg_fallback_1", usage: {} } }) + "\n" }];
var noUsage = [{ name: "z.jsonl", text: JSON.stringify(
  { timestamp: "2026-05-01T00:00:00.000Z", requestId: "req_no_usage" }) + "\n" }];
// Offsets: ordering is by instant, so a +09:00 stamp sorts before a later Z one.
var offsetMix = [{ name: "o.jsonl", text: [
  JSON.stringify({ timestamp: "2026-05-01T12:00:00+09:00", requestId: "req_kst",
                   message: { usage: {} } }),
  JSON.stringify({ timestamp: "2026-05-01T04:00:00Z", requestId: "req_utc",
                   message: { usage: {} } })
].join("\n") + "\n" }];

/* The module's OWN async path, end to end. Everything above hashes with node's
   crypto directly so this script can stay synchronous; if only that were
   checked, a change to identity.js's hashing (a dropped prefix, a different
   digest) would sail through every assertion. So fingerprint() is run for real
   and its output is compared against the independently computed anchors. */
Promise.all([
  identity.fingerprint(scans.base),
  identity.anchorsOf(["req_pinned_example"])
]).then(function (res) {
  emit(res[0], res[1]);
}, function (err) {
  process.stderr.write("fingerprint() rejected: " + String(err && err.message) + "\n");
  process.exit(2);
});

function emit(moduleFp, modulePinned) {
process.stdout.write(JSON.stringify({
  module_fingerprint: { count: moduleFp.count, sampled: moduleFp.sampled,
                        anchors: moduleFp.anchors },
  module_pinned: modulePinned[0],
  anchor_prefix: identity.ANCHOR_PREFIX,
  anchor_count: identity.ANCHOR_COUNT,
  head_count: identity.HEAD_COUNT,
  base_ids: base.map(function (r) { return r.rid; }),
  scans: {
    base: fp.base,
    append: fp.append,
    trimmed: fp.trimmed,
    churned: fp.churned,
    other: fp.other,
    shuffled: fpShuffled
  },
  overlaps: {
    append: overlap(fp.base.anchors, fp.append.anchors),
    trimmed: overlap(fp.base.anchors, fp.trimmed.anchors),
    churned: overlap(fp.base.anchors, fp.churned.anchors),
    other: overlap(fp.base.anchors, fp.other.anchors),
    shuffled: overlap(fp.base.anchors, fpShuffled.anchors)
  },
  // A pinned digest: the prefix and the hash function are part of the contract
  // between this module and functions/api/submit.js.
  pinned: {
    id: "req_pinned_example",
    digest: anchorsSync(["req_pinned_example"])[0]
  },
  edges: {
    tiny_ids: identity.collect(filesOf(tiny, 2, "tiny")).length,
    tiny_sampled: identity.sample(identity.collect(filesOf(tiny, 2, "tiny"))).length,
    no_timestamp: identity.collect(noTimestamp),
    msg_id_only: identity.collect(msgIdOnly),
    no_usage: identity.collect(noUsage),
    offset_order: identity.collect(offsetMix),
    empty: identity.collect([]),
    junk: identity.collect([{ name: "j.jsonl", text: "not json at all\n{\n" }]),
    sanitize: identity.sanitizeAnchors(
      [fp.base.anchors[0], "zz", fp.base.anchors[0].toUpperCase(), 3, fp.base.anchors[1]])
  },
  fixtures: {
    ids: fixtureIds,
    engine_requests: fixtureResult ? fixtureResult.totals.requests : null
  }
}));
}
