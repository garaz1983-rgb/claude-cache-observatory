#!/usr/bin/env python3
"""Mutation harness for the site's judgment + submission-validation logic.

Targets:
  - assets/parse.js          — judged by tests/parity_check.py (M01..M15, M22)
  - functions/api/submit.js  — judged by tests/submit_contract_test.py
                               (S16..S21, S23..S24)
  - assets/localtime.js      — judged by tests/localtime_test.py (L25..L28)
  - check.html, ko/check.html — judged by tests/localtime_test.py (C29..C30)

M12.1 added the last two targets. Before it, no anchor reached check.html at
all, and a mutant that built the submission payload out of the localised view —
the one thing M12 exists to forbid — survived every suite while producing a
KST-cut submission the API would have accepted.

For each mutation: apply a single-anchor source edit to the target file, run
that mutation's test runner, judge by exit code, then restore the target from
git.

Preconditions (all fatal if unmet):
  - node found at an absolute path and `node --version` exits 0;
  - `git -C <site> diff --quiet` passes (work must be committed first);
  - every mutation anchor occurs exactly once in its target.

Verdicts: runner exit 0 -> SURVIVED, exit 1 -> KILLED,
exit >= 2 -> FATAL setup failure (aborts the whole run — an infra problem
must never be reported as a defended mutation).
All KILLED -> prints MUT_ALL_DEFENDED as the last line, exit 0.
Any SURVIVED -> lists them, exit 1. Setup failures exit 2.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
PARITY = os.path.join(HERE, "parity_check.py")
CONTRACT = os.path.join(HERE, "submit_contract_test.py")
LOCALTIME = os.path.join(HERE, "localtime_test.py")

PARSE_JS_REL = "assets/parse.js"
SUBMIT_JS_REL = "functions/api/submit.js"
LOCALTIME_JS_REL = "assets/localtime.js"
CHECK_HTML_REL = "check.html"
CHECK_HTML_KO_REL = "ko/check.html"

NODE_FALLBACKS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/usr/bin/node",
]

RUNNERS = {
    # name: (script, per-run timeout seconds)
    "parity": (PARITY, 300),
    "contract": (CONTRACT, 900),
    "localtime": (LOCALTIME, 600),
}

# (id, target file relative to site/, anchor — must occur exactly once in the
#  target, replacement, runner)
MUTATIONS = [
    # -- assets/parse.js: judgment rules (parity vs the CLI SSOT) -----------
    ("M01_main_ttl_shrunk", PARSE_JS_REL,
     "var MAIN_TTL_SECONDS = 1800;",
     "var MAIN_TTL_SECONDS = 300;", "parity"),
    ("M02_main_ttl_doubled", PARSE_JS_REL,
     "var MAIN_TTL_SECONDS = 1800;",
     "var MAIN_TTL_SECONDS = 3600;", "parity"),
    ("M03_sub_ttl_widened", PARSE_JS_REL,
     "var SUBAGENT_TTL_SECONDS = 300;",
     "var SUBAGENT_TTL_SECONDS = 1800;", "parity"),
    ("M04_iron_widened", PARSE_JS_REL,
     "var IRON_SECONDS = 300;",
     "var IRON_SECONDS = 600;", "parity"),
    ("M05_ttl_branch_swap", PARSE_JS_REL,
     "isSub ? SUBAGENT_TTL_SECONDS : MAIN_TTL_SECONDS",
     "isSub ? MAIN_TTL_SECONDS : SUBAGENT_TTL_SECONDS", "parity"),
    ("M06_in_ttl_boundary_lte", PARSE_JS_REL,
     "var inTtl = gap < ttlSeconds;",
     "var inTtl = gap <= ttlSeconds;", "parity"),
    ("M07_iron_boundary_lte", PARSE_JS_REL,
     "if (gap < IRON_SECONDS) {",
     "if (gap <= IRON_SECONDS) {", "parity"),
    ("M08_pmnf_string_changed", PARSE_JS_REL,
     '"previous_message_not_found"',
     '"previous_message_not_found_x"', "parity"),
    ("M09_pmnf_check_inverted", PARSE_JS_REL,
     "if (r.rtype !== PMNF_REASON) continue;",
     "if (r.rtype === PMNF_REASON) continue;", "parity"),
    ("M10_backfill_disabled", PARSE_JS_REL,
     "if (prev && prev.rtype === null && rtype) {",
     "if (false && prev && rtype) {", "parity"),
    ("M11_backfill_overwrites", PARSE_JS_REL,
     "if (prev && prev.rtype === null && rtype) {",
     "if (prev && rtype) {", "parity"),
    ("M12_gap_sign_flipped", PARSE_JS_REL,
     "(r.epochMs - reqs[i - 1].epochMs) / 1000",
     "(reqs[i - 1].epochMs - r.epochMs) / 1000", "parity"),
    ("M13_gap_first_guard_off", PARSE_JS_REL,
     "var gap = i > 0 ?",
     "var gap = i >= 0 ?", "parity"),
    ("M14_dedup_disabled", PARSE_JS_REL,
     "if (seen.has(rid)) {",
     "if (false && seen.has(rid)) {", "parity"),
    ("M15_daily_wasted_dropped", PARSE_JS_REL,
     "day.wasted_tokens += r.cc;",
     "day.wasted_tokens += 0;", "parity"),

    # -- functions/api/submit.js: validation branches (submit contract) -----
    ("S16_losses_le_requests_flipped", SUBMIT_JS_REL,
     "if (t.in_ttl_losses > t.requests) {",
     "if (t.in_ttl_losses < t.requests) {", "contract"),
    ("S17_period_span_widened", SUBMIT_JS_REL,
     "if (spanDays > MAX_PERIOD_DAYS) {",
     "if (spanDays > MAX_PERIOD_DAYS + 1) {", "contract"),
    # The pre-check comparison mutant (count >= -> count >) became equivalent
    # once the post-increment re-check landed (the re-check still 429s the
    # 4th submit), so the limit CONSTANT is mutated instead — that relaxes
    # both checks at once and stays observable.
    ("S18_rate_limit_relaxed", SUBMIT_JS_REL,
     "const RATE_LIMIT_MAX = 3;",
     "const RATE_LIMIT_MAX = 4;", "contract"),
    ("S19_unknown_field_check_disabled", SUBMIT_JS_REL,
     'errors.push("undefined field: " + key);',
     "void key;", "contract"),
    ("S20_nickname_length_widened", SUBMIT_JS_REL,
     "nickname.length > MAX_NICKNAME",
     "nickname.length > MAX_NICKNAME + 1", "contract"),
    ("S21_sha_retry_disabled", SUBMIT_JS_REL,
     "if (put.status !== 409) break;",
     "if (put.status === 409) break;", "contract"),

    # -- M3.1 codex-review fixes ------------------------------------------
    ("M22_cc_coercion_removed", PARSE_JS_REL,
     'if (typeof cc !== "number" || !isFinite(cc) || cc < 0) cc = 0;',
     "cc = cc || 0;", "parity"),
    ("S23_daily_sum_check_inverted", SUBMIT_JS_REL,
     "if (sumRequests !== t.requests) {",
     "if (sumRequests === t.requests) {", "contract"),
    ("S24_daily_empty_allowed", SUBMIT_JS_REL,
     "if (body.daily.length === 0) {",
     "if (false) {", "contract"),

    # -- M12.1: the local-time view (assets/localtime.js) -------------------
    # L28 is the M12 defect itself: place an event by its exact instant instead
    # of by the cell its own UTC hour was attributed to. Whole-hour zones cannot
    # tell the difference, which is exactly why it shipped.
    ("L25_cellms_end_of_hour", LOCALTIME_JS_REL,
     'return stampMs(dateKey, pad2(h) + ":00:00");',
     'return stampMs(dateKey, pad2(h) + ":59:59");', "localtime"),
    ("L26_bucketms_offset_dropped", LOCALTIME_JS_REL,
     "return epochMs + offsetMinutes * 60000;",
     "return epochMs;", "localtime"),
    ("L27_allzero_row_dropped", LOCALTIME_JS_REL,
     "bucketOf(p0 ? p0.date : row.date);",
     "void p0;", "localtime"),
    ("L28_event_placed_by_instant", LOCALTIME_JS_REL,
     "var cell = u ? partsAt(cellMs(u.date, u.hour), offsetAt) : null;",
     "var cell = p;", "localtime"),
    ("L29_offset_frozen_at_load", LOCALTIME_JS_REL,
     "var settled = res(guess - off * 60000);",
     "var settled = res(Date.now());", "localtime"),

    # -- M12.1: the pages' payload builder (D3) ----------------------------
    # The exact regression M12 forbids: build the submission out of the
    # localised screen. sum(daily) still equals totals and the period still
    # matches the daily range, so /api/submit ACCEPTS it — nothing downstream
    # can catch this, which is why the guard has to sit here.
    ("C30_payload_reads_localised_view", CHECK_HTML_REL,
     "  var r = LAST.result;",
     "  var r = VIEW && VIEW.localized ? { totals: LAST.result.totals,"
     " daily: VIEW.daily, events: LAST.result.events } : LAST.result;",
     "localtime"),
    ("C31_payload_reads_localised_view_ko", CHECK_HTML_KO_REL,
     "  var r = LAST.result;",
     "  var r = VIEW && VIEW.localized ? { totals: LAST.result.totals,"
     " daily: VIEW.daily, events: LAST.result.events } : LAST.result;",
     "localtime"),

    # -- M12.1: the pages' labels (D2) -------------------------------------
    # Print the offset detected once at page load instead of the offset at the
    # instant being labelled — the measured "13:02:00 UTC-4" over an 18:02Z
    # instant.
    ("C32_label_offset_frozen_at_load", CHECK_HTML_REL,
     "  var off = ObservatoryLocalTime.offsetAtLocal(dateKey, hour, VIEW ? VIEW.offsetAt : undefined);\n"
     "  return typeof off === \"number\" ? off : ZONE.offsetMinutes;",
     "  return ZONE.offsetMinutes;", "localtime"),
    ("C33_label_offset_frozen_at_load_ko", CHECK_HTML_KO_REL,
     "  var off = ObservatoryLocalTime.offsetAtLocal(dateKey, hour, VIEW ? VIEW.offsetAt : undefined);\n"
     "  return typeof off === \"number\" ? off : ZONE.offsetMinutes;",
     "  return ZONE.offsetMinutes;", "localtime"),
]


def target_abs(rel):
    return os.path.join(SITE_DIR, rel.replace("/", os.sep))


def read_target(rel):
    with open(target_abs(rel), "rb") as fh:
        return fh.read().decode("utf-8")


def write_target(rel, src):
    with open(target_abs(rel), "wb") as fh:
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


def restore_target(rel, original_src):
    proc = subprocess.run(
        ["git", "-C", SITE_DIR, "checkout", "--", rel],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print("FATAL: git checkout restore of %s failed: %s"
              % (rel, proc.stderr.strip()))
        return False
    if read_target(rel) != original_src:
        print("FATAL: %s differs from original after restore" % rel)
        return False
    return True


def run_runner(runner, env):
    script, timeout = RUNNERS[runner]
    proc = subprocess.run([sys.executable, script], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout)
    return proc


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

    sources = {}
    for rel in {rel for _, rel, _, _, _ in MUTATIONS}:
        sources[rel] = read_target(rel)

    anchor_errors = []
    for mut_id, rel, anchor, _, _ in MUTATIONS:
        count = sources[rel].count(anchor)
        if count != 1:
            anchor_errors.append("%s: anchor occurs %d times in %s (want 1): %r"
                                 % (mut_id, count, rel, anchor))
    if anchor_errors:
        print("FATAL: anchor pre-scan failed:")
        for err in anchor_errors:
            print("  - " + err)
        return 2
    print("anchors: %d/%d verified (each exactly once)"
          % (len(MUTATIONS), len(MUTATIONS)))

    env = dict(os.environ)
    env["CACHE_OBS_NODE"] = node

    # Baseline: every runner must pass on the unmutated tree, otherwise the
    # verdicts below would be meaningless.
    for runner in sorted({runner for _, _, _, _, runner in MUTATIONS}):
        base = run_runner(runner, env)
        if base.returncode != 0:
            print("FATAL: baseline %s failed (exit %d) — fix it before mutating"
                  % (runner, base.returncode))
            print(base.stdout)
            return 2
        print("baseline: %s exit 0" % runner)

    survived = []
    killed = 0
    for mut_id, rel, anchor, replacement, runner in MUTATIONS:
        src = sources[rel]
        mutated = src.replace(anchor, replacement, 1)
        if mutated == src:
            print("FATAL: mutation %s produced no change" % mut_id)
            return 2
        write_target(rel, mutated)
        try:
            proc = run_runner(runner, env)
        finally:
            if not restore_target(rel, src):
                return 2
        if proc.returncode == 0:
            verdict = "SURVIVED"
        elif proc.returncode == 1:
            verdict = "KILLED"
        else:
            print("FATAL: %s runner infra failure under %s (exit %d):"
                  % (runner, mut_id, proc.returncode))
            print(proc.stdout[-3000:])
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
