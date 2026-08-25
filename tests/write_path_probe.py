#!/usr/bin/env python3
"""Measures two properties of the write path that the contract test asserts but
does not quantify, and prints them as tables (M14.2).

  1. SIMULTANEOUS SUBMISSIONS. N submissions released together by a barrier,
     against the real function under wrangler and the real mock Git Data API.
     Reports accepted / refused and the round trips one submission costs. The
     dataset is validated afterwards, because "they all got a 200" is only half
     the claim — the other half is that what landed still adds up.

  2. A HAND-CORRUPTED DETAIL FILE. Eight malformed shapes for one row of an
     existing data/subs/<id>.json, each followed by a real submission from the
     machine that owns that row. Reports the HTTP status, whether anything was
     committed, and whether the published dataset is valid before and after.
     A row a repo admin broke must not become a dataset the API publishes.

Both reuse tests/submit_contract_test.py's mock and its wrangler boot, so this
measures the same code path the contract test does, at a port of its own.

The mock answers in microseconds and real GitHub does not, so a burst measured
here understates the conflict window by roughly 100x. Set

    CACHE_OBS_MOCK_LATENCY_MS=100

to give every mock call a GitHub-like delay and measure the burst at a realistic
window width. The default of 0 keeps this runnable as a suite.

Exit 0 = every expectation below holds, 1 = one does not, 2 = setup failure.
"""
import json
import math
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Own port: the contract test may be running, and two wranglers on one port
# would silently measure whichever booted first.
os.environ.setdefault("CACHE_OBS_SUBMIT_PORT", "8791")

import submit_contract_test as sct   # noqa: E402  (must follow the port setting)
import dataset_validate              # noqa: E402

MOCK = sct.MOCK
BURST_SIZES = (3, 5, 10)
# A burst of N is N-1 machines racing for one branch, so some loss is inherent.
# The bar is "mostly succeed", not "all succeed": at 10 the old path accepted 2.
MIN_ACCEPT_RATIO = 0.8

SHAPES = [
    ("date key missing", {"requests": 10, "losses": 0, "wasted_tokens": 0}),
    ("row is a number", 5),
    ("row is a string", "2026-05-01"),
    ("row is an array", ["2026-05-01", 10, 0, 0]),
    ("row is a bool", True),
    ("date is not a date",
     {"date": "not-a-date", "requests": 10, "losses": 0, "wasted_tokens": 0}),
    ("date is null",
     {"date": None, "requests": 10, "losses": 0, "wasted_tokens": 0}),
    ("row is null", None),
    ("daily is emptied", sct.EMPTY_DAILY),
]


def latency_ms():
    try:
        return int(os.environ.get("CACHE_OBS_MOCK_LATENCY_MS", "0"))
    except ValueError:
        return 0


def dataset_errors():
    files = sct.repo_files()
    try:
        return dataset_validate.validate(
            json.loads(files[sct.INDEX_PATH]),
            json.loads(files[sct.FLEET_PATH]),
            {p[len("data/subs/"):-len(".json")]: json.loads(t)
             for p, t in files.items()
             if p.startswith("data/subs/") and p.endswith(".json")},
            json.loads(files[sct.IDENTITY_PATH])
            if sct.IDENTITY_PATH in files else None)
    except Exception as exc:                     # a shape the validator cannot walk
        return ["the dataset could not be validated at all: %r" % exc]


def counters():
    with MOCK.lock:
        return {"requests": MOCK.requests, "commits": MOCK.commit_count,
                "patches": MOCK.ref_patches}


# ---------------------------------------------------------------------------
# 1. round trips per submission
# ---------------------------------------------------------------------------

