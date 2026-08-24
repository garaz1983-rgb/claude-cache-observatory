#!/usr/bin/env python3
"""Local-time view test for the personal diagnosis screens (M12).

check.html and ko/check.html now draw the day rows, the hour columns, the
per-event times, the daily trend and the observed period in the reader's own
timezone, while the submission payload and the public observatory stay on UTC.
That split is only safe if re-bucketing is a MOVE and never an edit, so this is
what the test holds:

  1. SUM PRESERVATION. Re-cutting the calendar moves whole requests between
     days; it never creates, drops or splits one. For every offset, and on
     every fixture set:
         sum(local daily.requests)      == sum(utc daily.requests)      == totals.requests
         sum(local daily.losses)        == sum(utc daily.losses)        == totals.in_ttl_losses
         sum(local daily.wasted_tokens) == sum(utc daily.wasted_tokens) == totals.wasted_tokens
     The hourly census is checked the same way: its grand total must equal
     totals.requests both before and after the move, which is also what catches
     the test runner's copy of check.html's census walk drifting from the page's.

  2. THE BOUNDARY ACTUALLY MOVES, AND MOVES TO THE RIGHT DAY. tests/fixtures_tz
     is the two-line synthetic seed the defect was measured on (a loss stamped
     2026-08-23T23:12:11Z, which is 2026-08-24 08:12:11 in Seoul), extended with
     a pair that straddles local midnight from the other side: a request at
     local 23:50:49 and a loss twelve minutes later at local 00:03:00, both
     inside the SAME UTC day, plus one loss that moves a day BACKWARD for a
     reader west of UTC. Each one is asserted at its expected local date, hour
     and clock time — and the UTC daily rows the submission carries are asserted
     unchanged in the same breath.

  3. OFFSET 0 IS A NO-OP, BYTE FOR BYTE. A reader whose timezone is UTC must see
     exactly what they saw before M12. The comparison is against a baseline the
     runner builds with a literal copy of the page's pre-M12 code, so it is two
     implementations agreeing rather than the module agreeing with itself, and
     it is a string comparison of the serialised daily rows, census and event
     map rather than a structural one.

  4. A SAVED RUN LOCALISES THE SAME WAY. M10 keeps runs anchored to the engine's
     UTC buckets and applies the reader's clock at display time. So the same
     scan pushed through store.js save -> load -> hydrate -> localize must draw
     the same screen as the live scan, at every offset.

Judgment is deliberately out of scope here: whether a request is an in-TTL loss
is decided by the idle gap between requests, which no timezone can change.
tests/parity_check.py is what holds that, and this test only ever reads the
classification the engine already stamped.

Exit 0 all-pass, exit 1 on a mismatch, exit 2 on setup failure (node missing).
Node + Python stdlib only; nothing outside site/tests is touched.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "run_localtime.js")
FIXTURE_TZ = os.path.join(HERE, "fixtures_tz")
FIXTURE_MAIN = os.path.join(HERE, "fixtures")
FIXTURE_HOSTILE = os.path.join(HERE, "fixtures_hostile")

# Minutes east of UTC. Whole hours either side, the half- and quarter-hour
# zones (India, Newfoundland, Chatham) and the two extremes.
OFFSETS = [0, 60, -300, 330, 540, 825, -210, -720, 780]

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]

# tests/fixtures_tz, as the engines bucket it: this is what the submission
# payload carries, and it must not move no matter what the screen shows.
TZ_UTC_DAILY = [
    {"date": "2026-08-23", "requests": 2, "losses": 1, "wasted_tokens": 86300},
    {"date": "2026-08-24", "requests": 2, "losses": 1, "wasted_tokens": 91700},
    {"date": "2026-08-25", "requests": 2, "losses": 1, "wasted_tokens": 40000},
]
TZ_TOTALS = {"requests": 6, "in_ttl_losses": 3, "iron_losses": 0,
             "wasted_tokens": 218000}

# The same six records read on three different clocks.
TZ_LOCAL_DAILY = {
    0: TZ_UTC_DAILY,
    # Seoul: the 23:12Z loss is a morning loss, and the 15:03Z one has already
    # crossed into the next day while its own UTC day is still running.
    540: [
        {"date": "2026-08-24", "requests": 3, "losses": 1, "wasted_tokens": 86300},
        {"date": "2026-08-25", "requests": 3, "losses": 2, "wasted_tokens": 131700},
    ],
    # New York (standard time): the 02:12Z loss belongs to the previous evening.
    -300: [
        {"date": "2026-08-23", "requests": 2, "losses": 1, "wasted_tokens": 86300},
        {"date": "2026-08-24", "requests": 4, "losses": 2, "wasted_tokens": 131700},
    ],
}

# (offset, utc timestamp, expected local key, expected local clock time)
TZ_EVENT_PLACEMENT = [
    # The reported defect: 08:12 in Seoul, drawn on 8/23 before M12.
    (540, "2026-08-23T23:12:11.000Z", "2026-08-24#8", "08:12:11"),
    # Just after local midnight, inside a UTC day that has not ended.
    (540, "2026-08-24T15:03:00.000Z", "2026-08-25#0", "00:03:00"),
    (540, "2026-08-25T02:12:11.000Z", "2026-08-25#11", "11:12:11"),
    # A day backward for a reader west of UTC.
    (-300, "2026-08-25T02:12:11.000Z", "2026-08-24#21", "21:12:11"),
    (-300, "2026-08-23T23:12:11.000Z", "2026-08-23#18", "18:12:11"),
    # Offset 0 leaves every event exactly where the engine put it.
    (0, "2026-08-23T23:12:11.000Z", "2026-08-23#23", "23:12:11"),
    (0, "2026-08-24T15:03:00.000Z", "2026-08-24#15", "15:03:00"),
]

# The request twelve minutes BEFORE local midnight has to stay on the earlier
# local day: (offset, local date, local hour, expected request count).
TZ_CENSUS_CELLS = [
    (540, "2026-08-24", 23, 1),   # 2026-08-24T14:50:49Z -> 23:50:49 in Seoul
    (540, "2026-08-25", 0, 1),    # 2026-08-24T15:03:00Z -> 00:03:00, next day
    (540, "2026-08-24", 8, 2),    # both 23:xxZ records, one local morning hour
    (0, "2026-08-24", 14, 1),     # unmoved at offset 0
    (0, "2026-08-24", 15, 1),
]

EXPECTED_LABELS = {
    "0": "UTC", "60": "UTC+1", "330": "UTC+5:30", "540": "UTC+9",
    "825": "UTC+13:45", "-300": "UTC-5", "-210": "UTC-3:30",
}


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


def check(cond, msg):
    if not cond:
        raise CheckFail(msg)


def run_runner(node, fixture_dir, offsets, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [node, RUNNER, fixture_dir, json.dumps(offsets)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=180,
    )
    if proc.returncode != 0:
        print("localtime_test: runner failed on %s (exit %d)"
              % (fixture_dir, proc.returncode))
        print(proc.stderr)
        sys.exit(2)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        print("localtime_test: runner produced non-JSON output")
        print(proc.stdout[:2000])
        sys.exit(2)


def sum_daily(rows, key):
    return sum(r[key] for r in rows)


def check_sums(tag, data):
    """Invariant 1, on one fixture set: nothing is created, dropped or split."""
    totals = data["totals"]
    utc = data["utc_daily"]
    check(sum_daily(utc, "requests") == totals["requests"],
          "%s: engine daily requests != totals" % tag)
    check(sum_daily(utc, "losses") == totals["in_ttl_losses"],
          "%s: engine daily losses != totals" % tag)
    check(sum_daily(utc, "wasted_tokens") == totals["wasted_tokens"],
          "%s: engine daily wasted_tokens != totals" % tag)
    for off, view in sorted(data["views"].items(), key=lambda kv: int(kv[0])):
        s = view["sums"]
        check(s["requests"] == totals["requests"],
              "%s off=%s: local requests %d != totals %d"
              % (tag, off, s["requests"], totals["requests"]))
        check(s["losses"] == totals["in_ttl_losses"],
              "%s off=%s: local losses %d != totals %d"
              % (tag, off, s["losses"], totals["in_ttl_losses"]))
        check(s["wasted_tokens"] == totals["wasted_tokens"],
              "%s off=%s: local wasted_tokens %d != totals %d"
              % (tag, off, s["wasted_tokens"], totals["wasted_tokens"]))
        check(view["census_total"] == totals["requests"],
              "%s off=%s: local census total %r != totals.requests %d"
              % (tag, off, view["census_total"], totals["requests"]))
        check(len(view["events"]) == totals["in_ttl_losses"],
              "%s off=%s: %d localised events for %d losses"
              % (tag, off, len(view["events"]), totals["in_ttl_losses"]))
        dates = [r["date"] for r in view["daily"]]
        check(dates == sorted(dates) and len(set(dates)) == len(dates),
              "%s off=%s: local daily dates not sorted/unique: %r" % (tag, off, dates))


def check_offset0_identity(tag, data):
    """Invariant 3: at offset 0 the module reproduces the pre-M12 output."""
    zero = data["views"]["0"]
    legacy = data["legacy"]
    for field in ("daily_json", "census_json", "evt_json"):
        check(zero[field] == legacy[field],
              "%s: offset 0 changed %s\n  before: %s\n  after : %s"
              % (tag, field, legacy[field][:400], zero[field][:400]))
    check(zero["localized"] is True, "%s: offset 0 view not marked localised" % tag)


def check_restored(tag, data):
    """Invariant 4: a saved run draws the same screen as the live scan."""
    check(data["restored"], "%s: store round trip produced no view" % tag)
    for off, live in data["views"].items():
        got = data["restored"].get(off)
        check(got is not None, "%s off=%s: no restored view" % (tag, off))
        check(got["daily_json"] == live["daily_json"],
              "%s off=%s: restored daily differs\n  live    : %s\n  restored: %s"
              % (tag, off, live["daily_json"][:400], got["daily_json"][:400]))
        check(got["census_json"] == live["census_json"],
              "%s off=%s: restored census differs" % (tag, off))
        check(got["evt_keys"] == live["evt_keys"],
              "%s off=%s: restored heatmap cells differ: %r vs %r"
              % (tag, off, got["evt_keys"], live["evt_keys"]))


def check_boundary(data):
    """Invariant 2, on the fixture the defect was measured on."""
    check(data["totals"] == TZ_TOTALS,
          "fixtures_tz totals drifted: %r" % (data["totals"],))
    check(data["utc_daily"] == TZ_UTC_DAILY,
          "fixtures_tz UTC daily drifted (this is the submission payload): %r"
          % (data["utc_daily"],))

    for off, expected in sorted(TZ_LOCAL_DAILY.items()):
        got = data["views"][str(off)]["daily"]
        check(got == expected,
              "fixtures_tz off=%d: local daily\n  want %r\n  got  %r"
              % (off, expected, got))

    by_ts = {}
    for off, view in data["views"].items():
        for ev in view["events"]:
            by_ts[(int(off), ev["utc_date"] + "T" + ev["utc_time"] + ".000Z")] = ev
    for off, ts, want_key, want_time in TZ_EVENT_PLACEMENT:
        ev = by_ts.get((off, ts))
        check(ev is not None, "fixtures_tz off=%d: no localised event for %s" % (off, ts))
        key = ev["date"] + "#" + str(ev["hour"])
        check(key == want_key,
              "fixtures_tz off=%d: %s landed in %s, expected %s" % (off, ts, key, want_key))
        check(ev["time"] == want_time,
              "fixtures_tz off=%d: %s shown at %s, expected %s"
              % (off, ts, ev["time"], want_time))
        check(want_key in data["views"][str(off)]["evt_keys"],
              "fixtures_tz off=%d: heatmap has no cell %s" % (off, want_key))

    for off, date, hour, want in TZ_CENSUS_CELLS:
        rows = json.loads(data["views"][str(off)]["census_json"])
        row = next((r for r in rows if r["date"] == date), None)
        check(row is not None,
              "fixtures_tz off=%d: no census row for %s" % (off, date))
        check(row["hours"][hour] == want,
              "fixtures_tz off=%d: %s hour %02d has %d requests, expected %d"
              % (off, date, hour, row["hours"][hour], want))


def check_aggregate_only(data):
    """Pasted CLI aggregates carry nothing per-request, so they must stay UTC
    and say so, rather than being re-labelled on a guess."""
    agg = data["aggregate_only"]
    check(agg["localized"] is False,
          "aggregate-only source was marked as localised")
    check(agg["daily_json"] == data["legacy"]["daily_json"],
          "aggregate-only source had its dates changed")
    check(agg["evt_keys"] == [], "aggregate-only source invented heatmap cells")


def check_labels(data):
    for key, want in sorted(EXPECTED_LABELS.items()):
        got = data["labels"].get(key)
        check(got == want, "offsetLabel(%s) = %r, expected %r" % (key, got, want))


def check_host(data):
    """The browser path: no pinned offset, the machine answers per instant."""
    host = data["host"]
    totals = data["totals"]
    check(host["view"]["sums"]["requests"] == totals["requests"],
          "host view lost requests")
    check(host["view"]["sums"]["losses"] == totals["in_ttl_losses"],
          "host view lost losses")
    check(host["view"]["census_total"] == totals["requests"],
          "host view lost census requests")
    label = host["detect"]["label"]
    check(isinstance(label, str) and label,
          "detect() produced no timezone label")
    check(host["detect"]["offset"] in label or label == host["detect"]["offset"],
          "detect() label %r does not carry its own offset %r"
          % (label, host["detect"]["offset"]))
    off = host["offset_at_boundary"]
    if off == host["offset_now"] and str(off) in data["views"]:
        check(host["view"]["daily_json"] == data["views"][str(off)]["daily_json"],
              "host view (offset %d) differs from the pinned view for the same offset"
              % off)
        return "host offset %+d matched the pinned view" % off
    return "host offset %+d not pinned in this sweep" % off


def main():
    node = find_node()
    if node is None:
        print("localtime_test: node executable not found "
              "(set CACHE_OBS_NODE or add node to PATH)")
        return 2

    errors = []
    evidence = []

    tz = run_runner(node, FIXTURE_TZ, OFFSETS)
    main_fx = run_runner(node, FIXTURE_MAIN, OFFSETS)
    hostile = run_runner(node, FIXTURE_HOSTILE, OFFSETS)

    suites = [("fixtures_tz", tz), ("fixtures", main_fx), ("fixtures_hostile", hostile)]
    for tag, data in suites:
        for fn in (check_sums, check_offset0_identity, check_restored):
            try:
                fn(tag, data)
            except CheckFail as exc:
                errors.append(str(exc))

    for fn in (check_boundary, check_aggregate_only, check_labels):
        try:
            fn(tz)
        except CheckFail as exc:
            errors.append(str(exc))

    try:
        evidence.append(check_host(tz))
    except CheckFail as exc:
        errors.append(str(exc))

    # A real environment pinned to UTC must reproduce the pre-M12 screen. Not
    # every platform honours TZ (Windows ignores IANA names), so a run that
    # fails to land on UTC is reported as unavailable rather than as a defect.
    utc_run = run_runner(node, FIXTURE_TZ, [0], {"TZ": "UTC"})
    if utc_run["host"]["offset_now"] == 0:
        if utc_run["host"]["view"]["daily_json"] != utc_run["legacy"]["daily_json"]:
            errors.append("TZ=UTC host run did not reproduce the pre-M12 daily rows")
        elif utc_run["host"]["view"]["evt_json"] != utc_run["legacy"]["evt_json"]:
            errors.append("TZ=UTC host run did not reproduce the pre-M12 heatmap cells")
        else:
            evidence.append("TZ=UTC host run byte-identical to the pre-M12 baseline")
    else:
        evidence.append("TZ=UTC not honoured by this platform (host offset %+d) - skipped"
                        % utc_run["host"]["offset_now"])

    if errors:
        print("LOCALTIME_FAIL (%d)" % len(errors))
        for err in errors:
            print("  - " + err)
        return 1

    print("LOCALTIME_OK")
    print("  fixtures_tz totals: %s" % json.dumps(tz["totals"], sort_keys=True))
    print("  UTC daily (what the submission carries):")
    for row in tz["utc_daily"]:
        print("    %s  requests=%d losses=%d wasted=%d"
              % (row["date"], row["requests"], row["losses"], row["wasted_tokens"]))
    for off in (540, -300):
        print("  local daily at %s:" % tz["labels"].get(str(off), str(off)))
        for row in tz["views"][str(off)]["daily"]:
            print("    %s  requests=%d losses=%d wasted=%d"
                  % (row["date"], row["requests"], row["losses"], row["wasted_tokens"]))
    print("  boundary loss 2026-08-23T23:12:11Z -> %s (UTC row stays 2026-08-23)"
          % next(e["date"] + " " + e["time"] for e in tz["views"]["540"]["events"]
                 if e["utc_time"] == "23:12:11"))
    print("  sums preserved at offsets: %s"
          % ", ".join(sorted(tz["views"], key=lambda k: int(k))))
    print("  offset 0 byte-identical to the pre-M12 baseline on: %s"
          % ", ".join(tag for tag, _ in suites))
    print("  host: %s %s" % (tz["host"]["zone"], tz["host"]["detect"]["label"]))
    for line in evidence:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
