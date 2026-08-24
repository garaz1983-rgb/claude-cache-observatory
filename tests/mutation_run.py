#!/usr/bin/env python3
"""Mutation harness for the judgment logic in assets/parse.js.

For each mutation: apply a single-anchor source edit to parse.js, run
tests/parity_check.py (which diffs parse.js against the unmodified CLI
script on the shared fixtures), judge by exit code only, then restore
parse.js from git.

Preconditions (all fatal if unmet):
  - node found at an absolute path and `node --version` exits 0;
  - `git -C <site> diff --quiet` passes (work must be committed first);
  - every mutation anchor occurs exactly once in parse.js.

Verdicts: parity exit != 0 -> KILLED, exit 0 -> SURVIVED.
All KILLED -> prints MUT_ALL_DEFENDED as the last line, exit 0.
Any SURVIVED -> lists them, exit 1. Setup failures exit 2.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
PARSE_JS = os.path.join(SITE_DIR, "assets", "parse.js")
PARSE_JS_REL = "assets/parse.js"
PARITY = os.path.join(HERE, "parity_check.py")

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]

# (id, anchor — must occur exactly once in parse.js, replacement)
MUTATIONS = [
    ("M01_main_ttl_shrunk",
     "var MAIN_TTL_SECONDS = 1800;",
     "var MAIN_TTL_SECONDS = 300;"),
    ("M02_main_ttl_doubled",
     "var MAIN_TTL_SECONDS = 1800;",
     "var MAIN_TTL_SECONDS = 3600;"),
    ("M03_sub_ttl_widened",
     "var SUBAGENT_TTL_SECONDS = 300;",
     "var SUBAGENT_TTL_SECONDS = 1800;"),
    ("M04_iron_widened",
     "var IRON_SECONDS = 300;",
     "var IRON_SECONDS = 600;"),
    ("M05_ttl_branch_swap",
     "isSub ? SUBAGENT_TTL_SECONDS : MAIN_TTL_SECONDS",
     "isSub ? MAIN_TTL_SECONDS : SUBAGENT_TTL_SECONDS"),
    ("M06_in_ttl_boundary_lte",
     "var inTtl = gap < ttlSeconds;",
     "var inTtl = gap <= ttlSeconds;"),
    ("M07_iron_boundary_lte",
     "if (gap < IRON_SECONDS) {",
     "if (gap <= IRON_SECONDS) {"),
    ("M08_pmnf_string_changed",
     '"previous_message_not_found"',
     '"previous_message_not_found_x"'),
    ("M09_pmnf_check_inverted",
     "if (r.rtype !== PMNF_REASON) continue;",
     "if (r.rtype === PMNF_REASON) continue;"),
    ("M10_backfill_disabled",
     "if (prev && prev.rtype === null && rtype) {",
     "if (false && prev && rtype) {"),
    ("M11_backfill_overwrites",
     "if (prev && prev.rtype === null && rtype) {",
     "if (prev && rtype) {"),
    ("M12_gap_sign_flipped",
     "(r.epochMs - reqs[i - 1].epochMs) / 1000",
     "(reqs[i - 1].epochMs - r.epochMs) / 1000"),
    ("M13_gap_first_guard_off",
     "var gap = i > 0 ?",
     "var gap = i >= 0 ?"),
    ("M14_dedup_disabled",
     "if (seen.has(rid)) {",
     "if (false && seen.has(rid)) {"),
    ("M15_daily_wasted_dropped",
     "day.wasted_tokens += r.cc;",
     "day.wasted_tokens += 0;"),
]


def read_parse_js():
    with open(PARSE_JS, "rb") as fh:
        return fh.read().decode("utf-8")


def write_parse_js(src):
    with open(PARSE_JS, "wb") as fh:
        fh.write(src.encode("utf-8"))


def probe_node():
    """Absolute-path node probe: --version must exit 0."""
    candidates = []
    which = shutil.which("node")
    if which:
        candidates.append(os.path.abspath(which))
    candidates.extend(NODE_FALLBACKS)
    for cand in candidates:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            proc = subprocess.run([cand, "--version"],
                                  capture_output=True, text=True, timeout=30)
        except OSError:
            continue
        if proc.returncode == 0:
            return cand
    return None


def git_tree_clean():
    proc = subprocess.run(["git", "-C", SITE_DIR, "diff", "--quiet"],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def restore_parse_js(original_src):
    proc = subprocess.run(
        ["git", "-C", SITE_DIR, "checkout", "--", PARSE_JS_REL],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print("FATAL: git checkout restore failed: %s" % proc.stderr.strip())
        return False
    if read_parse_js() != original_src:
        print("FATAL: parse.js differs from original after restore")
        return False
    return True


def main():
    node = probe_node()
    if node is None:
        print("FATAL: node not found or --version probe failed")
        return 2
    print("node: %s" % node)

    if not git_tree_clean():
        print("FATAL: `git -C %s diff --quiet` failed — commit the site "
              "tree before running mutations" % SITE_DIR)
        return 2

    src = read_parse_js()

    anchor_errors = []
    for mut_id, anchor, _ in MUTATIONS:
        count = src.count(anchor)
        if count != 1:
            anchor_errors.append("%s: anchor occurs %d times (want 1): %r"
                                 % (mut_id, count, anchor))
    if anchor_errors:
        print("FATAL: anchor pre-scan failed:")
        for err in anchor_errors:
            print("  - " + err)
        return 2
    print("anchors: %d/%d verified (each exactly once)"
          % (len(MUTATIONS), len(MUTATIONS)))

    env = dict(os.environ)
    env["CACHE_OBS_NODE"] = node

    # Baseline: the unmutated engine must pass parity, otherwise every
    # verdict below would be meaningless.
    base = subprocess.run([sys.executable, PARITY], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=300)
    if base.returncode != 0:
        print("FATAL: baseline parity_check failed (exit %d) — fix parity "
              "before mutating" % base.returncode)
        print(base.stdout)
        return 2
    print("baseline: parity_check exit 0")

    survived = []
    killed = 0
    for mut_id, anchor, replacement in MUTATIONS:
        mutated = src.replace(anchor, replacement, 1)
        if mutated == src:
            print("FATAL: mutation %s produced no change" % mut_id)
            return 2
        write_parse_js(mutated)
        try:
            proc = subprocess.run([sys.executable, PARITY],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  env=env, timeout=300)
            verdict = "KILLED" if proc.returncode != 0 else "SURVIVED"
        finally:
            if not restore_parse_js(src):
                return 2
        print("%-8s %s" % (verdict, mut_id))
        if verdict == "SURVIVED":
            survived.append(mut_id)
        else:
            killed += 1

    print("mutations: %d total / %d KILLED / %d SURVIVED"
          % (len(MUTATIONS), killed, len(survived)))
    if survived:
        print("SURVIVED mutations:")
        for mut_id in survived:
            print("  - " + mut_id)
        return 1
    print("MUT_ALL_DEFENDED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