def measure_round_trips():
    with MOCK.lock:
        sct._reset_repo()
    anchors = sct.anchor_set("probe-rt", 16, 0)

    before = counters()["requests"]
    status, data = sct.submit(
        sct.scan_payload("2026-06-01", 3, 100, 2, 500, 1, anchors=anchors),
        "203.0.113.201")
    new_row = counters()["requests"] - before
    if status != 200:
        raise sct.SetupFail("round-trip probe: first submission got %s (%r)"
                            % (status, data))

    before = counters()["requests"]
    status, data = sct.submit(
        sct.scan_payload("2026-06-10", 3, 100, 2, 500, 1, anchors=anchors),
        "203.0.113.201")
    merge = counters()["requests"] - before
    if status != 200 or data.get("merged") is not True:
        raise sct.SetupFail("round-trip probe: merge got %s (%r)" % (status, data))
    return new_row, merge


# ---------------------------------------------------------------------------
# 2. the burst table
# ---------------------------------------------------------------------------

def burst(n, ip_block):
    """n submissions released at the same instant, one machine each."""
    with MOCK.lock:
        sct._reset_repo()
        MOCK.requests = 0
    gate = threading.Barrier(n)
    out = [None] * n

    def one(i):
        payload = sct.scan_payload("2026-07-01", 3, 100 + i, 2, 500, 1,
                                   anchors=sct.anchor_set("burst-%d-%d"
                                                          % (ip_block, i), 16, 0))
        gate.wait()
        started = time.monotonic()
        try:
            status, data = sct.submit(payload, "203.0.113.%d" % (ip_block + i))
        except Exception as exc:                 # a transport failure is a refusal
            status, data = 0, {"error": repr(exc)}
        out[i] = (status, data, time.monotonic() - started)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    wall = time.monotonic() - t0

    accepted = [r for r in out if r and r[0] == 200]
    refused = [r for r in out if not r or r[0] != 200]
    by_status = {}
    for r in refused:
        key = "%s %s" % (r[0], (r[1] or {}).get("error", "?"))
        by_status[key] = by_status.get(key, 0) + 1
    with MOCK.lock:
        requests = MOCK.requests
        commits = MOCK.commit_count
    return {
        "n": n, "accepted": len(accepted), "refused": len(refused),
        "refusals": by_status, "wall": wall, "requests": requests,
        "commits": commits,
        "slowest": max((r[2] for r in out if r), default=0.0),
        "errors": dataset_errors(),
    }


# ---------------------------------------------------------------------------
# 3. the malformed-detail-row table
# ---------------------------------------------------------------------------

def shape_case(name, shape, ip):
    with MOCK.lock:
        sct._reset_repo()
    anchors = sct.anchor_set("shape-" + name, 16, 0)
    status, data = sct.submit(
        sct.scan_payload("2026-05-01", 2, 10, 0, 0, 0, anchors=anchors), ip)
    if status != 200:
        raise sct.SetupFail("shape %r: the base submission got %s (%r)"
                            % (name, status, data))
    sct.corrupt_detail_row(data["id"], shape)
    pre = dataset_errors()
    before = counters()
    status, data = sct.submit(
        sct.scan_payload("2026-05-20", 2, 10, 0, 0, 0, anchors=anchors), ip)
    after = counters()
    return {
        "name": name, "status": status,
        "body": (data or {}).get("error", "" if status == 200 else "?"),
        "committed": after["commits"] - before["commits"],
        "pre_errors": pre, "post_errors": dataset_errors(),
    }


# ---------------------------------------------------------------------------

