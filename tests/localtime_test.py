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

M12.1 adds four more, all of them defects M12 shipped:

  5. A LOSS IS DRAWN WHERE ITS OWN REQUEST WAS COUNTED. M12 placed an hour cell
     by the instant it begins and an event by its exact instant. In a :30/:45
     zone those are different local hours and, for the hour beginning 23:30
     local, different local DAYS: at Asia/Kolkata the loss landed on a date the
     census did not have, so the heatmap drew it NOWHERE and the daily row read
     losses:1 against requests:0. So for every fixture and every offset:
     every event sits in a cell the local census has, that cell holds at least
     as many requests as it holds losses, and no daily row shows more losses
     than requests. tests/fixtures_tz_subhour pins the exact cells and clock
     times at +5:30, +5:45, +9 and UTC, and carries the only timestamp in any
     fixture written in its own UTC offset rather than "...Z".

  6. A ROW IS LABELLED WITH THE OFFSET AT ITS OWN INSTANT. The offset was
     detected once at page load, so a February row in New York on a page opened
     in August printed UTC-4 over an instant that is UTC-5 — an hour out for
     anyone cross-checking the screen against the UTC payload. The pages'
     labelling code is lifted out of check.html / ko/check.html between its own
     markers and driven with a pinned 2026 New York DST function, so the
     printed strings are asserted, not the intent behind them.

  7. THE PAYLOAD IS BUILT FROM THE ENGINE, NOT FROM THE SCREEN. The single
     invariant M12 exists to protect had no test at all: a mutant reading
     VIEW.daily inside buildSubmitPayload survived every suite while producing a
     KST-cut submission that /api/submit would have accepted. The pages' payload
     block is lifted the same way and run against a LAST and a VIEW that
     deliberately disagree.

  8. BRANCHES NO FIXTURE REACHES are probed directly: an all-zero census row
     (a day the scan covered with no request in it) and offsetAtLocal() on
     either side of a DST change.

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
FIXTURE_SUBHOUR = os.path.join(HERE, "fixtures_tz_subhour")
FIXTURE_MAIN = os.path.join(HERE, "fixtures")
FIXTURE_HOSTILE = os.path.join(HERE, "fixtures_hostile")

# Minutes east of UTC. Whole hours either side, the half- and quarter-hour
# zones (India, Nepal, Newfoundland, Chatham) and the two extremes.
OFFSETS = [0, 60, -300, 330, 345, 540, 825, -210, -720, 780]

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
    "0": "UTC", "60": "UTC+1", "330": "UTC+5:30", "345": "UTC+5:45",
    "540": "UTC+9", "825": "UTC+13:45", "-300": "UTC-5", "-210": "UTC-3:30",
}

# ---- M12.1 / D1: tests/fixtures_tz_subhour ---------------------------------
# Two requests inside one UTC hour (the second an in-TTL loss of 50,000 tokens,
# the pair the defect was measured on), then a second pair stamped in its own
# +05:30 offset rather than as "...Z".
SUBHOUR_TOTALS = {"requests": 4, "in_ttl_losses": 2, "iron_losses": 0,
                  "wasted_tokens": 57000}
SUBHOUR_UTC_DAILY = [
    {"date": "2026-08-23", "requests": 2, "losses": 1, "wasted_tokens": 50000},
    {"date": "2026-08-24", "requests": 2, "losses": 1, "wasted_tokens": 7000},
]
SUBHOUR_LOCAL_DAILY = {
    0: SUBHOUR_UTC_DAILY,
    330: SUBHOUR_UTC_DAILY,
    345: SUBHOUR_UTC_DAILY,
    540: [{"date": "2026-08-24", "requests": 4, "losses": 2, "wasted_tokens": 57000}],
}
# (offset, local date, local hour, requests in that cell, losses in that cell).
# The requests column is what a mutated cellMs moves: at +5:30 the hour
# beginning 18:00Z is 23:30 local and belongs to column 23, not column 0 of the
# next day.
SUBHOUR_CELLS = [
    (330, "2026-08-23", 23, 2, 1), (330, "2026-08-24", 7, 2, 1),
    (345, "2026-08-23", 23, 2, 1), (345, "2026-08-24", 7, 2, 1),
    (540, "2026-08-24", 3, 2, 1), (540, "2026-08-24", 11, 2, 1),
    (0, "2026-08-23", 18, 2, 1), (0, "2026-08-24", 2, 2, 1),
]
# (offset, the engine's own bucket instant, the cell drawn, the exact local
#  clock printed). The 02:10 rows are the +05:30-stamped record: the engine
#  buckets it by its own local date, so its bucket instant is 02:10 on 08-24
#  and not the 20:40Z its epoch alone would give — which is what fails loudly
#  if bucketMsOf ever drops a record's offset.
SUBHOUR_EVENTS = [
    (330, "2026-08-23T18:45:00", "2026-08-23#23", "00:15:00"),
    (330, "2026-08-24T02:10:00", "2026-08-24#7", "07:40:00"),
    (345, "2026-08-23T18:45:00", "2026-08-23#23", "00:30:00"),
    (345, "2026-08-24T02:10:00", "2026-08-24#7", "07:55:00"),
    (540, "2026-08-23T18:45:00", "2026-08-24#3", "03:45:00"),
    (540, "2026-08-24T02:10:00", "2026-08-24#11", "11:10:00"),
    (0, "2026-08-23T18:45:00", "2026-08-23#18", "18:45:00"),
    (0, "2026-08-24T02:10:00", "2026-08-24#2", "02:10:00"),
]

