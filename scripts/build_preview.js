/*
 * build_preview.js — turns one machine's real logs into assets/preview.js.
 *
 * The landing page used to preview a diagnosis with a seeded generator
 * (assets/demo.js). It was honest, and it read as a toy: fourteen invented
 * days cannot carry the one thing the real data says, which is that losses
 * do not arrive evenly. This replaces it with a measurement.
 *
 * WHAT IS PUBLISHED, AND WHAT CANNOT BE
 * -------------------------------------
 * The engine's own event records carry `file` and `requestId`. Neither may
 * ever reach a public file:
 *
 *   - requestId is M13's identity anchor. Publishing one hands a stranger the
 *     ability to claim this machine's row.
 *   - `file` is a log path, which names the projects on a person's disk.
 *
 * Two things keep them out. First, this generator emits only from
 * localtime.js's localized events, whose fields are already `{time, gapMin,
 * tokens, sub}` — a shape built by picking, not by deleting. Second, the
 * generated text is scanned before it is written, and a hit aborts the build
 * rather than warning: a guard that writes the file anyway is not a guard.
 *
 * Hour-level data IS being published here, and the submission schema
 * deliberately has no hour field for exactly the reason that makes that worth
 * saying out loud — hours show when a person works. That asymmetry is the
 * point and the page states it: the maintainer publishes their own hours by
 * choice; a submitter's hours are never collected in the first place.
 *
 * The published asset is generated over the FULL scan (--days above the
 * history length), so its window and totals line up with the machine's fleet
 * row — two different numbers for the same machine on one page read as an
 * error, whichever one is right. Any remaining gap is snapshot drift: losses
 * that happen after generation join the fleet row at the next submission.
 *
 * Usage:
 *   node scripts/build_preview.js <logRoot> [--days 92] [--out assets/preview.js]
 *
 * Deterministic given the same logs: no clock, no randomness. Re-running it
 * on an unchanged folder rewrites the same bytes.
 */
"use strict";

var fs = require("fs");
var path = require("path");

var SITE = path.dirname(__dirname);
var engine = require(path.join(SITE, "assets", "parse.js"));
var lt = require(path.join(SITE, "assets", "localtime.js"));

function arg(name, dflt) {
  var i = process.argv.indexOf(name);
  if (i !== -1 && i + 1 < process.argv.length) return process.argv[i + 1];
  return dflt;
}

var LOG_ROOT = process.argv[2];
if (!LOG_ROOT || LOG_ROOT.charAt(0) === "-") {
  console.error("usage: node scripts/build_preview.js <logRoot> [--days N] [--out PATH]");
  process.exit(2);
}
var DAYS = parseInt(arg("--days", "92"), 10);
var OUT = path.resolve(SITE, arg("--out", path.join("assets", "preview.js")));

/* ---------------- scan ---------------- */
function walk(dir, out) {
  var entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
  catch (e) { return out; }                    // unreadable dir: skipped, counted below
  entries.sort(function (a, b) { return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0); });
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    var p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (e.name.endsWith(".jsonl")) out.push(p);
  }
  return out;
}

var files = walk(LOG_ROOT, []);
process.stderr.write("scanning " + files.length + " file(s)\n");

var scan = engine.createScan({ census: true });
var unreadable = 0;
for (var i = 0; i < files.length; i++) {
  var text;
  try { text = fs.readFileSync(files[i], "utf8"); }
  catch (e) { unreadable += 1; continue; }
  // The name matters: the engine reads `/subagents/` out of it to pick the
  // 5-minute TTL. It never reaches the output.
  scan.addFile(path.relative(LOG_ROOT, files[i]).split(path.sep).join("/"), text);
}
if (unreadable) process.stderr.write("unreadable: " + unreadable + " file(s)\n");

var result = scan.finish();
if (!result.daily || !result.daily.length) {
  console.error("FATAL: the scan produced no dated rows");
  process.exit(1);
}

/* ---------------- localize, THEN cut the window ---------------- */
/* The same call the pages make, on the same clock the maintainer's own check
   page would use, so the preview is literally what this machine's diagnosis
   looks like rather than a re-derivation of it.
   Order matters, and engine.filterRange() is deliberately not used here: it
   returns a submission-shaped aggregate, which carries neither the census nor
   the events this preview is made of. The window is cut AFTER localizing,
   because the rows being cut are local days — cutting UTC days first would
   shave a partial day off whichever edge the offset falls on. */
var view = lt.localize(result, result.census, lt.hostOffsetAt);
if (!view.localized) { console.error("FATAL: localize() refused the scan"); process.exit(1); }

