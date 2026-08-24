/*
 * charts.js — claude-cache-observatory canvas charts (Milestone 2).
 *
 * Ported from the design-SSOT prototypes (eviction_observatory_{ko,en}.html):
 * same tokens (CSS custom properties), same canvas set-up, same visual
 * grammar (crit bars, amber rate line, ok clean-day outline, usage-shaded
 * day x hour heatmap).
 *
 * Language-agnostic: every label/tooltip string is injected by the caller
 * through opts. No data, no fetch, no judgment logic — render only.
 *
 * Browser global: window.ObservatoryCharts
 *   renderFleetTrend(canvas, model, opts)   — per-submission daily loss-rate lines
 *   renderDailyBars(canvas, days, opts)     — daily loss bars + loss-rate line
 *   renderUsageHeatmap(tableEl, rows, opts) — usage-vs-losses day x hour heatmap
 *   PALETTE                                  — per-submission series colors
 */
(function (root) {
  "use strict";

  var PALETTE = [
    "#D64545", "#2E86C1", "#2E9E7B", "#B07A2A",
    "#7D5BA6", "#C2527F", "#5B8A72", "#4A7A9D"
  ];

  var MONO = "Consolas,monospace";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function setupCanvas(c) {
    var dpr = window.devicePixelRatio || 1;
    var w = c.clientWidth;
    var h = parseInt(c.getAttribute("height"), 10);
    c.width = w * dpr;
    c.height = h * dpr;
    c.style.height = h + "px";
    var ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  // Smallest "nice" axis maximum >= v (1/2/2.5/5 x 10^k), minimum floor.
  function niceMax(v, floor) {
    var target = Math.max(floor || 1, v);
    var mag = Math.pow(10, Math.floor(Math.log(target) / Math.LN10));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] * mag >= target) return steps[i] * mag;
    }
    return 10 * mag;
  }

  function labelStep(n, maxLabels) {
    return Math.max(1, Math.ceil(n / (maxLabels || 12)));
  }

  /*
   * Fleet daily loss-rate trend.
   * model = {
   *   dates : ["YYYY-MM-DD", ...]                     — sorted x axis
   *   series: [{ color, values: [rate%|null, ...] }]  — aligned to dates,
   *            null = the submission does not cover that date (gap in line)
   * }
   * opts = { dateFormat: fn(iso)->string, rateFormat: fn(number)->string }
   */
  function renderFleetTrend(canvas, model, opts) {
    opts = opts || {};
    var dateFormat = opts.dateFormat || function (d) { return d.slice(5); };
    var rateFormat = opts.rateFormat || function (v) {
      return (v % 1 === 0 ? v : v.toFixed(1)) + "%";
    };
    var box = setupCanvas(canvas);
    var ctx = box.ctx, w = box.w, h = box.h;
    var muted = cssVar("--muted"), grid = cssVar("--grid"), ink = cssVar("--ink");
    var padL = 44, padR = 14, padT = 14, padB = 26;
    var iw = w - padL - padR, ih = h - padT - padB;
    var dates = model.dates, series = model.series;
    var n = dates.length;

    var maxRate = 0;
    series.forEach(function (s) {
      s.values.forEach(function (v) {
        if (v !== null && v > maxRate) maxRate = v;
      });
    });
    var yMax = niceMax(maxRate * 1.15, 1);

    ctx.clearRect(0, 0, w, h);
    ctx.font = "10.5px " + MONO;
    var yDiv = 4;
    for (var g = 0; g <= yDiv; g++) {
      var v = yMax * g / yDiv;
      var y = padT + ih - ih * g / yDiv;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
      ctx.fillStyle = muted; ctx.textAlign = "right";
      ctx.fillText(rateFormat(v), padL - 6, y + 3.5);
    }

    function xAt(i) {
      return n === 1 ? padL + iw / 2 : padL + iw * i / (n - 1);
    }
    function yAt(v) {
      return padT + ih - ih * Math.min(v, yMax) / yMax;
    }

    var step = labelStep(n, 12);
    for (var i = 0; i < n; i++) {
      if (i % step !== 0 && i !== n - 1) continue;
      ctx.fillStyle = muted; ctx.textAlign = "center";
      ctx.fillText(dateFormat(dates[i]), xAt(i), h - 8);
    }

    series.forEach(function (s) {
      ctx.strokeStyle = s.color; ctx.lineWidth = 2;
      ctx.beginPath();
      var pen = false;
      for (var i = 0; i < n; i++) {
        var v = s.values[i];
        if (v === null || v === undefined) { pen = false; continue; }
        var x = xAt(i), y = yAt(v);
        if (pen) { ctx.lineTo(x, y); } else { ctx.moveTo(x, y); pen = true; }
      }
      ctx.stroke();
      ctx.fillStyle = s.color;
      for (var j = 0; j < n; j++) {
        var vv = s.values[j];
        if (vv === null || vv === undefined) continue;
        ctx.beginPath(); ctx.arc(xAt(j), yAt(vv), 2.6, 0, 7); ctx.fill();
      }
    });

    // Ink baseline for orientation, drawn last so dots sit on top of grid only.
    ctx.strokeStyle = ink; ctx.globalAlpha = 0.25; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, padT + ih); ctx.lineTo(padL + iw, padT + ih); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  /*
   * Daily bars: losses (left axis, crit bars; ok outline on clean days)
   * plus loss-rate % line (right axis, amber) — the prototype's daily chart.
   * days = [{ label, count, pct }]
   * opts = { pctFormat: fn(number)->string }
   */
  function renderDailyBars(canvas, days, opts) {
    opts = opts || {};
    var box = setupCanvas(canvas);
    var ctx = box.ctx, w = box.w, h = box.h;
    var ink = cssVar("--ink"), muted = cssVar("--muted"), grid = cssVar("--grid"),
        crit = cssVar("--crit"), ok = cssVar("--ok"), amber = cssVar("--amber");
    var padL = 34, padR = 44, padT = 14, padB = 26;
    var iw = w - padL - padR, ih = h - padT - padB;

    var maxCount = 0, maxPct = 0;
    days.forEach(function (d) {
      if (d.count > maxCount) maxCount = d.count;
      if (d.pct > maxPct) maxPct = d.pct;
    });
    var yMax = niceMax(maxCount * 1.1, 4);
    var pMax = niceMax(maxPct * 1.1, 2);

    ctx.clearRect(0, 0, w, h);
    ctx.font = "10.5px " + MONO;
    var yDiv = 4;
    for (var g = 0; g <= yDiv; g++) {
      var y = padT + ih - ih * g / yDiv;
      ctx.strokeStyle = grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
      ctx.fillStyle = muted; ctx.textAlign = "right";
      var cv = yMax * g / yDiv;
      ctx.fillText(cv % 1 === 0 ? String(cv) : cv.toFixed(1), padL - 6, y + 3.5);
      ctx.textAlign = "left";
      var pv = pMax * g / yDiv;
      ctx.fillText((pv % 1 === 0 ? pv : pv.toFixed(1)) + "%", padL + iw + 6, y + 3.5);
    }

    var n = days.length, slot = iw / n, bw = Math.max(2, Math.min(26, slot * 0.55));
    var step = labelStep(n, 12);
    // Counts are the primary reading of this chart (prototype behavior):
    // draw the number above every bar unless slots are too narrow to fit
    // two digits at all.
    var showCountLabels = slot >= 9;
    days.forEach(function (d, i) {
      var x = padL + slot * i + slot / 2;
      if (d.count > 0) {
        var bh = ih * Math.min(d.count, yMax) / yMax;
        ctx.fillStyle = crit;
        ctx.fillRect(x - bw / 2, padT + ih - bh, bw, bh);
        if (showCountLabels) {
          ctx.fillStyle = ink; ctx.textAlign = "center";
          ctx.fillText(String(d.count), x, padT + ih - bh - 5);
        }
      } else {
        ctx.strokeStyle = ok; ctx.lineWidth = 1.4;
        ctx.strokeRect(x - bw / 2, padT + ih - 4, bw, 4);
      }
      if (i % step === 0 || i === n - 1) {
        ctx.fillStyle = d.count >= yMax / 2 ? ink : muted;
        ctx.textAlign = "center";
        ctx.fillText(d.label, x, h - 8);
      }
    });

    ctx.strokeStyle = amber; ctx.lineWidth = 2; ctx.beginPath();
    days.forEach(function (d, i) {
      var x = padL + slot * i + slot / 2;
      var y = padT + ih - ih * Math.min(d.pct, pMax) / pMax;
      if (i === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
    });
    ctx.stroke();
    days.forEach(function (d, i) {
      var x = padL + slot * i + slot / 2;
      var y = padT + ih - ih * Math.min(d.pct, pMax) / pMax;
      ctx.fillStyle = amber; ctx.beginPath(); ctx.arc(x, y, 2.6, 0, 7); ctx.fill();
    });
  }

  /*
   * Day x hour heatmap — losses against actual usage (prototype port).
   * rows = [{ key, label, usage: [24 ints], losses: [24 ints] }]
   * opts = {
   *   cellTitle: fn(row, hour, usage, losses)->string   — tooltip text
   *   onSelect : fn(row, hour, td) | undefined          — click on a loss cell
   *   onClear  : fn() | undefined                       — selection toggled off
   *   usageLegendEl: element | undefined                — chip to tint
   * }
   */
  function renderUsageHeatmap(tableEl, rows, opts) {
    opts = opts || {};
    var critRGB = cssVar("--crit"), mutedRGB = cssVar("--muted");
    var cellTitle = opts.cellTitle || function () { return ""; };

    var maxLoss = 4, capReq = 100;
    rows.forEach(function (r) {
      r.losses.forEach(function (v) { if (v > maxLoss) maxLoss = v; });
      r.usage.forEach(function (v) { if (v > capReq) capReq = v; });
    });
    capReq = Math.min(capReq, 400);

    var html = "<tr><th class='day'></th>" +
      Array.from({ length: 24 }, function (_, x) {
        return "<th>" + String(x).padStart(2, "0") + "</th>";
      }).join("") + "</tr>";

    rows.forEach(function (r, ri) {
      html += "<tr><th class='day'>" + r.label + "</th>" + r.losses.map(function (v, hi) {
        var rq = r.usage[hi];
        var tip = "title=\"" + cellTitle(r, hi, rq, v) + "\"";
        if (v > 0) {
          var a = 0.3 + 0.7 * Math.min(v, maxLoss) / maxLoss;
          return "<td class='hit' " + tip + " data-r='" + ri + "' data-h='" + hi +
            "' style='background:color-mix(in srgb, " + critRGB + " " +
            Math.round(a * 100) + "%, var(--grid))'>" + v + "</td>";
        }
        if (rq > 0) {
          var u = 12 + Math.round(38 * Math.min(rq, capReq) / capReq);
          return "<td " + tip + " style='background:color-mix(in srgb, " +
            mutedRGB + " " + u + "%, var(--bg))'></td>";
        }
        return "<td " + tip + "></td>";
      }).join("") + "</tr>";
    });
    tableEl.innerHTML = html;

    if (!tableEl.dataset.bound) {
      tableEl.dataset.bound = "1";
      tableEl.addEventListener("click", function (ev) {
        var td = ev.target.closest("td.hit");
        if (!td) return;
        var sel = tableEl.querySelector("td.hit.sel");
        if (sel === td) {
          td.classList.remove("sel");
          if (tableEl._hmOnClear) tableEl._hmOnClear();
          return;
        }
        if (sel) sel.classList.remove("sel");
        td.classList.add("sel");
        var cb = tableEl._hmOnSelect;
        if (cb) {
          var rws = tableEl._hmRows;
          cb(rws[parseInt(td.dataset.r, 10)], parseInt(td.dataset.h, 10), td);
        }
      });
    }
    // Re-render safe: callbacks/rows are refreshed on every call.
    tableEl._hmRows = rows;
    tableEl._hmOnSelect = opts.onSelect || null;
    tableEl._hmOnClear = opts.onClear || null;

    if (opts.usageLegendEl) {
      opts.usageLegendEl.style.background =
        "color-mix(in srgb, " + mutedRGB + " 35%, var(--bg))";
    }
  }

  root.ObservatoryCharts = {
    PALETTE: PALETTE,
    renderFleetTrend: renderFleetTrend,
    renderDailyBars: renderDailyBars,
    renderUsageHeatmap: renderUsageHeatmap
  };
})(typeof self !== "undefined" ? self : this);