# ---- M12.1 / D2: what the pages PRINT --------------------------------------
# Driven with a pinned 2026 America/New_York DST function, so these hold on a
# machine in any zone. The February row is the measured defect: it printed
# UTC-4 (the offset at page load, in August) over an instant that is UTC-5.
EXPECTED_PAGE_LABELS = {
    "check.html": {
        "ny_cell_feb": "2026-02-15 13:00 UTC-5",
        "ny_cell_aug": "2026-08-15 14:00 UTC-4",
        "ny_offset_feb": -300,
        "ny_offset_aug": -240,
        "ny_head_feb": "Time (UTC-5)",
        "ny_head_aug": "Time (UTC-4)",
        "ny_head_split": "Time",
        "ny_time_uniform": "13:02:00",
        "ny_time_split": "13:02:00 UTC-5",
        "kolkata_cell": "2026-08-23 23:30–00:30 UTC+5:30",
        "seoul_cell": "2026-08-24 08:00 UTC+9",
        "utc_cell": "2026-08-23 23:00 UTC",
    },
    "ko/check.html": {
        "ny_cell_feb": "2026-02-15 13:00 UTC-5",
        "ny_cell_aug": "2026-08-15 14:00 UTC-4",
        "ny_offset_feb": -300,
        "ny_offset_aug": -240,
        "ny_head_feb": "시각(UTC-5)",
        "ny_head_aug": "시각(UTC-4)",
        "ny_head_split": "시각",
        "ny_time_uniform": "13:02:00",
        "ny_time_split": "13:02:00 UTC-5",
        "kolkata_cell": "2026-08-23 23:30~00:30 UTC+5:30",
        "seoul_cell": "2026-08-24 08:00 UTC+9",
        "seoul_cell_han": "2026-08-24 08시 UTC+9",
        "kolkata_cell_han": "2026-08-23 23:30~00:30 UTC+5:30",
        "utc_cell": "2026-08-23 23:00 UTC",
    },
}

# The heatmap's own disclosure. A whole-hour zone must keep, word for word, the
# sentence the page carried before M12.1 — that is the "renders exactly as it
# does today" half of the requirement, asserted rather than assumed.
LEGACY_HM_NOTE = {
    "check.html": (
        "The day rows and the hour columns are your own local clock",
        ", not UTC, so 08:00 here is 08:00 where you are sitting.",
    ),
    "ko/check.html": (
        "날짜 행과 시각 열은 UTC가 아니라 "
        "이 브라우저가 있는 곳의 시계",
        "다. 여기 08시는 지금 앉아 있는 "
        "곳의 08시다.",
    ),
}
# What a :30/:45 zone must be told instead, and what a DST-spanning scan adds.
SUBHOUR_NOTE_MARK = {"check.html": ":30 past the hour shown",
                     "ko/check.html": ":30 지난 지점"}
DST_NOTE_MARK = {"check.html": "daylight-saving change",
                 "ko/check.html": "서머타임 경계"}

PAGES = ["check.html", "ko/check.html"]


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


def census_rows(view):
    return {r["date"]: r["hours"] for r in json.loads(view["census_json"])}