var window_ = engine.clampRange(view.daily.map(function (d) { return d.date; }), DAYS);
if (!window_) { console.error("FATAL: could not clamp to " + DAYS + " days"); process.exit(1); }
function inWindow(date) { return date >= window_.start && date <= window_.end; }

/* ---------------- shape, by allowlist ---------------- */
var EVENT_FIELDS = ["time", "gapMin", "tokens", "sub"];

var days = view.daily.filter(function (d) { return inWindow(d.date); }).map(function (d) {
  return { date: d.date, requests: d.requests | 0, losses: d.losses | 0 };
});

var censusPairs = [];
view.census.forEach(function (hours, date) {
  if (!inWindow(date)) return;
  censusPairs.push([date, hours.map(function (n) { return n | 0; })]);
});
censusPairs.sort(function (a, b) { return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0); });

var eventPairs = [];
view.evtMap.forEach(function (list, key) {
  if (!inWindow(key.split("#")[0])) return;
  eventPairs.push([key, list.map(function (e) {
    var picked = {};
    for (var j = 0; j < EVENT_FIELDS.length; j++) {
      var f = EVENT_FIELDS[j];
      picked[f] = f === "sub" ? !!e[f] : (f === "time" ? String(e[f]) : e[f]);
    }
    return picked;
  })]);
});
eventPairs.sort(function (a, b) { return a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0); });

var totRequests = 0, totLosses = 0, totIron = 0, totTokens = 0;
days.forEach(function (d) { totRequests += d.requests; totLosses += d.losses; });
eventPairs.forEach(function (p) {
  p[1].forEach(function (e) {
    totTokens += e.tokens | 0;
    if (e.gapMin < 5) totIron += 1;
  });
});

/* The period is the period OF THE ROWS EMITTED, not of the scan. Reporting the
   scan's span next to the window's totals is the one inconsistency a reader
   would be right to call a lie. */
var meta = {
  period: { start: days[0].date, end: days[days.length - 1].date },
  days: days.length,
  offsets: view.offsets,
  offsetLabel: lt.offsetLabel(view.offsets.length ? view.offsets[0] : 0)
};

/* ---------------- emit ---------------- */
function num(n) {
  // Round the one float so the file does not carry 13 meaningless digits.
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}
function evtJs(e) {
  return "{time:" + JSON.stringify(e.time) + ",gapMin:" + num(e.gapMin) +
         ",tokens:" + num(e.tokens | 0) + ",sub:" + (e.sub ? "1" : "0") + "}";
}

var lines = [];
lines.push("/*");
lines.push(" * preview.js — MEASURED data for the landing-page preview.");
lines.push(" *");
lines.push(" * Generated by scripts/build_preview.js from one real machine's session");
lines.push(" * logs: the maintainer's own. It is NOT a submission, NOT the fleet, and");
lines.push(" * NOT anyone else's data. Regenerate with:");
lines.push(" *");
lines.push(" *     node scripts/build_preview.js <logRoot>");
lines.push(" *");
lines.push(" * What is deliberately absent: requestId and log file paths. The engine");
lines.push(" * records both per event and neither is emitted here — this file is built");
lines.push(" * from localtime.js's localized events, whose fields are picked rather than");
lines.push(" * deleted, and the generator refuses to write a file that matches either.");
lines.push(" * A published requestId would let a stranger claim this machine's row.");
lines.push(" *");
lines.push(" * Hours ARE published here. The submission schema has no hour field on");
lines.push(" * purpose, because hours show when a person works. The maintainer publishes");
lines.push(" * their own by choice; nobody else's are ever collected.");
lines.push(" *");
lines.push(" * Window: " + meta.period.start + " .. " + meta.period.end +
           " (" + meta.days + " days, " + meta.offsetLabel + ").");
