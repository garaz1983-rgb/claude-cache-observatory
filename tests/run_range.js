#!/usr/bin/env node
/*
 * run_range.js — test helper for the submission range helpers in
 * assets/parse.js (filterRange / clampRange / daySpan).
 *
 * Usage: node run_range.js <fixture_dir> <job_json>
 *
 *   job_json = {
 *     "ranges": [[start, end], ...],              // -> filterRange()
 *     "clamps": [{"dates": [...], "maxDays": n}], // -> clampRange()
 *     "spans":  [[start, end], ...]               // -> daySpan()
 *   }
 *   start/end may be null (unbounded on that side).
 *
 * Prints one JSON object on stdout:
 *   { totals, daily, events: [{date, classification, iron}], slices, clamps, spans }
 *
 * The engine's raw daily/events are echoed so the checker can recompute the
 * expected slice on its own rather than trusting the same code twice.
 */
"use strict";

var fs = require("fs");
var path = require("path");
var engine = require(path.join(__dirname, "..", "assets", "parse.js"));

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

var root = process.argv[2];
var jobRaw = process.argv[3];
if (!root || !jobRaw) {
  process.stderr.write("usage: node run_range.js <fixture_dir> <job_json>\n");
  process.exit(2);
}
var job = JSON.parse(jobRaw);
var result = engine.parseFiles(collect(root, "", []));

var out = {
  totals: result.totals,
  daily: result.daily,
  events: result.events.map(function (e) {
    return { date: e.date, classification: e.classification, iron: e.iron === true };
  }),
  max_period_days: engine.MAX_PERIOD_DAYS,
  slices: (job.ranges || []).map(function (r) {
    return engine.filterRange(result, r[0], r[1]);
  }),
  clamps: (job.clamps || []).map(function (c) {
    return engine.clampRange(c.dates, c.maxDays);
  }),
  spans: (job.spans || []).map(function (s) {
    return engine.daySpan(s[0], s[1]);
  })
};
process.stdout.write(JSON.stringify(out));