def check_placement(tag, data):
    """M12.1 invariant 5, on every fixture at every offset.

    Not "the loss looks about right" but three structural facts: the cell it is
    drawn in exists, that cell was counted as usage, and the day it lands on
    cannot claim more losses than requests. Any of the three failing is the
    Kolkata defect back in some form.
    """
    for off, view in sorted(data["views"].items(), key=lambda kv: int(kv[0])):
        rows = census_rows(view)
        per_cell = {}
        for ev in view["events"]:
            key = (ev["date"], ev["hour"])
            per_cell[key] = per_cell.get(key, 0) + 1
        for (date, hour), losses in sorted(per_cell.items()):
            check(date in rows,
                  "%s off=%s: %d loss(es) drawn on %s, a day the census has no "
                  "row for - nothing would render them" % (tag, off, losses, date))
            check(rows[date][hour] >= losses,
                  "%s off=%s: cell %s#%d holds %d losses over %d requests"
                  % (tag, off, date, hour, losses, rows[date][hour]))
            check(("%s#%d" % (date, hour)) in view["evt_keys"],
                  "%s off=%s: %s#%d missing from the heatmap event map"
                  % (tag, off, date, hour))
        for row in view["daily"]:
            check(row["losses"] <= row["requests"],
                  "%s off=%s: daily row %s reads losses %d against requests %d"
                  % (tag, off, row["date"], row["losses"], row["requests"]))


def check_subhour(data):
    """M12.1 invariant 5 pinned to exact cells and clock times."""
    check(data["totals"] == SUBHOUR_TOTALS,
          "fixtures_tz_subhour totals drifted: %r" % (data["totals"],))
    check(data["utc_daily"] == SUBHOUR_UTC_DAILY,
          "fixtures_tz_subhour UTC daily drifted (this is the submission "
          "payload): %r" % (data["utc_daily"],))

    for off, expected in sorted(SUBHOUR_LOCAL_DAILY.items()):
        got = data["views"][str(off)]["daily"]
        check(got == expected,
              "fixtures_tz_subhour off=%d: local daily\n  want %r\n  got  %r"
              % (off, expected, got))

    for off, date, hour, want_req, want_loss in SUBHOUR_CELLS:
        view = data["views"][str(off)]
        rows = census_rows(view)
        check(date in rows,
              "fixtures_tz_subhour off=%d: no census row for %s (have %r)"
              % (off, date, sorted(rows)))
        check(rows[date][hour] == want_req,
              "fixtures_tz_subhour off=%d: cell %s#%d holds %d requests, "
              "expected %d" % (off, date, hour, rows[date][hour], want_req))
        got_loss = sum(1 for e in view["events"]
                       if e["date"] == date and e["hour"] == hour)
        check(got_loss == want_loss,
              "fixtures_tz_subhour off=%d: cell %s#%d holds %d losses, "
              "expected %d" % (off, date, hour, got_loss, want_loss))

    for off, bucket, want_key, want_time in SUBHOUR_EVENTS:
        view = data["views"][str(off)]
        ev = next((e for e in view["events"]
                   if e["utc_date"] + "T" + e["utc_time"] == bucket), None)
        check(ev is not None,
              "fixtures_tz_subhour off=%d: no event bucketed at %s (have %r)"
              % (off, bucket, [e["utc_date"] + "T" + e["utc_time"]
                               for e in view["events"]]))
        key = ev["date"] + "#" + str(ev["hour"])
        check(key == want_key,
              "fixtures_tz_subhour off=%d: %s drawn in %s, expected %s"
              % (off, bucket, key, want_key))
        check(ev["time"] == want_time,
              "fixtures_tz_subhour off=%d: %s printed at %s, expected %s"
              % (off, bucket, ev["time"], want_time))
        check(ev["offsetMinutes"] == off,
              "fixtures_tz_subhour off=%d: %s labelled %r"
              % (off, bucket, ev["offsetMinutes"]))


