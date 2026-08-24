#!/usr/bin/env python3
"""Storage test for the opt-in local save (M10).

check.html can keep a diagnosis in the browser's own localStorage so a later
run starts after the last submitted day instead of re-sending a period that is
already on the observatory. That feature only earns its place if three things
hold, and this test is where they are held:

  1. SAVE/RESTORE IS LOSSLESS. What comes back out of storage equals what went
     in: totals, daily rows, hourly census, per-event detail, submissions.

  2. FORBIDDEN FIELDS CANNOT ENTER THE STORE. The engine's own event records
     carry `file` (a real log path), `requestId` and the raw `timestamp`.
     assets/store.js rebuilds every record from a whitelist, so none of those
     may appear in the serialised object. This is asserted by grepping the
     stored JSON for strings lifted out of the actual fixture files, not by
     trusting a key list.

  3. THE DATE ARITHMETIC IS RIGHT AT THE BOUNDARIES. The incremental default
     is the day AFTER the last submitted period_end (month ends, year ends and
     the 2028 leap day included), and overlap detection counts a submission
     that ends on the selected start day as one overlapping day while a
     selection starting the next day overlaps by zero.

Plus the failure modes that decide whether the page stays usable: a storage
that throws on every access (private window, blocked site data) must disable
the feature silently rather than raise, a quota rejection must degrade to a
smaller record instead of losing the save, and clear() must actually empty it.

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
RUNNER = os.path.join(HERE, "run_storage.js")

MAX_PERIOD_DAYS = 92

# Every key the stored object is allowed to carry, per level.
RUN_KEYS = {"saved_at", "source", "script_version", "period_start", "period_end",
            "totals", "daily", "census", "events", "events_saved"}
TOTALS_KEYS = {"requests", "in_ttl_losses", "iron_losses", "wasted_tokens"}
DAILY_KEYS = {"date", "requests", "losses", "wasted_tokens"}
CENSUS_KEYS = {"date", "hours"}
EVENT_KEYS = {"date", "hour", "time", "gap_min", "tokens", "main"}
SUBMISSION_KEYS = {"period_start", "period_end", "submitted_at", "id"}

# Key names that must never appear anywhere in the serialised state.
BANNED_KEYS = {"file", "requestId", "request_id", "sessionId", "session_id",
               "uuid", "cwd", "timestamp", "message", "content", "text",
               "usage", "nickname", "classification", "is_subagent",
               "cache_creation_tokens", "gap_seconds"}

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]


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


def day(iso):
    return datetime.date(int(iso[0:4]), int(iso[5:7]), int(iso[8:10]))


def shift(iso, n):
    return (day(iso) + datetime.timedelta(days=n)).isoformat()


def span_days(a, b):
    return (day(b) - day(a)).days + 1


def walk_keys(node, path="$"):
    """Yield (path, key) for every object key in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield (path, k)
            for item in walk_keys(v, path + "." + k):
                yield item
    elif isinstance(node, list):
        for i, v in enumerate(node):
            for item in walk_keys(v, path + "[%d]" % i):
                yield item


def fixture_secrets():
    """Strings that exist in the real fixtures and must NOT reach storage.

    Deliberately taken from the files rather than hardcoded: if a fixture is
    replaced, the leak test keeps testing the new contents.
    """
    ids, stamps = set(), set()
    for base, _dirs, files in os.walk(FIXTURE_DIR):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(base, name), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(o.get("requestId"), str):
                        ids.add(o["requestId"])
                    if isinstance(o.get("timestamp"), str):
                        stamps.add(o["timestamp"])
    return ids, stamps


