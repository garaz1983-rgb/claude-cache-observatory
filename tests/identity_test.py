#!/usr/bin/env python3
"""Identity test for the machine fingerprint (M13, assets/identity.js).

The observatory adds every row in data/submissions.json together, so a person
who submits twice used to be counted twice. It happened for real on
2026-08-24: 188,174 requests and 343 losses on screen for a machine that had
153,623 and 228. The fix is a per-machine pseudonym derived from the logs
themselves, and this file is where its four claims are held:

  1. IT IDENTIFIES THE MACHINE, NOT THE SCAN. Two different scans of the same
     log folder must share at least one anchor, and a scan of a DIFFERENT
     machine must share none. Both directions are asserted; only the second
     one is absolute.

  2. NOTHING REVERSIBLE LEAVES. What the page sends are sha-256 digests. The
     requestIds they were computed from are grepped for in the output, and the
     digest of a pinned id is recomputed here independently — the prefix and
     the hash are a contract with functions/api/submit.js, not an
     implementation detail.

  3. THE SAMPLE IS THE DOCUMENTED ONE. HEAD_COUNT earliest records plus an even
     spread of bucket midpoints across the whole scan, ordered by instant. The
     positions are recomputed here from the module's own constants rather than
     copied, so shrinking the head or dropping the spread fails.

  4. IT IS DETERMINISTIC. The same logs, split into different files and handed
     over in a different order, sample the same records.

The overlap NUMBERS are pinned, not just bounded. They are the measurement
this milestone rests on, and the honest one is unflattering: appending a week
of logs keeps the 8 head anchors and loses all 8 spread anchors, and rotating
30% of the folder away leaves a single coincidental match. That is the drift
the design accepts (the server refreshes the stored set on every update, so
the chain survives as long as submissions are not months apart), and pinning
it means a change in either direction is visible rather than discovered later.

Exit 0 all-pass, exit 1 on a mismatch, exit 2 on setup failure (node missing).
Node + Python stdlib only; no network, nothing outside site/tests is touched.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
FIXTURE_DIR = os.path.join(HERE, "fixtures")
RUNNER = os.path.join(HERE, "run_identity.js")

HEX64 = re.compile(r"[0-9a-f]{64}")

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]

# Pinned measurements. See the module docstring: these are observations of a
# controlled churn, not properties the design guarantees.
EXPECTED_OVERLAP = {
    "append": 8,    # +100 newer records: the whole head survives, no spread does
    "trimmed": 6,   # the 3 oldest deleted: 6 of the 8 head anchors survive
    "churned": 1,   # 150 oldest deleted + 150 appended: one coincidental match
    "other": 0,     # a different machine: never, under any circumstances
    "shuffled": 16, # the same logs, different files, different order: identical
}


class CheckFail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise CheckFail(msg)


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


def js_round(x):
    """JavaScript Math.round: half away from zero upward, not banker's."""
    return int(math.floor(x + 0.5))


