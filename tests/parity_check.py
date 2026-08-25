#!/usr/bin/env python3
"""Parity check: assets/parse.js vs scripts/check_cache_loss.py (v2.1 SSOT).

Feeds the same fixture set (tests/fixtures/) to both engines:

  (a) the CLI script, unmodified, pointed at the fixtures by overriding the
      home directory (USERPROFILE/HOME) of a subprocess — a wrapper around
      its hardcoded ~/.claude/projects root, no source edit;
  (b) parse.js via node (tests/run_parse.js).

Compares:
  - totals: requests / in_ttl_losses / iron_losses / wasted_tokens
    against the CLI TOTAL row (req / inTTL_loss / iron<5m / inTTL_wasted);
  - daily[]: rolled up to months against the CLI's monthly rows
    (req / inTTL_loss / inTTL_wasted) — the CLI's finest granularity;
  - daily[] self-consistency: per-day sums equal totals, dates sorted
    and unique.

Also runs parse.js alone on tests/fixtures_hostile/ (malformed cc values a
hostile JSONL could carry — the CLI cannot ingest these, a string cc would
crash its sums) and holds it to fixed coerced expectations.

Also runs BOTH engines over tests/fixtures_detector/ (the M15 detector census)
and compares the whole `detector` block byte for byte, plus its two internal
invariants. The fixture contains a loss the judgment rules cannot see because
the server renamed the reason: if the census ever stops reporting it, the
page goes back to printing a confident zero, and this check fails.

Exit 0 on full match, exit 1 with a diff otherwise. Never touches the real
~/.claude/projects. Python stdlib + node only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
FIXTURE_DIR = os.path.join(HERE, "fixtures")
HOSTILE_DIR = os.path.join(HERE, "fixtures_hostile")
CLI_SCRIPT = os.path.join(SITE_DIR, "scripts", "check_cache_loss.py")
RUNNER = os.path.join(HERE, "run_parse.js")

# fixtures_hostile/: 2 requests, one in-TTL PMNF loss whose
# cache_creation_input_tokens is the STRING "777" — must coerce to 0.
HOSTILE_EXPECTED = {"requests": 2, "in_ttl_losses": 1, "iron_losses": 0,
                    "wasted_tokens": 0}

DETECTOR_DIR = os.path.join(HERE, "fixtures_detector")

# Hand-computed from tests/fixtures_detector/main_detector.jsonl, so that a
# bug present in BOTH engines still fails this check. Agreement alone would
# not catch a shared mistake.
#   8 deduped requests; only 2 are judged losses. The third real one wears a
#   renamed reason ("cache_entry_not_found") and is invisible to the rules -
#   that gap is the entire point of the census.
DETECTOR_EXPECTED = {
    "diagnosed_requests": 6,
    "unknown_reasons": 4,
    "cold_writes": 4,
    "reasons": {
        "(invalid)": 2,                    # a number, and an empty string
        "previous_message_not_found": 2,   # plain form + {"type": ...} form
        "brand_new_reason": 1,             # arrived by back-fill, later record
        "cache_entry_not_found": 1,        # the renamed loss
    },
    "versions": {"2.2.0": 5, "2.1.0": 2, "(none)": 1},
}
DETECTOR_TOTALS = {"requests": 8, "in_ttl_losses": 2, "iron_losses": 2,
                   "wasted_tokens": 12000}

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]


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


def run_cli(tmp_root, fixture_dir=FIXTURE_DIR, json_mode=False):
    """Run the unmodified CLI script against a copy of the fixtures."""
    home = os.path.join(tmp_root, "home")
    projects = os.path.join(home, ".claude", "projects")
    shutil.copytree(fixture_dir, os.path.join(projects, "synthetic_fixtures"))
    env = dict(os.environ)
    env["USERPROFILE"] = home  # ntpath.expanduser (Windows)
    env["HOME"] = home         # posixpath.expanduser
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    proc = subprocess.run(
        [sys.executable, CLI_SCRIPT] + (["--json"] if json_mode else []),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    if proc.returncode != 0:
        print("parity_check: CLI script failed (exit %d)" % proc.returncode)
        print(proc.stderr)
        sys.exit(1)
    if json_mode:
        try:
            return json.loads(proc.stdout)
        except ValueError:
            print("parity_check: CLI --json produced non-JSON output")
            print(proc.stdout[:2000])
            sys.exit(1)
    return parse_cli_output(proc.stdout)


def check_detector(node, errors):
    """Both engines over the detector fixture: identical blocks, and both
    equal to the hand-computed expectation."""
    with tempfile.TemporaryDirectory() as tmp_root:
        cli = run_cli(tmp_root, DETECTOR_DIR, json_mode=True)
    js = run_js(node, DETECTOR_DIR)

    for label, doc in (("js", js), ("cli", cli)):
        det = doc.get("detector")
        if not isinstance(det, dict):
            errors.append("detector: %s emitted no detector block" % label)
            continue
        if det != DETECTOR_EXPECTED:
            errors.append("detector(%s): %s != expected %s"
                          % (label, json.dumps(det, sort_keys=True),
                             json.dumps(DETECTOR_EXPECTED, sort_keys=True)))
        # Invariants that must hold for ANY input, not just this fixture.
        reason_sum = sum((det.get("reasons") or {}).values())
        if reason_sum != det.get("diagnosed_requests"):
            errors.append("detector(%s): sum(reasons)=%r != diagnosed_requests=%r"
                          % (label, reason_sum, det.get("diagnosed_requests")))
        version_sum = sum((det.get("versions") or {}).values())
        want_requests = (doc.get("totals") or {}).get("requests")
        if version_sum != want_requests:
            errors.append("detector(%s): sum(versions)=%r != totals.requests=%r"
                          % (label, version_sum, want_requests))

    if js.get("detector") != cli.get("detector"):
        errors.append("detector: js and cli blocks differ")

    for key, want in sorted(DETECTOR_TOTALS.items()):
        for label, doc in (("js", js), ("cli", cli)):
            got = (doc.get("totals") or {}).get(key)
            if got != want:
                errors.append("detector fixture totals.%s(%s): %r != %r"
                              % (key, label, got, want))


def parse_cli_output(out):
    """Parse the CLI's monthly table + TOTAL row into dicts of ints."""
    months = {}
    total = None
    for line in out.splitlines():
        toks = line.split()
        if len(toks) != 9:
            continue
        head = toks[0]
        if head != "TOTAL" and not re.fullmatch(r"\d{4}-\d{2}", head):
            continue
        try:
            nums = [int(t.replace(",", ""))
                    for t in (toks[1], toks[2], toks[3], toks[4], toks[5], toks[7], toks[8])]
        except ValueError:
            continue  # header line
        row = {
            "requests": nums[0],
            "cache_write": nums[1],
            "pmnf_raw": nums[2],
            "in_ttl_losses": nums[3],
            "wasted_tokens": nums[4],
            "iron_losses": nums[5],
            "iron_wasted": nums[6],
        }
        if head == "TOTAL":
            total = row
        else:
            months[head] = row
    if total is None:
        print("parity_check: could not find TOTAL row in CLI output:")
        print(out)
        sys.exit(1)
    return months, total