def expected_overlap(start, end, submissions):
    """Recompute the overlap independently: a set of calendar days."""
    lo, hi = (start, end) if start <= end else (end, start)
    days = set()
    for s in submissions:
        a, b = s["period_start"], s["period_end"]
        if a > b:
            a, b = b, a
        a = max(a, lo)
        b = min(b, hi)
        d = day(a)
        while d <= day(b):
            days.add(d.isoformat())
            d += datetime.timedelta(days=1)
    return len(days)


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

    # --- boundary cases for the incremental default ------------------------
    # (name, last submitted period_end, scan dates, today)
    inc_cases = [
        ("plain", "2026-08-10", [shift("2026-08-08", i) for i in range(10)], "2026-08-17"),
        ("month-end", "2026-01-31", [shift("2026-01-29", i) for i in range(8)], "2026-02-05"),
        ("year-end", "2025-12-31", [shift("2025-12-29", i) for i in range(8)], "2026-01-05"),
        ("leap-day", "2028-02-28", [shift("2028-02-26", i) for i in range(8)], "2028-03-04"),
        ("nothing-new", "2026-08-20", [shift("2026-08-10", i) for i in range(11)], "2026-08-24"),
        ("submitted-today", "2026-08-24", [shift("2026-08-20", i) for i in range(5)], "2026-08-24"),
        ("over-cap", "2026-01-01", [shift("2026-01-01", i) for i in range(200)], "2026-07-20"),
    ]
    increments = [{
        "dates": dates,
        "submissions": [{"period_start": shift(end, -5), "period_end": end,
                         "submitted_at": "2026-08-24T00:00:00Z", "id": "x"}],
        "today": today,
        "maxDays": MAX_PERIOD_DAYS,
    } for (_n, end, dates, today) in inc_cases]
    # No submission recorded yet: there is no increment to propose.
    increments.append({"dates": [shift("2026-08-01", i) for i in range(5)],
                       "submissions": [], "today": "2026-08-10",
                       "maxDays": MAX_PERIOD_DAYS})

    # --- boundary cases for overlap ---------------------------------------
    sub = lambda a, b: {"period_start": a, "period_end": b,
                        "submitted_at": "2026-08-01T00:00:00Z", "id": "s"}
    overlap_cases = [
        # name, selected start/end, submissions, expected overlapping days
        ("touch-on-end", "2026-01-10", "2026-01-20", [sub("2026-01-01", "2026-01-10")], 1),
        ("starts-next-day", "2026-01-11", "2026-01-20", [sub("2026-01-01", "2026-01-10")], 0),
        ("ends-day-before", "2026-01-01", "2026-01-09", [sub("2026-01-10", "2026-01-20")], 0),
        ("touch-on-start", "2026-01-01", "2026-01-10", [sub("2026-01-10", "2026-01-20")], 1),
        ("contained", "2026-01-05", "2026-01-08", [sub("2026-01-01", "2026-01-20")], 4),
        ("contains", "2026-01-01", "2026-01-31", [sub("2026-01-10", "2026-01-12")], 3),
        ("identical", "2026-01-01", "2026-01-31", [sub("2026-01-01", "2026-01-31")], 31),
        ("disjoint", "2026-03-01", "2026-03-10", [sub("2026-01-01", "2026-01-31")], 0),
        ("two-subs-merge", "2026-01-01", "2026-01-31",
         [sub("2026-01-05", "2026-01-15"), sub("2026-01-10", "2026-01-20")], 16),
        ("two-subs-apart", "2026-01-01", "2026-01-31",
         [sub("2026-01-05", "2026-01-06"), sub("2026-01-20", "2026-01-21")], 4),
        ("month-boundary", "2026-03-01", "2026-03-05", [sub("2026-02-25", "2026-03-01")], 1),
        ("leap-day", "2028-02-29", "2028-03-05", [sub("2028-02-20", "2028-02-29")], 1),
    ]
    overlaps = [{"start": a, "end": b, "submissions": subs}
                for (_n, a, b, subs, _e) in overlap_cases]

    next_days = ["2026-01-31", "2026-02-28", "2028-02-28", "2028-02-29",
                 "2025-12-31", "2026-08-24"]

    job = {"increments": increments, "overlaps": overlaps, "nextDays": next_days}
    proc = subprocess.run(
        [node, RUNNER, FIXTURE_DIR, json.dumps(job)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=SITE_DIR)
    if proc.returncode != 0:
        print("FATAL: run_storage.js failed: %s" % proc.stderr.strip())
        return 2
    out = json.loads(proc.stdout)

    try:
        run = out["run"]
        check(run is not None, "buildRun returned nothing for the fixtures")

        # ---------- 1. round trip ----------
        check(out["saved_ok"] is True, "save() reported failure on a working storage")
        rt = out["roundtrip"]
        check(rt is not None, "load() returned nothing after a successful save")
        check(len(rt["runs"]) == 1, "expected exactly one stored run, got %d" % len(rt["runs"]))
        check(rt["runs"][0] == run,
              "round trip changed the record:\nsaved %r\nread  %r" % (run, rt["runs"][0]))
        check(run["totals"] == out["totals"],
              "stored totals %r != engine totals %r" % (run["totals"], out["totals"]))
        check(run["daily"] == out["daily"],
              "stored daily rows differ from the engine's")
        check(run["period_start"] == out["daily"][0]["date"] and
              run["period_end"] == out["daily"][-1]["date"],
              "stored period bounds do not match the daily rows")
        engine_losses = sum(1 for e in out["raw_events"]
                            if e["classification"] in ("in_ttl", "iron"))
        check(engine_losses > 0, "fixture sanity: no in-TTL events in the sample")
        print("PASS round trip: %d daily rows, %d census rows, %d events preserved exactly"
              % (len(run["daily"]), len(run["census"] or []), len(run["events"] or [])))

        # ---------- 2. forbidden fields ----------
        check(set(run.keys()) == RUN_KEYS,
              "run carries unexpected keys: %r" % (set(run.keys()) ^ RUN_KEYS))
        check(set(run["totals"].keys()) == TOTALS_KEYS, "totals keys drifted")
        for d in run["daily"]:
            check(set(d.keys()) == DAILY_KEYS, "daily row keys drifted: %r" % set(d.keys()))
        for c in (run["census"] or []):
            check(set(c.keys()) == CENSUS_KEYS, "census row keys drifted: %r" % set(c.keys()))
            check(len(c["hours"]) == 24, "census row %s is not 24 hours" % c["date"])
        check(run["events"], "fixture sanity: no per-event detail stored, the leak test is blind")
        for e in run["events"]:
            check(set(e.keys()) == EVENT_KEYS, "event keys drifted: %r" % set(e.keys()))
            check(re.fullmatch(r"\d{2}:\d{2}:\d{2}", e["time"]),
                  "event time %r is not a bare time of day" % e["time"])
            check(0 <= e["hour"] <= 23, "event hour out of range: %r" % e["hour"])

        state2 = json.loads(out["state2_json"])
        for path, key in walk_keys(state2):
            check(key not in BANNED_KEYS,
                  "banned key %r reached storage at %s" % (key, path))
        subs = out["state2_submissions"]
        check(len(subs) == 1, "expected 1 recorded submission, got %d" % len(subs))
        check(set(subs[0].keys()) == SUBMISSION_KEYS,
              "submission keys drifted: %r" % set(subs[0].keys()))
        check(subs[0]["id"] == "sub-20260825-abcd", "the server id was not kept")

        blob = out["state2_json"]
        req_ids, stamps = fixture_secrets()
        check(req_ids, "fixture sanity: no requestIds found, the leak test is blind")
        leaked = [r for r in req_ids if r in blob]
        check(not leaked, "requestId leaked into storage: %r" % leaked[:3])
        leaked_ts = [t for t in stamps if t in blob]
        check(not leaked_ts, "raw timestamp leaked into storage: %r" % leaked_ts[:3])
        for frag in (".jsonl", "subagents/", "\\\\", "/home/", "C:\\\\", "secret"):
            check(frag not in blob, "path fragment %r leaked into storage" % frag)
        engine_files = set(e["file"] for e in out["raw_events"] if e.get("file"))
        check(engine_files, "fixture sanity: engine events carry no file paths")
        for f in engine_files:
            check(f not in blob, "engine file path %r leaked into storage" % f)
        print("PASS no forbidden field reached storage "
              "(%d requestIds, %d timestamps, %d file paths checked against the blob)"
              % (len(req_ids), len(stamps), len(engine_files)))

        # ---------- hydrate keeps the iron recount possible ----------
        hy = out["hydrated_iron"]
        engine_iron = out["totals"]["iron_losses"]
        check(hy["iron_events"] == engine_iron,
              "hydrate rebuilt %d iron events, engine counted %d"
              % (hy["iron_events"], engine_iron))
        check(hy["census"] is True, "hydrate lost the census")
        print("PASS hydrate: %d iron events reconstructed from the stored gaps" % engine_iron)

        # ---------- comparison ----------
        cmp_ = out["compare"]
        check(cmp_ is not None, "compareRuns returned nothing for two stored runs")
        check(cmp_["losses_delta"] == cmp_["losses_cur"] - cmp_["losses_prev"],
              "compareRuns losses_delta is not cur - prev")
        print("PASS compareRuns on two saved runs (%d -> %d losses)"
              % (cmp_["losses_prev"], cmp_["losses_cur"]))

        # ---------- 3. incremental boundaries ----------
        for (name, end, dates, today), got in zip(inc_cases, out["increments"]):
            want_start = shift(end, 1)
            fresh = [d for d in dates if d >= want_start]
            if not fresh:
                check(got["has_new_data"] is False,
                      "%s: expected no new data past %s, got %r" % (name, end, got))
                check(got["start"] is None, "%s: proposed a start with no new data" % name)
            else:
                # Expected start: the day after the last period_end, unless
                # that window is wider than the API cap, in which case the
                # widest window ending on the newest day that still fits.
                want = fresh[0]
                clamped = span_days(fresh[0], fresh[-1]) > MAX_PERIOD_DAYS
                if clamped:
                    want = next(d for d in fresh
                                if span_days(d, fresh[-1]) <= MAX_PERIOD_DAYS)
                check(got["has_new_data"] is True, "%s: expected new data, got %r" % (name, got))
                check(got["start"] == want,
                      "%s: start %r != expected %r (first day after %s%s)"
                      % (name, got["start"], want, end, ", clamped" if clamped else ""))
                check(got["clamped"] is clamped,
                      "%s: clamped flag %r, expected %r" % (name, got["clamped"], clamped))
                check(got["end"] == fresh[-1],
                      "%s: end %r != newest scanned day %r" % (name, got["end"], fresh[-1]))
                check(span_days(got["start"], got["end"]) <= MAX_PERIOD_DAYS,
                      "%s: proposed window spans %d days, over the %d cap"
                      % (name, span_days(got["start"], got["end"]), MAX_PERIOD_DAYS))
                if not clamped:
                    check(got["start"] == shift(end, 1) or got["start"] > shift(end, 1),
                          "%s: start %r is not after %s" % (name, got["start"], end))
            check(got["last_end"] == end, "%s: last_end %r != %r" % (name, got["last_end"], end))
            want_since = max(0, span_days(end, today) - 1)
            check(got["since_days"] == want_since,
                  "%s: since_days %r != %d" % (name, got["since_days"], want_since))
        over = out["increments"][6]
        check(over["clamped"] is True,
              "over-cap: a 200-day scan was not clamped (%r)" % over)
        check(span_days(over["start"], over["end"]) <= MAX_PERIOD_DAYS,
              "over-cap: clamped window still exceeds the cap")
        check(out["increments"][-1] is None,
              "with no submission recorded there must be no incremental proposal")
        print("PASS incremental default = day after the last period_end "
              "on %d cases (month end, year end, leap day, over-cap, nothing-new)"
              % len(inc_cases))

        for iso, got in zip(next_days, out["next_days"]):
            want = shift(iso, 1)
            check(got == want, "nextDay(%s) = %r, expected %r" % (iso, got, want))
        print("PASS nextDay on %d boundary dates" % len(next_days))

        # ---------- 4. overlap boundaries ----------
        for (name, a, b, subs_in, want_days), got in zip(overlap_cases, out["overlaps"]):
            indep = expected_overlap(a, b, subs_in)
            check(want_days == indep,
                  "%s: the test's own expectation (%d) disagrees with the day walk (%d)"
                  % (name, want_days, indep))
            check(got["days"] == want_days,
                  "%s: overlapWith reported %d days, expected %d (%r)"
                  % (name, got["days"], want_days, got))
            total = sum(span_days(s["start"], s["end"]) for s in got["spans"])
            check(total == got["days"],
                  "%s: reported spans cover %d days but days=%d (spans overlap?)"
                  % (name, total, got["days"]))
            for s in got["spans"]:
                check(min(a, b) <= s["start"] <= s["end"] <= max(a, b),
                      "%s: span %r falls outside the selected period" % (name, s))
        print("PASS overlap on %d boundary cases (same-day end = 1, next-day start = 0)"
              % len(overlap_cases))

        # ---------- failure modes ----------
        th = out["throwing"]
        check(th.get("threw") is not True,
              "a storage that throws propagated the error out of store.js: %r" % th)
        check(th["available"] is False, "available() was true on a throwing storage")
        check(th["load"] is None, "load() returned data from a throwing storage")
        check(th["save"] is False, "save() claimed success on a throwing storage")
        check(th["clear"] is False, "clear() claimed success on a throwing storage")
        print("PASS blocked storage: no exception escapes, every call reports failure")

        q = out["quota"]
        check(q["saved"] is True, "a quota rejection lost the save entirely")
        check(q["has_daily"] is True, "the degraded save dropped the daily rows")
        check(q["events_saved"] is False,
              "the degraded save kept per-event detail it could not fit (%r)" % q)
        check(q["tiny_saved"] is False,
              "a quota nothing fits into still reported a successful save")
        check(q["tiny_back"] is None,
              "a failed save left a partial record behind: %r" % q["tiny_back"])
        print("PASS quota: the save degrades to aggregates, and reports failure "
              "when nothing fits at all")

        c = out["clear"]
        check(c["before"] is True, "setup: nothing was stored before the clear")
        check(c["returned"] is True, "clear() reported failure on a working storage")
        check(c["after"] is None, "clear() left data behind: %r" % c["after"])
        print("PASS clear: the stored state is gone immediately")
    except CheckFail as exc:
        print("STORAGE_FAIL: %s" % exc)
        return 1

    print("STORAGE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