lines.push(" * Totals below are the totals OF THESE ROWS: nothing is shown that the");
lines.push(" * numbers do not add up to.");
lines.push(" *");
lines.push(" * Browser global: window.ObservatoryPreview");
lines.push(" *   .days   [{ date, requests, losses }]");
lines.push(" *   .census Map(date -> [24 request counts])");
lines.push(" *   .events Map(\"date#hour\" -> [{ time, gapMin, tokens, sub }])");
lines.push(" *   .totals { requests, losses, iron, tokens, rate }");
lines.push(" *   .MEASURED = true   .SYNTHETIC = false");
lines.push(" */");
lines.push("(function (root) {");
lines.push("  \"use strict\";");
lines.push("");
lines.push("  var DAYS = [");
days.forEach(function (d, i) {
  lines.push("    {date:" + JSON.stringify(d.date) + ",requests:" + d.requests +
             ",losses:" + d.losses + "}" + (i === days.length - 1 ? "" : ","));
});
lines.push("  ];");
lines.push("");
lines.push("  var CENSUS = [");
censusPairs.forEach(function (p, i) {
  lines.push("    [" + JSON.stringify(p[0]) + ",[" + p[1].join(",") + "]]" +
             (i === censusPairs.length - 1 ? "" : ","));
});
lines.push("  ];");
lines.push("");
lines.push("  var EVENTS = [");
eventPairs.forEach(function (p, i) {
  lines.push("    [" + JSON.stringify(p[0]) + ",[" +
             p[1].map(evtJs).join(",") + "]]" +
             (i === eventPairs.length - 1 ? "" : ","));
});
lines.push("  ];");
lines.push("");
lines.push("  var census = new Map();");
lines.push("  for (var i = 0; i < CENSUS.length; i++) census.set(CENSUS[i][0], CENSUS[i][1]);");
lines.push("  var events = new Map();");
lines.push("  for (var j = 0; j < EVENTS.length; j++) {");
lines.push("    events.set(EVENTS[j][0], EVENTS[j][1].map(function (e) {");
lines.push("      return { time: e.time, gapMin: e.gapMin, tokens: e.tokens, sub: !!e.sub };");
lines.push("    }));");
lines.push("  }");
lines.push("");
lines.push("  root.ObservatoryPreview = {");
lines.push("    MEASURED: true,");
lines.push("    SYNTHETIC: false,");
lines.push("    period: " + JSON.stringify(meta.period) + ",");
lines.push("    offsetLabel: " + JSON.stringify(meta.offsetLabel) + ",");
lines.push("    days: DAYS,");
lines.push("    census: census,");
lines.push("    events: events,");
lines.push("    totals: {");
lines.push("      requests: " + totRequests + ",");
lines.push("      losses: " + totLosses + ",");
lines.push("      iron: " + totIron + ",");
lines.push("      tokens: " + totTokens + ",");
lines.push("      rate: " + (totRequests > 0 ? num(100 * totLosses / totRequests) : "0"));
lines.push("    }");
lines.push("  };");
lines.push("})(typeof self !== \"undefined\" ? self : this);");

var text = lines.join("\n") + "\n";

/* ---------------- the guard ---------------- */
/* Aborts. A generator that warns and writes anyway has published the thing it
   warned about. */
var FORBIDDEN = [
  [/req_[A-Za-z0-9]{6,}/, "a requestId"],
  [/toolu_[A-Za-z0-9]{6,}/, "a tool-use id"],
  [/msg_[A-Za-z0-9]{6,}/, "a message id"],
  [/\.jsonl/, "a log file path"],
  [/[A-Za-z]:[\\/]+Users[\\/]+/, "a Windows user path"],
  [/\/(?:home|Users)\/[A-Za-z0-9._-]+\//, "a POSIX home path"],
  [/c--[A-Za-z0-9-]+/, "a Claude Code project key"],
  [/\b[0-9a-f]{32,}\b/, "a long hex digest"],
  [/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/, "a session UUID"]
];
var violations = [];
FORBIDDEN.forEach(function (rule) {
  var m = rule[0].exec(text);
  if (m) violations.push(rule[1] + " (" + JSON.stringify(m[0].slice(0, 48)) + ")");
});
if (violations.length) {
  console.error("REFUSED to write " + path.relative(SITE, OUT) + " — it contains:");
  violations.forEach(function (v) { console.error("  - " + v); });
  process.exit(1);
}

fs.writeFileSync(OUT, text, "utf8");

process.stderr.write("\n");
process.stdout.write("wrote " + path.relative(SITE, OUT) + " (" + text.length + " bytes)\n");
process.stdout.write("  window   " + meta.period.start + " .. " + meta.period.end +
                     "  (" + meta.days + " days, " + meta.offsetLabel + ")\n");
process.stdout.write("  requests " + totRequests + "\n");
process.stdout.write("  losses   " + totLosses + "  (iron " + totIron + ")\n");
process.stdout.write("  tokens   " + totTokens + "\n");
process.stdout.write("  rate     " + (totRequests > 0 ? (100 * totLosses / totRequests).toFixed(3) : "0") + "%\n");
process.stdout.write("  cells    " + censusPairs.length + " day rows, " +
                     eventPairs.length + " hours carrying a loss\n");
process.stdout.write("  guard    clean (" + FORBIDDEN.length + " patterns checked)\n");
