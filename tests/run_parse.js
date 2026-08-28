#!/usr/bin/env node
/*
 * run_parse.js — test helper: feed a directory of *.jsonl fixtures to
 * assets/parse.js and print the resulting JSON on stdout.
 *
 * Usage: node run_parse.js <fixture_dir>
 *
 * File names are passed as relative paths (forward slashes) so that a
 * "subagents" path segment is visible to the engine, mirroring how the
 * CLI script sees paths on disk.
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
if (!root) {
  process.stderr.write("usage: node run_parse.js <fixture_dir>\n");
  process.exit(2);
}
/* M19: driven through createScan with the census on, because that is the
   path the check page actually runs and the only one that emits wire_hourly.
   The census itself is a Map (not JSON-serialisable) and is display-side, so
   it is dropped before printing. */
var files = collect(root, "", []);
var scan = engine.createScan({ census: true });
for (var i = 0; i < files.length; i++) scan.addFile(files[i].name, files[i].text);
var result = scan.finish();
delete result.files;
delete result.census;
process.stdout.write(JSON.stringify(result));