def expected_positions(n, anchor_count, head_count):
    """The sample identity.js documents, recomputed rather than copied."""
    if n <= anchor_count:
        return list(range(n))
    out = []
    seen = set()

    def take(i):
        i = max(0, min(n - 1, i))
        if i not in seen:
            seen.add(i)
            out.append(i)

    head = min(head_count, n)
    for i in range(head):
        take(i)
    spread = anchor_count - head
    for j in range(spread):
        take(js_round((j + 0.5) * n / spread))
    return out


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

    proc = subprocess.run([node, RUNNER, FIXTURE_DIR], capture_output=True,
                          text=True, encoding="utf-8", timeout=180, cwd=SITE_DIR)
    if proc.returncode != 0:
        print("FATAL: run_identity.js failed: %s" % proc.stderr.strip())
        return 2
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        print("FATAL: run_identity.js printed no JSON (%s): %.400s" % (exc, proc.stdout))
        return 2

    try:
        anchor_count = out["anchor_count"]
        head_count = out["head_count"]
        base_ids = out["base_ids"]
        scans = out["scans"]

        # ---------- 2. nothing reversible leaves ----------
        check(out["anchor_prefix"] == "cco.anchor.v1|",
              "the anchor prefix changed to %r; functions/api/submit.js and every "
              "already-stored fingerprint assume the old one" % out["anchor_prefix"])
        want_digest = hashlib.sha256(
            ("cco.anchor.v1|" + out["pinned"]["id"]).encode("utf-8")).hexdigest()
        check(out["pinned"]["digest"] == want_digest,
              "the anchor digest is not sha256(prefix + requestId): got %s, want %s"
              % (out["pinned"]["digest"], want_digest))
        # The module's own async path, not a re-implementation of it.
        check(out["module_pinned"] == want_digest,
              "identity.anchorsOf() produced %s for the pinned id; sha256(prefix + id) "
              "is %s. functions/api/submit.js compares against digests already stored "
              "under the old rule, so this cannot change quietly."
              % (out["module_pinned"], want_digest))
        mod = out["module_fingerprint"]
        check(mod["anchors"] == scans["base"]["anchors"],
              "identity.fingerprint() disagrees with the same sample hashed "
              "independently\n  module: %r\n  ours  : %r"
              % (mod["anchors"][:2], scans["base"]["anchors"][:2]))
        check(mod["count"] == len(base_ids) and mod["sampled"] == len(mod["anchors"]),
              "identity.fingerprint() miscounted: %r" % mod)
        blob = json.dumps({k: v["anchors"] for k, v in scans.items()})
        leaked = [r for r in base_ids if r in blob]
        check(not leaked, "a requestId reached the anchors: %r" % leaked[:3])
        print("PASS digests only: prefix pinned, sha-256 verified independently, "
              "0 of %d requestIds present in the output" % len(base_ids))

        # ---------- 3. the sample is the documented one ----------
        for name, scan in sorted(scans.items()):
            anchors = scan["anchors"]
            check(len(anchors) <= anchor_count,
                  "%s: %d anchors, over the %d cap" % (name, len(anchors), anchor_count))
            check(len(set(anchors)) == len(anchors), "%s: the anchors repeat" % name)
            for h in anchors:
                check(HEX64.fullmatch(h), "%s: %r is not a lowercase sha-256 digest"
                      % (name, h))
            check(len(scan["sampled"]) == len(anchors),
                  "%s: %d records sampled but %d anchors sent"
                  % (name, len(scan["sampled"]), len(anchors)))

        idx = {rid: i for i, rid in enumerate(base_ids)}
        got_positions = [idx[r] for r in scans["base"]["sampled"]]
        want_positions = expected_positions(len(base_ids), anchor_count, head_count)
        check(got_positions == want_positions,
              "the sample is not HEAD_COUNT earliest + an even spread\n"
              "  got  %r\n  want %r" % (got_positions, want_positions))
        check(got_positions[:head_count] == list(range(head_count)),
              "the head is not the earliest %d records: %r"
              % (head_count, got_positions[:head_count]))
        check(max(got_positions) < len(base_ids) - 1,
              "the sample rests on the newest record, which by definition did not "
              "exist at the previous submission")
        print("PASS sample shape: %d earliest + %d evenly spaced over %d records "
              "at positions %r" % (head_count, anchor_count - head_count,
                                   len(base_ids), got_positions[head_count:]))

        # ---------- 4. deterministic ----------
        check(scans["shuffled"]["sampled"] == scans["base"]["sampled"],
              "the same logs in different files/order sampled different records")
        print("PASS deterministic: 500 records split 5 ways and 7 ways, reversed, "
              "sample identically")

        # ---------- 1. it identifies the machine ----------
        got = out["overlaps"]
        check(got["other"] == 0,
              "a DIFFERENT machine shares %d anchors — the fingerprint would merge "
              "two strangers into one row" % got["other"])
        check(got["append"] >= 1,
              "a scan with a week of newer logs no longer matches the earlier one "
              "(overlap %d): every returning submitter would open a second row"
              % got["append"])
        check(got["trimmed"] >= 1,
              "deleting the 3 oldest records broke the link (overlap %d)"
              % got["trimmed"])
        for name, want in sorted(EXPECTED_OVERLAP.items()):
            check(got[name] == want,
                  "overlap(%s) = %d, the pinned measurement is %d. If the sampler "
                  "changed on purpose, re-measure and update EXPECTED_OVERLAP "
                  "together with the drift note in README.md."
                  % (name, got[name], want))
        print("PASS identity: append %d/16, 3-oldest-deleted %d/16, "
              "30%%-rotated %d/16, different machine %d/16"
              % (got["append"], got["trimmed"], got["churned"], got["other"]))

        # ---------- edges ----------
        e = out["edges"]
        check(e["tiny_ids"] == 5 and e["tiny_sampled"] == 5,
              "a scan smaller than the cap must send every id it has: %r" % e)
        check(e["no_timestamp"] == [],
              "a record with no orderable timestamp was sampled: %r" % e["no_timestamp"])
        check(e["msg_id_only"] == ["msg_fallback_1"],
              "the message.id fallback did not fire: %r" % e["msg_id_only"])
        check(e["no_usage"] == [],
              "a record outside the engine's usage prefilter was sampled: %r"
              % e["no_usage"])
        check(e["offset_order"] == ["req_kst", "req_utc"],
              "ordering is not by instant: a +09:00 stamp four hours earlier must "
              "sort first, got %r" % e["offset_order"])
        check(e["empty"] == [] and e["junk"] == [],
              "empty or unparsable input did not yield an empty sample: %r" % e)
        base_anchors = scans["base"]["anchors"]
        check(e["sanitize"] == [base_anchors[0], base_anchors[1]],
              "sanitizeAnchors let a malformed value through: %r" % e["sanitize"])
        print("PASS edges: no timestamp, message.id fallback, no usage block, "
              "mixed offsets, empty and unparsable input, malformed anchors")

        # ---------- the real fixtures ----------
        fx = out["fixtures"]
        check(fx["engine_requests"] is not None and fx["engine_requests"] > 0,
              "fixture sanity: the engine counted no requests")
        check(len(fx["ids"]) == fx["engine_requests"],
              "collect() found %d request ids in the fixtures but the engine "
              "counted %d requests: the fingerprint is not drawn from the same "
              "request set the engine dedupes on"
              % (len(fx["ids"]), fx["engine_requests"]))
        print("PASS fixture parity: %d request ids, %d engine requests"
              % (len(fx["ids"]), fx["engine_requests"]))

        # ---------- M16: streaming must sample what the array path sampled ----
        # The page no longer hands this module an array of file texts. It opens
        # a collector and feeds it the lines assets/parse.js is already walking.
        # The engine strips a trailing \r from each line and collect() does not,
        # so the CRLF set is the one that would catch a divergence.
        streaming = out.get("streaming")
        check(isinstance(streaming, dict) and streaming,
              "no streaming probe in the runner output")
        for key in sorted(streaming):
            row = streaming[key]
            check(row.get("equal") is True,
                  "🔴 streaming(%s): the engine-fed collector produced %r ids "
                  "and collect() produced %r. A different sample means a "
                  "returning submitter stops matching their own row."
                  % (key, row.get("n_engine"), row.get("n_files")))
            check(row.get("anchors_equal") is True,
                  "🔴 streaming(%s): same ids, different anchors" % key)
        print("PASS streaming equivalence on %s: the engine-fed collector "
              "samples exactly what collect() samples"
              % ", ".join(sorted(streaming)))
    except CheckFail as exc:
        print("IDENTITY_FAIL: %s" % exc)
        return 1

    print("IDENTITY_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