def check_payload(tag, data, discriminating):
    """M12.1 invariant 7 (D3): the payload comes from the engine's UTC rows.

    `discriminating` collects the fixtures where the localised view really does
    disagree with the engine. Not every fixture can (a scan inside one local day
    localises to the same rows), but at least one must, or the guard would pass
    while guarding nothing — main() asserts that separately.
    """
    utc_daily = [{k: d[k] for k in ("date", "requests", "losses", "wasted_tokens")}
                 for d in data["utc_daily"]]
    first, last = utc_daily[0]["date"], utc_daily[-1]["date"]
    for page in PAGES:
        got = data["page_payload"].get(page)
        check(got is not None, "%s: no payload probe for %s" % (tag, page))
        if got["view_daily"] != utc_daily and (tag, page) not in discriminating:
            discriminating.append((tag, page))
        for path in ("whole", "ranged"):
            pay = got[path]
            check(pay["daily"] == utc_daily,
                  "%s %s (%s): payload.daily is not the engine's UTC rows\n"
                  "  engine: %r\n  screen: %r\n  sent  : %r"
                  % (tag, page, path, utc_daily, got["view_daily"], pay["daily"]))
            check(pay["period_start"] == first and pay["period_end"] == last,
                  "%s %s (%s): payload period %s..%s, engine %s..%s, screen %r"
                  % (tag, page, path, pay["period_start"], pay["period_end"],
                     first, last, got["view_period"]))
            check(pay["totals"]["requests"] == data["totals"]["requests"] and
                  pay["totals"]["in_ttl_losses"] == data["totals"]["in_ttl_losses"] and
                  pay["totals"]["wasted_tokens"] == data["totals"]["wasted_tokens"],
                  "%s %s (%s): payload totals %r != engine totals %r"
                  % (tag, page, path, pay["totals"], data["totals"]))
            # M13: the fingerprint has to leave with the payload. Nothing
            # downstream can catch its absence — /api/submit accepts an
            # anchorless submission and simply appends a second row, which is
            # the double count this milestone removed.
            want = data["probe_identity"]
            check(pay.get("anchors") == want["anchors"],
                  "%s %s (%s): payload.anchors %r, want the machine's %d "
                  "fingerprint hashes. Without them every returning submitter "
                  "opens a new row again."
                  % (tag, page, path, pay.get("anchors"), len(want["anchors"])))
            check(pay.get("token") == want["token"],
                  "%s %s (%s): payload.token %r, want the browser's stored link "
                  "token %r" % (tag, page, path, pay.get("token"), want["token"]))
            # M15: the detector vocabulary leaves with the payload too, and
            # like the fingerprint it describes the MACHINE. Nothing downstream
            # can catch its absence: /api/submit accepts a submission without
            # one and stores a row that simply never reports which reason names
            # it saw, which reads on the front page as "an old client" rather
            # than as a bug here.
            want_vocab = data["detector_vocab"]
            check(pay.get("detector") == want_vocab,
                  "%s %s (%s): payload.detector %r, want the engine's census "
                  "keys %r" % (tag, page, path, pay.get("detector"), want_vocab))
            # Narrowing the period must not touch either: they identify the
            # machine, not the window.
            check(pay.get("anchors") == got["whole"].get("anchors"),
                  "%s %s (%s): the period picker changed the fingerprint"
                  % (tag, page, path))
            check(pay.get("detector") == got["whole"].get("detector"),
                  "%s %s (%s): the period picker changed the detector "
                  "vocabulary — the census covers the whole scan, so narrowing "
                  "the window must not touch it" % (tag, page, path))
        bare = got["bare"]
        check("anchors" not in bare and "token" not in bare,
              "%s %s: a scan with no fingerprint sent one anyway: %r"
              % (tag, page, {k: bare[k] for k in ("anchors", "token") if k in bare}))


def check_page_labels(data):
    """M12.1 invariant 6 (D2): the strings the pages print."""
    for page in PAGES:
        got = data["page_labels"].get(page)
        check(got is not None, "no label probe for %s" % page)
        for key, want in sorted(EXPECTED_PAGE_LABELS[page].items()):
            check(got.get(key) == want,
                  "%s: %s = %r, expected %r" % (page, key, got.get(key), want))
        lead, rest = LEGACY_HM_NOTE[page]
        for zone in ("seoul", "utc"):
            note = got[zone + "_note"]
            check(note["lead"] == lead and note["rest"] == rest,
                  "%s: the %s heatmap note is not the sentence this page "
                  "carried before M12.1\n  want %r\n  got  %r"
                  % (page, zone, lead + rest, note["lead"] + note["rest"]))
        sub = got["kolkata_note"]
        check(sub["lead"] == lead,
              "%s: the sub-hour note changed its opening clause" % page)
        check(SUBHOUR_NOTE_MARK[page] in sub["rest"],
              "%s: a :30 zone is not told what its columns cover: %r"
              % (page, sub["rest"]))
        check(DST_NOTE_MARK[page] not in sub["rest"],
              "%s: a single-offset zone was told it crosses a DST change" % page)
        dst = got["ny_note"]
        check(DST_NOTE_MARK[page] in dst["rest"],
              "%s: a DST-spanning scan is not told its rows carry per-instant "
              "offsets: %r" % (page, dst["rest"]))