def run_js(node, fixture_dir=FIXTURE_DIR):
    proc = subprocess.run(
        [node, RUNNER, fixture_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        # A crashing engine is a mismatch by definition (mutation -> KILLED).
        print("parity_check: parse.js run failed (exit %d)" % proc.returncode)
        print(proc.stderr)
        sys.exit(1)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        print("parity_check: parse.js produced non-JSON output")
        print(proc.stdout[:2000])
        sys.exit(1)


def main():
    node = find_node()
    if node is None:
        print("parity_check: node executable not found "
              "(set CACHE_OBS_NODE or add node to PATH)")
        return 1

    with tempfile.TemporaryDirectory() as tmp_root:
        cli_months, cli_total = run_cli(tmp_root)
    js = run_js(node)

    errors = []
    totals = js.get("totals") or {}
    expected_totals = {
        "requests": cli_total["requests"],
        "in_ttl_losses": cli_total["in_ttl_losses"],
        "iron_losses": cli_total["iron_losses"],
        "wasted_tokens": cli_total["wasted_tokens"],
    }
    for key, want in sorted(expected_totals.items()):
        got = totals.get(key)
        if got != want:
            errors.append("totals.%s: js=%r cli=%r" % (key, got, want))

    daily = js.get("daily") or []
    rollup = {}
    day_sums = {"requests": 0, "losses": 0, "wasted_tokens": 0}
    dates = []
    for day in daily:
        date = str(day.get("date", ""))
        dates.append(date)
        month = date[:7]
        acc = rollup.setdefault(
            month, {"requests": 0, "losses": 0, "wasted_tokens": 0})
        for key in ("requests", "losses", "wasted_tokens"):
            value = day.get(key)
            if not isinstance(value, int):
                errors.append("daily[%s].%s: not an int: %r" % (date, key, value))
                value = 0
            acc[key] += value
            day_sums[key] += value

    if dates != sorted(dates) or len(set(dates)) != len(dates):
        errors.append("daily dates not sorted/unique: %r" % dates)

    if set(rollup) != set(cli_months):
        errors.append("month set: js=%r cli=%r"
                      % (sorted(rollup), sorted(cli_months)))
    for month in sorted(set(rollup) & set(cli_months)):
        got = rollup[month]
        want = cli_months[month]
        pairs = [("requests", "requests"),
                 ("losses", "in_ttl_losses"),
                 ("wasted_tokens", "wasted_tokens")]
        for js_key, cli_key in pairs:
            if got[js_key] != want[cli_key]:
                errors.append("month %s %s: js=%r cli=%r"
                              % (month, js_key, got[js_key], want[cli_key]))

    consistency = [("requests", "requests"),
                   ("losses", "in_ttl_losses"),
                   ("wasted_tokens", "wasted_tokens")]
    for day_key, total_key in consistency:
        if day_sums[day_key] != totals.get(total_key):
            errors.append("daily sum %s=%r != totals.%s=%r"
                          % (day_key, day_sums[day_key],
                             total_key, totals.get(total_key)))

    # JS-only hostile fixtures: malformed cc must coerce to 0, never leak
    # a non-int into totals (the DOM XSS vector the coercion closes).
    hostile_totals = run_js(node, HOSTILE_DIR).get("totals") or {}
    for key, want in sorted(HOSTILE_EXPECTED.items()):
        got = hostile_totals.get(key)
        if got != want or not isinstance(got, int):
            errors.append("hostile totals.%s: js=%r want=%r" % (key, got, want))

    check_detector(node, errors)

    if errors:
        print("PARITY_FAIL (%d diff%s):" % (len(errors), "s" if len(errors) > 1 else ""))
        for err in errors:
            print("  - " + err)
        return 1

    print("PARITY_OK totals=%s months=%s detector=agreed"
          % (json.dumps(totals, sort_keys=True), sorted(rollup)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
