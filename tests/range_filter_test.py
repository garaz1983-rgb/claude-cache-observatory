#!/usr/bin/env python3
"""Range-filter test for the submission period picker (M6).

check.html lets the user narrow the shared period when a scan is longer than
the API's 92-day cap. The payload must then be RECOMPUTED for that window:
/api/submit re-derives every total from `daily` and rejects a payload whose
sums disagree (04_DATA_MODEL.md, 06_FUNCTIONAL_SPEC.md; submit_contract_test
case10). The tier split (confirmed/probable) and `iron_losses` have no daily
column, so they are recounted from per-event records — never estimated.

This test drives assets/parse.js through node (tests/run_range.js) and checks,
for each window:

  - sum(daily.requests)      == totals.requests
  - sum(daily.losses)        == confirmed_losses + probable_losses
  - sum(daily.pmnf)          == totals.pmnf_losses
  - sum(daily.wasted_tokens) == totals.wasted_tokens
  - totals tier counts       == events with that classification in-window
  - totals.iron_losses       == counted events with iron true in-window
  - totals.iron_losses      <= confirmed + probable (API sanity rule)
  - daily dates sorted, unique and inside [period_start, period_end]
  - the whole slice equals an independently recomputed expectation

plus clampRange()/daySpan(), the helpers behind the "last 92 days" default:
the window it picks must never exceed the API's calendar-span cap, and must
be the widest one that fits.

Exit 0 all-pass, exit 1 on a mismatch, exit 2 on setup failure (node missing).
Node + Python stdlib only; nothing outside site/tests is touched.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
FIXTURE_DIR = os.path.join(HERE, "fixtures")
RUNNER = os.path.join(HERE, "run_range.js")

MAX_PERIOD_DAYS = 92

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]


class SetupFail(Exception):
    pass


class CheckFail(Exception):
    pass


def find_node():
    env_node = os.environ.get("CACHE_OBS_NODE")
    if env_node and os.path.isfile(env_node):
        return env_node
    which = shutil.which("node")
    if which:
        return os.path.abspath(which)
    for cand in NODE_FALLBACKS:
        if os.path.isfile(cand):
            return cand
    return None


def is_date_key(v):
    """Same shape test parse.js applies to a range bound."""
    return isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None


def day(iso):
    return datetime.date(int(iso[0:4]), int(iso[5:7]), int(iso[8:10]))


def span_days(start_iso, end_iso):
    """Inclusive calendar span, the same arithmetic the API uses."""
    return (day(end_iso) - day(start_iso)).days + 1


def shift(iso, n):
    return (day(iso) + datetime.timedelta(days=n)).isoformat()


def check(cond, msg):
    if not cond:
        raise CheckFail(msg)


def expected_slice(daily, events, lo, hi):
    """Recompute the window independently of parse.js."""
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    rows = [d for d in daily
            if (lo is None or d["date"] >= lo) and (hi is None or d["date"] <= hi)]
    if not rows:
        return None
    rows.sort(key=lambda d: d["date"])
    first, last = rows[0]["date"], rows[-1]["date"]
    counted = [e for e in events
               if e["classification"] in ("confirmed", "probable")
               and first <= e["date"] <= last]
    return {
        "period_start": first,
        "period_end": last,
        "totals": {
            "requests": sum(r["requests"] for r in rows),
            "confirmed_losses": sum(1 for e in counted
                                    if e["classification"] == "confirmed"),
            "probable_losses": sum(1 for e in counted
                                   if e["classification"] == "probable"),
            "iron_losses": sum(1 for e in counted if e.get("iron") is True),
            "wasted_tokens": sum(r["wasted_tokens"] for r in rows),
            "pmnf_losses": sum(r["pmnf"] for r in rows),
        },
        "daily": [{k: r[k] for k in ("date", "requests", "losses", "pmnf",
                                     "wasted_tokens")}
                  for r in rows],
    }


def expected_clamp(dates, max_days):
    keys = sorted(d for d in dates)
    if not keys:
        return None
    end = keys[-1]
    start = end
    for k in keys:
        if span_days(k, end) <= max_days:
            start = k
            break
    return {"start": start, "end": end}


def synthetic_date_lists():
    """(name, dates, maxDays) triples for clampRange."""
    dense = [shift("2026-01-01", i) for i in range(150)]        # 150 straight days
    sparse = [shift("2026-01-01", i * 40) for i in range(10)]   # 361-day calendar span
    short = [shift("2026-05-01", i) for i in range(5)]
    exact = [shift("2026-03-01", i) for i in range(MAX_PERIOD_DAYS)]
    return [
        ("dense150", dense, MAX_PERIOD_DAYS),
        ("sparse40d", sparse, MAX_PERIOD_DAYS),
        ("short5", short, MAX_PERIOD_DAYS),
        ("exact92", exact, MAX_PERIOD_DAYS),
        ("single", [dense[0]], MAX_PERIOD_DAYS),
    ]


def check_slice_invariants(name, sl):
    """Contract rules /api/submit enforces on any payload we build."""
    t = sl["totals"]
    check(sum(d["requests"] for d in sl["daily"]) == t["requests"],
          "%s: sum(daily.requests) != totals.requests" % name)
    check(sum(d["losses"] for d in sl["daily"])
          == t["confirmed_losses"] + t["probable_losses"],
          "%s: sum(daily.losses) != confirmed+probable" % name)
    check(sum(d["pmnf"] for d in sl["daily"]) == t["pmnf_losses"],
          "%s: sum(daily.pmnf) != totals.pmnf_losses" % name)
    check(sum(d["wasted_tokens"] for d in sl["daily"]) == t["wasted_tokens"],
          "%s: sum(daily.wasted_tokens) != totals.wasted_tokens" % name)
    losses = t["confirmed_losses"] + t["probable_losses"]
    check(t["iron_losses"] <= losses,
          "%s: iron_losses %d > confirmed+probable %d"
          % (name, t["iron_losses"], losses))
    check(losses <= t["requests"], "%s: losses > requests" % name)
    dates = [d["date"] for d in sl["daily"]]
    check(dates == sorted(dates), "%s: daily dates not sorted" % name)
    check(len(set(dates)) == len(dates), "%s: duplicate daily dates" % name)
    check(sl["period_start"] == dates[0] and sl["period_end"] == dates[-1],
          "%s: period bounds do not match the daily rows" % name)
    for d in sl["daily"]:
        check(sl["period_start"] <= d["date"] <= sl["period_end"],
              "%s: daily %s outside the period" % (name, d["date"]))
        check(d["losses"] <= d["requests"],
              "%s: daily %s losses > requests" % (name, d["date"]))
        check(set(d.keys()) == {"date", "requests", "losses", "pmnf", "wasted_tokens"},
              "%s: daily %s carries undefined fields %r" % (name, d["date"], set(d)))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    node = find_node()
    if node is None:
        print("FATAL: node not found (install Node.js or set CACHE_OBS_NODE)")
        return 2
    if not os.path.isfile(RUNNER):
        print("FATAL: %s missing" % RUNNER)
        return 2

    # Windows to exercise, filled in once the fixture dates are known.
    probe = subprocess.run(
        [node, RUNNER, FIXTURE_DIR, json.dumps({})],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=SITE_DIR)
    if probe.returncode != 0:
        print("FATAL: run_range.js probe failed: %s" % probe.stderr.strip())
        return 2
    base = json.loads(probe.stdout)
    dates = [d["date"] for d in base["daily"]]
    check_setup = len(dates) >= 3
    if not check_setup:
        print("FATAL: fixtures carry %d dates, need >= 3" % len(dates))
        return 2

    ranges = [
        [None, None],                       # unbounded: the whole scan
        [dates[0], dates[-1]],              # explicit full range
        [dates[0], dates[0]],               # first day only
        [dates[1], dates[1]],               # middle day only
        [dates[-1], dates[-1]],             # last day only
        [dates[1], dates[-1]],              # drop the busiest first day
        [dates[0], dates[1]],               # drop the tail
        [dates[-1], dates[0]],              # reversed: must be corrected
        [shift(dates[-1], 30), shift(dates[-1], 60)],   # after every day: null
        [shift(dates[0], -60), shift(dates[0], -30)],   # before every day: null
        [shift(dates[0], -5), shift(dates[-1], 5)],     # wider than the data
        ["not-a-date", dates[-1]],          # junk bound is ignored, not fatal
    ]
    clamps = [{"dates": d, "maxDays": m} for (_, d, m) in synthetic_date_lists()]
    spans = [
        ["2026-01-01", "2026-01-01"],
        ["2026-01-01", "2026-04-02"],       # exactly 92
        ["2026-01-01", "2026-04-03"],       # 93, the API's first rejection
        ["2026-02-27", "2026-03-01"],       # across a month end
        ["2025-12-30", "2026-01-02"],       # across a year end
    ]

    job = {"ranges": ranges, "clamps": clamps, "spans": spans}
    proc = subprocess.run(
        [node, RUNNER, FIXTURE_DIR, json.dumps(job)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=SITE_DIR)
    if proc.returncode != 0:
        print("FATAL: run_range.js failed: %s" % proc.stderr.strip())
        return 2
    out = json.loads(proc.stdout)

    try:
        check(out["max_period_days"] == MAX_PERIOD_DAYS,
              "parse.js MAX_PERIOD_DAYS=%r, expected %d"
              % (out["max_period_days"], MAX_PERIOD_DAYS))

        iron_total = sum(1 for e in out["events"] if e.get("iron") is True)
        check(iron_total == out["totals"]["iron_losses"],
              "fixture sanity: %d iron events vs totals.iron_losses %d"
              % (iron_total, out["totals"]["iron_losses"]))
        check(iron_total > 0, "fixture sanity: no iron events, the recount is untested")

        for i, (rng, got) in enumerate(zip(ranges, out["slices"])):
            name = "range%d %s..%s" % (i, rng[0], rng[1])
            lo = rng[0] if is_date_key(rng[0]) else None
            hi = rng[1] if is_date_key(rng[1]) else None
            want = expected_slice(base["daily"], out["events"], lo, hi)
            check(got == want, "%s: slice %r != expected %r" % (name, got, want))
            if got is None:
                continue
            check_slice_invariants(name, got)
        print("PASS %d windows: sum(daily)==totals, iron recounted from events"
              % len(ranges))

        # A narrowed window must actually differ from the full scan, otherwise
        # the equality checks above would pass on a filter that does nothing.
        full = out["slices"][1]
        narrowed = out["slices"][3]
        check(narrowed["totals"]["requests"] < full["totals"]["requests"],
              "the narrowed window did not drop any request (filter is a no-op?)")
        check(narrowed["daily"] != full["daily"],
              "the narrowed window kept every daily row (filter is a no-op?)")
        iron_dates = set(e["date"] for e in out["events"]
                         if e.get("iron") is True)
        dropped = [d for d in dates if d in iron_dates and
                   not (narrowed["period_start"] <= d <= narrowed["period_end"])]
        check(dropped, "no iron-bearing day falls outside the narrowed window; "
                       "the iron recount is not actually exercised")
        check(narrowed["totals"]["iron_losses"] < full["totals"]["iron_losses"],
              "iron_losses did not shrink with the window (%d vs %d)"
              % (narrowed["totals"]["iron_losses"], full["totals"]["iron_losses"]))
        print("PASS narrowing drops requests and iron losses "
              "(full %d/%d -> window %d/%d)"
              % (full["totals"]["requests"], full["totals"]["iron_losses"],
                 narrowed["totals"]["requests"], narrowed["totals"]["iron_losses"]))

        for (name, ds, maxd), got in zip(synthetic_date_lists(), out["clamps"]):
            want = expected_clamp(ds, maxd)
            check(got == want, "clamp %s: %r != %r" % (name, got, want))
            check(span_days(got["start"], got["end"]) <= maxd,
                  "clamp %s: span %d exceeds %d"
                  % (name, span_days(got["start"], got["end"]), maxd))
            check(got["end"] == max(ds), "clamp %s: end is not the latest date" % name)
            kept = [d for d in ds if got["start"] <= d <= got["end"]]
            check(len(kept) <= maxd,
                  "clamp %s: %d entries exceed the %d-entry cap"
                  % (name, len(kept), maxd))
            earlier = [d for d in ds if d < got["start"]]
            if earlier:
                check(span_days(max(earlier), got["end"]) > maxd,
                      "clamp %s: window is narrower than it needs to be" % name)
        print("PASS clampRange on %d date lists (span <= %d, widest that fits)"
              % (len(clamps), MAX_PERIOD_DAYS))

        for pair, got in zip(spans, out["spans"]):
            want = span_days(pair[0], pair[1])
            check(got == want, "daySpan %r: %r != %r" % (pair, got, want))
        print("PASS daySpan on %d pairs (matches the API's inclusive arithmetic)"
              % len(spans))
    except CheckFail as exc:
        print("RANGE_FAIL: %s" % exc)
        return 1

    print("RANGE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
