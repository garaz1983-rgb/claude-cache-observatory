#!/usr/bin/env python3
"""Mutation harness for the site's judgment + submission-validation logic.

Targets:
  - assets/parse.js          — judged by tests/parity_check.py (M01..M15, M22)
  - functions/api/submit.js  — judged by tests/submit_contract_test.py
                               (S16..S21, S23..S24, S39..S47, S52..S62)
  - assets/localtime.js      — judged by tests/localtime_test.py (L25..L28)
  - check.html, ko/check.html — judged by tests/localtime_test.py
                               (C29..C30, C48..C51)
  - assets/identity.js       — judged by tests/identity_test.py (I34..I38)

M14 added the S52..S62 block. Those guard the split of the public dataset into
an index, a fleet-wide daily series and one detail file per submission, written
as ONE commit through the Git Data API. Two properties there have no production
check behind them: nothing recomputes the fleet series from scratch on the live
path (it is a delta, on purpose — recomputing it would restore the cost the
milestone removed), and nothing re-reads a commit to confirm all three files
were in it. Both therefore have to be proven by mutants that die.

M13 added the identity target and the S39..S47 block. Those two groups guard an
AUTHORISATION surface that did not exist before: matchIndex decides which
existing row a submission may rewrite, and mergeRecord decides what survives
that rewrite. An authorisation check that never runs is worse than none, so
every branch of both is mutated here — including the two that make the public
file safe to publish (the anchors are hashed a SECOND time before storage, and
what a client sends is never compared against itself).

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
IDENTITY = os.path.join(HERE, "identity_test.py")

PARSE_JS_REL = "assets/parse.js"
SUBMIT_JS_REL = "functions/api/submit.js"
LOCALTIME_JS_REL = "assets/localtime.js"
IDENTITY_JS_REL = "assets/identity.js"
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
    "identity": (IDENTITY, 300),
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
    # M14 moved storage onto the Git Data API, so the single retry is no longer
    # a 409 on a PUT but a 422 on the ref update. Same property, new anchor:
    # a branch that moved under the read must earn one re-read, and giving up
    # instead loses the submission.
    ("S21_ref_retry_disabled", SUBMIT_JS_REL,
     "if (written.moved) continue;",
     "if (written.moved) break;", "contract"),

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

    # -- M13: the machine fingerprint (assets/identity.js) ------------------
    # The sample's shape IS the guarantee. Only-the-oldest breaks on a log
    # cleanup, only-the-newest cannot match anything that came before, and a
    # sample ordered by anything but the record's own instant is not "the
    # earliest" at all.
    ("I34_head_dropped", IDENTITY_JS_REL,
     "for (var i = 0; i < head; i++) take(i);",
     "void head;", "identity"),
    ("I35_spread_dropped", IDENTITY_JS_REL,
     "for (var j = 0; j < spread; j++) take(Math.round((j + 0.5) * n / spread));",
     "void spread;", "identity"),
    ("I36_order_not_by_instant", IDENTITY_JS_REL,
     "if (a.ms !== b.ms) return a.ms - b.ms;",
     "if (false) return a.ms - b.ms;", "identity"),
    ("I37_anchor_prefix_dropped", IDENTITY_JS_REL,
     "return sha256Hex(ANCHOR_PREFIX + id);",
     "return sha256Hex(id);", "identity"),
    ("I38_head_count_shrunk", IDENTITY_JS_REL,
     "var HEAD_COUNT = 8;      // of those, how many are the earliest records",
     "var HEAD_COUNT = 1;      // of those, how many are the earliest records",
     "identity"),

    # -- M13: identity resolution and the merge (functions/api/submit.js) ---
    # S40 and S42 are the security pair. S40 makes the match accept anything
    # that has an anchor at all; S42 stores what the client sent instead of a
    # hash of it, which would turn every value in the public dataset into an
    # overwrite key for the row it sits in. Both must die on case13.
    ("S39_identity_match_disabled", SUBMIT_JS_REL,
     "if (want.has(have[j])) return i;",
     "if (false) return i;", "contract"),
    ("S40_identity_matches_anything", SUBMIT_JS_REL,
     "const want = new Set(anchorHashes);",
     "const want = { has: function () { return true; } };", "contract"),
    ("S41_token_match_disabled", SUBMIT_JS_REL,
     "if (storedTokenHash(subs[i]) === tokenHash) return i;",
     "if (false) return i;", "contract"),
    ("S42_anchor_stored_unhashed", SUBMIT_JS_REL,
     "anchorHashes.push(await sha256Hex(ANCHOR_STORE_PREFIX + anchors[i]));",
     "anchorHashes.push(anchors[i]);", "contract"),
    ("S43_merge_drops_existing_days", SUBMIT_JS_REL,
     "for (let i = 0; i < exDaily.length; i++) {",
     "for (let i = 0; i < 0; i++) {", "contract"),
    ("S44_merge_keeps_stale_day", SUBMIT_JS_REL,
     "byDate.set(row.date, { date: row.date, requests: row.requests,",
     "if (!prev) byDate.set(row.date, { date: row.date, requests: row.requests,",
     "contract"),
    ("S45_merge_period_narrows", SUBMIT_JS_REL,
     "    period_start: starts[0],",
     "    period_start: incoming.period_start,", "contract"),
    ("S46_merge_totals_not_recomputed", SUBMIT_JS_REL,
     "totals.requests += daily[i].requests;",
     "totals.requests += 0;", "contract"),
    # 🔴 The rule the user chose this design for: the folder-scan path is never
    # asked to store anything, so a fingerprinted submission must not be handed
    # a token. This is the single place that decides it.
    ("S47_token_forced_on_folder_path", SUBMIT_JS_REL,
     "  if (ident.anchorHashes.length === 0) {",
     "  if (true) {", "contract"),

    # -- M13: the pages still attach what keeps a submitter to one row -------
    # Nothing downstream can catch a page that stops sending its fingerprint:
    # /api/submit accepts an anchorless submission and appends a second row,
    # which is exactly the double count this milestone removed. Same reason
    # C30/C31 exist for the UTC provenance of the same block.
    ("C48_payload_drops_anchors", CHECK_HTML_REL,
     "  if(Array.isArray(LAST.anchors) && LAST.anchors.length) payload.anchors = LAST.anchors.slice(0, 16);",
     "  void 0;", "localtime"),
    ("C49_payload_drops_anchors_ko", CHECK_HTML_KO_REL,
     "  if(Array.isArray(LAST.anchors) && LAST.anchors.length) payload.anchors = LAST.anchors.slice(0, 16);",
     "  void 0;", "localtime"),
    # -- M14: three files, one commit (functions/api/submit.js) --------------
    # This block guards the two things the split bought and the one thing it
    # put at risk. S52/S53 are the split itself: a submission that quietly stops
    # writing one of the three files leaves a dataset whose files disagree, and
    # every one of those disagreements is a number on the page that no longer
    # adds up. S54/S55/S56 are the fleet series, which is maintained as a DELTA
    # and is therefore the one number on the site that can drift silently:
    # nothing recomputes it from scratch in production, so the mutants that make
    # it drift have to die in the test. S61/S62 are the atomicity: a commit that
    # is not chained to the tree and the head the read came from is not "one
    # commit" at all, it is a race with a nicer name.
    ("S52_detail_file_not_written", SUBMIT_JS_REL,
     "      { path: detailPath(id), text: serializeDetail(buildDetail(id, fields)) }",
     '      { path: "data/.mutant", text: "x\\n" }', "contract"),
    ("S53_fleet_file_not_written", SUBMIT_JS_REL,
     "text: serializeFleet(applyFleetDelta(state.fleet, previousDaily, fields.daily)) },",
     'text: "x\\n" },', "contract"),
    ("S54_fleet_delta_not_subtracted", SUBMIT_JS_REL,
     "    cur.requests -= r.requests;",
     "    cur.requests -= 0;", "contract"),
    ("S55_fleet_machines_not_counted", SUBMIT_JS_REL,
     "    cur.machines += 1;",
     "    cur.machines += 0;", "contract"),
    # NOT mutated: the `machines <= 0` drop in applyFleetDelta. The merge unions
    # dates and never removes one, so that branch cannot be reached by any
    # sequence of submissions — mutating it would be an equivalent mutant, and
    # a mutation list that carries one teaches the next reader to tolerate
    # survivors. It stays in the source as a guard on a hand-edited file, and
    # tests/dataset_validate.py is what would catch it going wrong.
    # 🔴 The rule that makes a merge safe now that the history lives one file
    # away: no daily rows, no merge. Merging against rows that failed to load
    # recomputes the row's totals from the incoming submission alone and
    # deletes that machine's history from a public file.
    ("S57_merge_without_history", SUBMIT_JS_REL,
     "      if (previousDaily === null) return { ok: false };",
     "      if (previousDaily === null) previousDaily = [];", "contract"),
    ("S58_detail_totals_zeroed", SUBMIT_JS_REL,
     "    totals: fields.totals,",
     "    totals: { requests: 0, in_ttl_losses: 0, iron_losses: 0, wasted_tokens: 0 },",
     "contract"),
    ("S59_index_row_keeps_daily", SUBMIT_JS_REL,
     "  rec.daily_days = fields.daily.length;",
     "  rec.daily_days = fields.daily.length; rec.daily = fields.daily;", "contract"),
    ("S61_commit_parent_dropped", SUBMIT_JS_REL,
     "parents: [state.headSha]",
     "parents: []", "contract"),
    ("S62_base_tree_dropped", SUBMIT_JS_REL,
     "body: JSON.stringify({ base_tree: state.rootTreeSha, tree: entries })",
     "body: JSON.stringify({ tree: entries })", "contract"),

    ("C50_payload_drops_token", CHECK_HTML_REL,
     '  if(typeof LAST.link_token === "string" && LAST.link_token) payload.token = LAST.link_token;',
     "  void 0;", "localtime"),
    ("C51_payload_drops_token_ko", CHECK_HTML_KO_REL,
     '  if(typeof LAST.link_token === "string" && LAST.link_token) payload.token = LAST.link_token;',
     "  void 0;", "localtime"),
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