def check_unit(data):
    """M12.1 invariant 8: branches no fixture reaches."""
    u = data["unit"]
    check(u["all_zero_row_utc"] == ["2026-08-20", "2026-08-21"],
          "an all-zero census row stopped naming the day it covered (UTC): %r"
          % (u["all_zero_row_utc"],))
    check(u["all_zero_row_seoul"] == ["2026-08-20", "2026-08-22"],
          "an all-zero census row moved wrongly at +9: %r"
          % (u["all_zero_row_seoul"],))
    check(u["offset_at_local_feb"] == -300,
          "offsetAtLocal on a February New York cell = %r, expected -300"
          % (u["offset_at_local_feb"],))
    check(u["offset_at_local_aug"] == -240,
          "offsetAtLocal on an August New York cell = %r, expected -240"
          % (u["offset_at_local_aug"],))
    check(u["offset_at_local_pinned"] == 330,
          "a pinned offset did not answer itself: %r"
          % (u["offset_at_local_pinned"],))
    check(u["offset_at_local_feb"] == u["offset_at_instant_feb"] and
          u["offset_at_local_aug"] == u["offset_at_instant_aug"],
          "the offset a cell is LABELLED with disagrees with the offset its "
          "own instant was PLACED with")


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
    subhour = run_runner(node, FIXTURE_SUBHOUR, OFFSETS)
    main_fx = run_runner(node, FIXTURE_MAIN, OFFSETS)
    hostile = run_runner(node, FIXTURE_HOSTILE, OFFSETS)

    suites = [("fixtures_tz", tz), ("fixtures_tz_subhour", subhour),
              ("fixtures", main_fx), ("fixtures_hostile", hostile)]
    discriminating = []
    for tag, data in suites:
        for fn in (check_sums, check_offset0_identity, check_restored,
                   check_placement):
            try:
                fn(tag, data)
            except CheckFail as exc:
                errors.append(str(exc))
        try:
            check_payload(tag, data, discriminating)
        except CheckFail as exc:
            errors.append(str(exc))
    if not discriminating:
        errors.append("the payload guard never saw a localised view that "
                      "differs from the engine's rows, so it proved nothing")
    else:
        evidence.append("payload guard discriminating on: %s"
                        % ", ".join("%s/%s" % p for p in discriminating))

    for fn in (check_boundary, check_aggregate_only, check_labels,
               check_page_labels, check_unit):
        try:
            fn(tz)
        except CheckFail as exc:
            errors.append(str(exc))

    try:
        check_subhour(subhour)
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
    print("  D1 sub-hour zones (fixtures_tz_subhour), loss cell vs its own usage:")
    for off in (330, 345, 540):
        view = subhour["views"][str(off)]
        rows = census_rows(view)
        cells = ", ".join(
            "%s#%d req=%d loss=1 clock=%s"
            % (e["date"], e["hour"], rows[e["date"]][e["hour"]], e["time"])
            for e in view["events"])
        print("    %-9s %s" % (tz["labels"].get(str(off), str(off)), cells))
    print("  D2 printed labels (pinned 2026 America/New_York):")
    for page in PAGES:
        lab = tz["page_labels"][page]
        print("    %-14s %s | %s | %s"
              % (page, lab["ny_cell_feb"], lab["ny_head_feb"], lab["ny_time_uniform"]))
    print("  D3 payload guard: %s built %s..%s from the engine while the "
          "screen read %s..%s"
          % (PAGES[0], tz["page_payload"][PAGES[0]]["whole"]["period_start"],
             tz["page_payload"][PAGES[0]]["whole"]["period_end"],
             tz["page_payload"][PAGES[0]]["view_period"]["start"],
             tz["page_payload"][PAGES[0]]["view_period"]["end"]))
    print("  host: %s %s" % (tz["host"]["zone"], tz["host"]["detect"]["label"]))
    for line in evidence:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