def report(rt, bursts, shapes):
    lag = latency_ms()
    print("")
    print("round trips per submission (one at a time, no contention)")
    print("  new row : %d" % rt[0])
    print("  merge   : %d" % rt[1])
    print("")
    print("simultaneous submissions   (mock latency %d ms/call)" % lag)
    print("  %-12s %-9s %-8s %s" % ("simultaneous", "accepted", "refused",
                                    "refusals"))
    for b in bursts:
        print("  %-12d %-9d %-8d %s"
              % (b["n"], b["accepted"], b["refused"],
                 ", ".join("%s x%d" % (k, v)
                           for k, v in sorted(b["refusals"].items())) or "-"))
    print("")
    for b in bursts:
        print("  n=%-3d wall %5.1fs  slowest submission %5.1fs  "
              "%d GitHub calls  %d commits  dataset: %s"
              % (b["n"], b["wall"], b["slowest"], b["requests"], b["commits"],
                 "valid" if not b["errors"] else
                 "INVALID (%s)" % "; ".join(b["errors"][:2])))
    print("")
    print("a hand-corrupted row in an existing detail file")
    print("  %-20s %-8s %-10s %-9s %s"
          % ("shape", "status", "error", "committed", "dataset violations"))
    for s in shapes:
        # before -> after. The admin's own edit is a violation and stays one;
        # what must not happen is the API ADDING one of its own on top, which
        # is exactly what "the fleet series carries a day no submission covers"
        # was before M14.2.
        print("  %-20s %-8s %-10s %-9d %d -> %d  %s"
              % (s["name"], s["status"], s["body"], s["committed"],
                 len(s["pre_errors"]), len(s["post_errors"]),
                 "(unchanged: the hand-edit, nothing added)"
                 if len(s["post_errors"]) <= len(s["pre_errors"])
                 else "ADDED: %s" % s["post_errors"][-1]))
    print("")


def judge(bursts, shapes):
    bad = []
    for b in bursts:
        want = int(math.ceil(b["n"] * MIN_ACCEPT_RATIO))
        if b["accepted"] < want:
            bad.append("burst of %d accepted only %d (want >= %d)"
                       % (b["n"], b["accepted"], want))
        if b["errors"]:
            bad.append("burst of %d left an invalid dataset: %s"
                       % (b["n"], b["errors"][0]))
    for s in shapes:
        if s["status"] == 200:
            bad.append("shape %r was accepted (HTTP 200) over a corrupted "
                       "detail file" % s["name"])
        elif s["status"] >= 500 and s["status"] != 502:
            bad.append("shape %r answered HTTP %s, not the documented 502"
                       % (s["name"], s["status"]))
        if s["committed"]:
            bad.append("shape %r still committed %d time(s)"
                       % (s["name"], s["committed"]))
        # The admin's own edit stays broken until a human fixes it; what must
        # not happen is the API adding a violation of its own on top.
        if len(s["post_errors"]) > len(s["pre_errors"]):
            bad.append("shape %r left MORE violations than it found "
                       "(%d -> %d): %s" % (s["name"], len(s["pre_errors"]),
                                           len(s["post_errors"]),
                                           s["post_errors"][0]))
    return bad


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if sct.port_in_use(sct.PORT):
        print("FATAL: port %d already in use" % sct.PORT)
        return 2
    npx = sct.find_npx()
    if npx is None:
        print("FATAL: npx not found (node.js install required)")
        return 2

    with MOCK.lock:
        sct._reset_repo()
        MOCK.latency_ms = latency_ms()
    server, mock_port = sct.start_mock()
    scratch = sct.make_scratch()
    proc = log = log_path = None
    code = 1
    try:
        proc, log, log_path = sct.start_wrangler(npx, mock_port, scratch)
        print("wrangler pages dev starting on %s …" % sct.BASE)
        sct.wait_ready(proc, log_path)
        print("wrangler ready — probing")

        rt = measure_round_trips()
        bursts = [burst(n, 20 + 30 * i) for i, n in enumerate(BURST_SIZES)]
        shapes = [shape_case(name, shape, "198.51.100.%d" % (60 + i))
                  for i, (name, shape) in enumerate(SHAPES)]
        report(rt, bursts, shapes)
        bad = judge(bursts, shapes)
        if bad:
            print("PROBE_FAIL: %d expectation(s) not met" % len(bad))
            for b in bad:
                print("  - " + b)
            code = 1
        else:
            print("PROBE_OK")
            code = 0
    except sct.SetupFail as exc:
        print("FATAL(setup): %s" % exc)
        code = 2
    except sct.ContractFail as exc:
        print("PROBE_FAIL: %s" % exc)
        code = 1
    finally:
        if proc is not None:
            sct.stop_wrangler(proc, log)
        server.shutdown()
        server.server_close()
        for _ in range(5):
            try:
                sct.guarded_delete_scratch(scratch)
                break
            except OSError:
                time.sleep(1)
    return code


if __name__ == "__main__":
    sys.exit(main())
