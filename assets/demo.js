/*
 * demo.js — SYNTHETIC EXAMPLE DATA for the landing-page preview.
 *
 * THIS IS NOT OBSERVATION DATA. Nothing in this file comes from a real
 * session log, a real submission, or the maintainer's own machine. Every
 * number is produced by the seeded generator below, so the page renders the
 * same example on every load and in every browser.
 *
 * Why it *has* to be synthetic: the submission schema carries daily
 * aggregates only. It has no hour-level field at all, on purpose — hour-level
 * data would expose when a person works. So no real submission can back an
 * hourly heatmap, and none is used here. The preview exists to show the shape
 * of a diagnosis, not to make a claim about anybody's account.
 *
 * The shape is tuned to what actually gets observed: loss rates well under
 * 1%, most days completely clean, and losses arriving in short bursts inside
 * one or two hours rather than spread evenly.
 *
 * Language-agnostic: no display strings live here (dates and HH:MM:SS only).
 * Both index.html and ko/index.html read the same structure.
 *
 * Browser global: window.ObservatoryDemo
 *   .days   [{ date, requests, losses }]         — 14 days, sorted
 *   .census Map(date -> [24 request counts])
 *   .events Map("date#hour" -> [{ time, gapMin, tokens, sub }])
 *   .totals { requests, losses, iron, tokens, rate }
 *   .SYNTHETIC = true                            — never rename this away
 */
(function (root) {
  "use strict";

  // Deterministic PRNG (mulberry32). A fixed seed is the point: the example
  // must not drift between visitors or between reloads.
  function rng32(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  var SEED = 20260815;
  var DAYS = 14;
  var START = "2026-08-05";       // example window, not an observation window

  // Relative request weight per hour of the day: quiet overnight, two working
  // humps, a long evening tail. Shape only — scaled per day below.
  var HOUR_WEIGHT = [
    2, 1, 0, 0, 0, 0, 0, 1,
    4, 11, 15, 16, 9, 6, 13, 16,
    15, 12, 9, 7, 12, 14, 10, 5
  ];

  function addDays(iso, n) {
    var d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  function build() {
    var rnd = rng32(SEED);
    var dates = [];
    for (var i = 0; i < DAYS; i++) dates.push(addDays(START, i));

    // ---- hourly request census -------------------------------------------
    var census = new Map();
    dates.forEach(function (date) {
      var dow = new Date(date + "T00:00:00Z").getUTCDay();
      var weekend = (dow === 0 || dow === 6);
      var intensity = (weekend ? 0.35 : 0.8) + rnd() * (weekend ? 0.30 : 0.55);
      var hours = HOUR_WEIGHT.map(function (w) {
        if (w === 0) return rnd() < 0.15 ? 1 : 0;
        return Math.max(1, Math.round(w * intensity * 5 * (0.65 + rnd() * 0.7)));
      });
      census.set(date, hours);
    });

    // ---- loss bursts ------------------------------------------------------
    // Only a minority of days are affected, and on those days the events sit
    // inside one or two busy hours. That clustering is the observed
    // signature; evenly sprinkled losses would look like ordinary expiry.
    var affected = {};
    [0, 4, 5, 9, 12].forEach(function (idx) { affected[idx] = true; });

    var events = new Map();
    dates.forEach(function (date, di) {
      if (!affected[di]) return;
      var hours = census.get(date);
      // Candidate hours: busy ones only.
      var busy = [];
      for (var h = 0; h < 24; h++) if (hours[h] >= 30) busy.push(h);
      if (!busy.length) return;

      var bursts = 1 + (rnd() < 0.55 ? 1 : 0);
      var used = {};
      for (var b = 0; b < bursts; b++) {
        var hour = busy[Math.floor(rnd() * busy.length)];
        if (used[hour]) continue;
        used[hour] = true;

        var count = 1 + Math.floor(rnd() * 5);           // 1..5 in one hour
        count = Math.min(count, Math.max(1, Math.floor(hours[hour] / 12)));
        var list = [];
        for (var k = 0; k < count; k++) {
          var mi = Math.floor(rnd() * 60);
          var se = Math.floor(rnd() * 60);
          var iron = rnd() < 0.45;
          var gapMin = iron ? 0.2 + rnd() * 4.5 : 5.2 + rnd() * 23;
          list.push({
            time: pad2(hour) + ":" + pad2(mi) + ":" + pad2(se),
            gapMin: gapMin,
            tokens: Math.round((12000 + rnd() * 94000) / 100) * 100,
            sub: rnd() < 0.35
          });
        }
        list.sort(function (x, y) { return x.time < y.time ? -1 : 1; });
        events.set(date + "#" + hour, list);
      }
    });

    // ---- roll-ups ---------------------------------------------------------
    var days = dates.map(function (date) {
      var hours = census.get(date);
      var requests = hours.reduce(function (a, v) { return a + v; }, 0);
      var losses = 0;
      for (var h = 0; h < 24; h++) {
        var l = events.get(date + "#" + h);
        if (l) losses += l.length;
      }
      return { date: date, requests: requests, losses: losses };
    });

    var totRequests = 0, totLosses = 0, totIron = 0, totTokens = 0;
    days.forEach(function (d) { totRequests += d.requests; totLosses += d.losses; });
    events.forEach(function (list) {
      list.forEach(function (e) {
        totTokens += e.tokens;
        if (e.gapMin < 5) totIron += 1;
      });
    });

    return {
      SYNTHETIC: true,
      days: days,
      census: census,
      events: events,
      totals: {
        requests: totRequests,
        losses: totLosses,
        iron: totIron,
        tokens: totTokens,
        rate: totRequests > 0 ? 100 * totLosses / totRequests : 0
      }
    };
  }

  root.ObservatoryDemo = build();
})(typeof self !== "undefined" ? self : this);
