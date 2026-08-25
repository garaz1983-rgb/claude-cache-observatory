#!/usr/bin/env python3
"""Mutation harness for the site's judgment + submission-validation logic.

Targets:
  - assets/parse.js          — judged by tests/parity_check.py (M01..M15, M22)
  - functions/api/submit.js  — judged by tests/submit_contract_test.py
                               (S16..S21, S23..S24, S39..S47, S52..S67,
                                S68..S82)
  - tests/dataset_validate.py — judged by tests/submit_contract_test.py (V83)
  - assets/localtime.js      — judged by tests/localtime_test.py (L25..L29)
  - check.html, ko/check.html — judged by tests/localtime_test.py
                               (C30..C33, C48..C51)
  - assets/identity.js       — judged by tests/identity_test.py (I34..I38)

🔴 About the ids, because a comment in this file described them wrongly until
M14.2 audited them. The NUMBER is a single counter shared by every target and
the letter only names which file the mutant edits, so the counter does not
restart per prefix: 48..51 are C48..C51, which is the whole reason no S48..S51
exists. Two numbers, 56 and 60, are used by nothing at all — a historical gap
with no surviving explanation, not a truncated list. So no id sequence here can
be read as a count of anything: `len(MUTATIONS)` is the only count that means
something, every anchor is verified to occur exactly once before any mutant is
applied, and both numbers are printed at the top of every run.

M14 added the S52..S62 block and M14.1 the S63..S67 one. Those guard the split
of the public dataset into an index, an identity map, a fleet-wide daily series
and one detail file per submission, written as ONE commit through the Git Data
API. Two properties there have no production check behind them: nothing
recomputes the fleet series from scratch on the live path (it is a delta, on
purpose — recomputing it would restore the cost the milestone removed), and
nothing re-reads a commit to confirm all four files were in it. Both therefore
have to be proven by mutants that die.

M14.2 added S68..S82 and V83, and most of them exist because an independent
verifier found that mutating those lines changed nothing any suite could see.
The reason was the same in every case: the mock could fail exactly one thing, a
blob POST. So a mutant that treated a failed root-tree GET as "this repository
is empty" — and would therefore have written an index containing only the new
row, wiping every other submitter's — survived the whole suite, and so did a
commit-POST failure branch whose meaning is "tell the submitter their data was
saved when no ref was ever updated". The mock can now fail tree listings by
position, truncate them, fail the commit POST, and hold the branch contended,
which is what makes S68..S73 and S80..S82 observable at all.

V83 is a different kind: the id regex in tests/dataset_validate.py claims to
mirror SUB_ID_RE in the worker and did not, because Python's `$` matches before
a trailing newline. A validator more lenient than the write path blesses rows
that can never be written again.

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
VALIDATE_PY_REL = "tests/dataset_validate.py"
# M15. The CLI is half the parity contract and had never been mutated:
# until now its only logic was the untouched v2.1 judgment loop, which
# the parse.js mutants already exercise from the other side.
CLI_PY_REL = "scripts/check_cache_loss.py"

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
    # M14 moved storage onto the Git Data API, so the retry is no longer a 409
    # on a PUT but a 422 on the ref update. Same property, new anchor: a branch
    # that moved under the read must earn a re-read, and giving up instead loses
    # the submission. M14.2 turned the single retry into a budget, so the mutant
    # is now "give up on the first conflict" rather than "do not loop".
    ("S21_ref_retry_disabled", SUBMIT_JS_REL,
     "      if (attempt >= COMMIT_MAX_ATTEMPTS ||",
     "      if (true ||", "contract"),

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
     "if (storedTokenHash(identityEntry(identities, subs[i] && subs[i].id)) === tokenHash) return i;",
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
    # adds up. S54/S55 are the fleet series, which is maintained as a DELTA
    # and is therefore the one number on the site that can drift silently:
    # nothing recomputes it from scratch in production, so the mutants that make
    # it drift have to die in the test. (M14.2 note: this sentence named a
    # non-existent S56 until the id gaps were audited — see the header.)
    # S61/S62 are the atomicity: a commit that
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

    # -- M14.1: the identity file (functions/api/submit.js) ------------------
    # Moving the digests out of the index turned one field into a fourth FILE,
    # and a file has three ways to go wrong that a field did not: it can fail to
    # be written, fail to be read, or be read in a way that quietly resolves
    # every submitter as a stranger. Each of those silently un-does M13 — the
    # machine that already has a row opens a second one — while every response
    # stays 200 and every file still parses. S63..S67 are those failures.
    #
    # S65 is the regression this milestone exists to prevent: putting the block
    # back on the index row. It stays a 200, the dataset still adds up
    # arithmetically, and the only thing that changes is that every visitor
    # downloads 1,313 bytes per fingerprinted row again — so nothing but an
    # explicit check can catch it, which is why the validator gained one.
    ("S63_identity_file_not_written", SUBMIT_JS_REL,
     "      { path: IDENTITY_PATH,\n"
     "        text: serializeIdentity({ schema_version: IDENTITY_SCHEMA_VERSION,\n"
     "                                  identities: identities }) },",
     '      { path: "data/.mutant2", text: "x\\n" },', "contract"),
    ("S64_identity_lookup_disabled", SUBMIT_JS_REL,
     "  if (!Object.prototype.hasOwnProperty.call(identities, id)) return null;",
     "  return null;", "contract"),
    ("S65_index_row_keeps_identity", SUBMIT_JS_REL,
     "    const record = composeRecord(id, submittedAt, updatedAt, fields);",
     "    const record = composeRecord(id, submittedAt, updatedAt, fields);\n"
     "    if (identity) record.identity = identity;", "contract"),
    ("S66_identity_absent_is_fatal", SUBMIT_JS_REL,
     "  let identities = {};\n  if (identitySha) {",
     "  let identities = {};\n  if (!identitySha) return null;\n  if (identitySha) {",
     "contract"),
    ("S67_identity_map_not_carried", SUBMIT_JS_REL,
     "      if (carried) identities[rowId] = carried;",
     "      void carried;", "contract"),
    # NOT mutated: swapping the rebuild loop to walk state.identities instead of
    # the index, which is how an "entry with no index row" would be produced.
    # No sequence of submissions can reach it — the map is derived from the
    # index every time, so an orphan never exists to be carried forward — and it
    # would therefore be an equivalent mutant. A mutation list that carries one
    # teaches the next reader to tolerate survivors. That half of the relation
    # is proven instead by run_validator_cases() in the contract test, which
    # hands the validator an orphan directly.

    # -- M14.2: the read guards, the retry budget and the honest refusal ------
    # 🔴 S68/S69 are the two the verifier's run named first. Both are one-line
    # status checks whose removal turns a FAILED read into a confident wrong
    # answer: `root` not being 200 means the walk never reaches data/, so the
    # write proceeds against an index it believes is empty and publishes one
    # containing only the new row — every other submitter's row gone, the fleet
    # series and the identity map replaced, every detail file orphaned. It is
    # the single most destructive thing this endpoint can do and nothing tested
    # it, because the mock could only fail blob POSTs.
    ("S68_root_tree_status_unchecked", SUBMIT_JS_REL,
     "  const root = await ghJson(api + \"/git/trees/\" + rootTreeSha, token);\n"
     "  if (root.status !== 200) return null;",
     "  const root = await ghJson(api + \"/git/trees/\" + rootTreeSha, token);",
     "contract"),
    ("S69_data_tree_status_unchecked", SUBMIT_JS_REL,
     "    if (dataTree.status !== 200) return null;",
     "    if (false) return null;", "contract"),
    # S70 (subs-tree status unchecked) was written and then withdrawn: it is
    # EQUIVALENT, and a survivor left in the list would teach the next reader
    # that survivors are tolerable. Evidence, not assertion: dropping
    # `if (listing.status !== 200) return null;` in readDetailDaily leaves
    # listing.body as GitHub's error object ({"message": ...}), which carries no
    # `truncated` and no `tree`, so treeTruncated() is false, treeEntry() finds
    # nothing, and the next line — `if (!entry) return null;` — produces the
    # identical refusal. Same 502, nothing published. The guard is still correct
    # and stays: it is the difference between "the read failed" and "the file is
    # not there" for any future caller that reads listing.body directly.
    # "Tell the submitter their data was saved when no ref was ever updated."
    ("S71_commit_post_failure_is_success", SUBMIT_JS_REL,
     "  if (commit.status !== 200 && commit.status !== 201) return null;",
     '  if (commit.status !== 200 && commit.status !== 201) return { url: "" };',
     "contract"),
    # Reachable by two ordinary submissions: machine A reports June, machine B
    # then reports May. Without the sort the public file reads
    # ["2026-06-01", "2026-05-01"] and tests/dataset_validate.py rejects it.
    ("S72_fleet_series_not_sorted", SUBMIT_JS_REL,
     "  out.sort(function (a, b) { return a.date < b.date ? -1 : "
     "(a.date > b.date ? 1 : 0); });\n  return { schema_version: "
     "FLEET_SCHEMA_VERSION, days: out };",
     "  return { schema_version: FLEET_SCHEMA_VERSION, days: out };",
     "contract"),
    # 🔴 D2: one hand-edited row used to make this API PUBLISH a dataset that
    # did not add up — mergeRecord drops a malformed row and applyFleetDelta
    # could not read a `.date` off it, so the date left the detail file and
    # stayed in the fleet series. Seven of eight shapes returned 200; the eighth
    # threw and escaped as a 500.
    ("S73_detail_row_shape_unchecked", SUBMIT_JS_REL,
     "    const row = dailyRowOf(detail.daily[i]);\n    if (!row) return null;",
     "    const row = detail.daily[i];", "contract"),
    ("S74_empty_detail_daily_allowed", SUBMIT_JS_REL,
     "  if (!detail.daily.length) return null;   // a row with no history is not one",
     "  void 0;", "contract"),
    # GitHub truncates a tree listing at 100,000 entries or 7 MB, whichever
    # comes first (~28,000 here), and the reply still
    # looks complete, so ignoring the flag is a read that is wrong in the one
    # direction that then writes.
    ("S75_tree_truncation_ignored", SUBMIT_JS_REL,
     "  return !!(body && body.truncated === true);",
     "  return false;", "contract"),
    # 🔴 D1: one retry means exactly one loser of any race gets a second chance,
    # so a burst of ten accepted two. The budget is a contract; halving it is
    # not a refactor.
    ("S76_retry_budget_back_to_one", SUBMIT_JS_REL,
     "const COMMIT_MAX_ATTEMPTS = 6;",
     "const COMMIT_MAX_ATTEMPTS = 2;", "contract"),
    # A lost race is not an outage. Saying "storage" makes it indistinguishable
    # from GitHub being down, which is what the page used to tell people.
    ("S77_conflict_reported_as_storage", SUBMIT_JS_REL,
     "  return { ok: false, conflict: conflicts > 0 };",
     "  return { ok: false };", "contract"),
    ("S78_conflict_409_not_recognised", SUBMIT_JS_REL,
     "  if (res.status === 422 || res.status === 409) return true;",
     "  if (res.status === 422) return true;", "contract"),
    ("S79_conflict_message_ignored", SUBMIT_JS_REL,
     "  return message.indexOf(\"fast forward\") !== -1 ||\n"
     "    message.indexOf(\"fast-forward\") !== -1;",
     "  return false;", "contract"),
    # The two the harness had recorded as equivalent. They ARE unreachable by
    # any sequence of submissions — the merge unions dates and never removes one
    # — but data/daily.json is a file in a public repository and an admin can
    # edit it, which is the universe these guards were written for. case31
    # reaches them, so they are covered instead of argued about.
    ("S80_fleet_phantom_day_kept", SUBMIT_JS_REL,
     "    if (d.machines <= 0) return;",
     "    if (false) return;", "contract"),
    ("S81_fleet_requests_not_clamped", SUBMIT_JS_REL,
     "      requests: Math.max(0, d.requests),",
     "      requests: d.requests,", "contract"),
    ("S82_fleet_losses_not_clamped", SUBMIT_JS_REL,
     "      losses: Math.max(0, d.losses),",
     "      losses: d.losses,", "contract"),
    # -- M14.2: the validator must not be more lenient than the write path ----
    # Python's `$` matches before a trailing newline and JavaScript's does not.
    ("V83_id_regex_allows_trailing_newline", VALIDATE_PY_REL,
     'SUB_ID_RE = re.compile(r"^sub-[0-9]{14}-[0-9a-f]{4}\\Z")',
     'SUB_ID_RE = re.compile(r"^sub-[0-9]{14}-[0-9a-f]{4}$")', "contract"),

    ("C50_payload_drops_token", CHECK_HTML_REL,
     '  if(typeof LAST.link_token === "string" && LAST.link_token) payload.token = LAST.link_token;',
     "  void 0;", "localtime"),
    ("C51_payload_drops_token_ko", CHECK_HTML_KO_REL,
     '  if(typeof LAST.link_token === "string" && LAST.link_token) payload.token = LAST.link_token;',
     "  void 0;", "localtime"),
    # -- M15: the detector census. Everything here is COUNTING, so every one of
    # these mutants is killed by the two engines disagreeing, or by both
    # disagreeing with tests/fixtures_detector's hand-computed expectation.
    # "Report a confident zero on the day the server renames the reason."
    ("D01_census_skips_unknown_reasons", PARSE_JS_REL,
     "          censusAdd(reasonCensus, r.rtype, false);",
     "          if (r.rtype === PMNF_REASON) censusAdd(reasonCensus, r.rtype, false);",
     "parity"),
    ("D02_unknown_counter_dead", PARSE_JS_REL,
     "          if (r.rtype !== PMNF_REASON) detector.unknown_reasons += 1;",
     "          if (false) detector.unknown_reasons += 1;", "parity"),
    ("D03_cold_write_ignores_the_read", PARSE_JS_REL,
     "        if (r.cc > 0 && r.cr === 0) detector.cold_writes += 1;",
     "        if (r.cc > 0) detector.cold_writes += 1;", "parity"),
    # The charset is what stops a log file putting free text into a public
    # file. fixtures_detector carries the reason "not a tag!" for this.
    ("D04_census_charset_relaxed", PARSE_JS_REL,
     "  var CENSUS_KEY_RE = /^[A-Za-z0-9._-]{1,64}$/;",
     "  var CENSUS_KEY_RE = /^.{1,64}$/;", "parity"),
    ("D05_missing_version_not_counted", PARSE_JS_REL,
     "        censusAdd(versionCensus, r.version, true);",
     "        censusAdd(versionCensus, r.version, false);", "parity"),
    # Sorting by count first is what makes the two engines agree at all: they
    # walk files in different orders, so insertion order is not shared.
    ("D06_census_sort_ignores_count", PARSE_JS_REL,
     "      return (b[1] - a[1]) || (a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0));",
     "      return (a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0));", "parity"),
    ("D07_census_cap_lifted", PARSE_JS_REL,
     "  var CENSUS_MAX_KEYS = 12;", "  var CENSUS_MAX_KEYS = 99;", "parity"),
    # The same two rules on the CLI side of the parity contract.
    ("D08_cli_census_charset_relaxed", CLI_PY_REL,
     'CENSUS_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")',
     'CENSUS_KEY_RE = re.compile(r"^.{1,64}$")', "parity"),
    ("D09_cli_missing_version_not_counted", CLI_PY_REL,
     "        census_add(version_census, ver, True)",
     "        census_add(version_census, ver, False)", "parity"),

    # -- M15: the published vocabulary. These strings are the only part of the
    # public dataset that begins as free text on a stranger's disk, so the
    # mutants below are the ones that would put it there.
    # "Publish whatever string was in someone's log file."
    ("D10_vocabulary_charset_relaxed", SUBMIT_JS_REL,
     "const CENSUS_KEY_RE = /^(?:[A-Za-z0-9._-]{1,64}|\((?:invalid|none|other)\))$/;",
     "const CENSUS_KEY_RE = /^.{1,200}$/;", "contract"),
    ("D11_vocabulary_cap_lifted", SUBMIT_JS_REL,
     "  if (v.length > MAX_CENSUS_KEYS) {", "  if (false) {", "contract"),
    ("D12_vocabulary_duplicates_allowed", SUBMIT_JS_REL,
     '    if (seen.has(v[i])) errors.push(label + "[" + i + "]: duplicate entry");',
     "    void 0;", "contract"),
    ("D13_vocabulary_undefined_field_allowed", SUBMIT_JS_REL,
     "        if (DETECTOR_FIELDS.indexOf(key) === -1) {",
     "        if (false) {", "contract"),
    # The published order is the server's, never the submitter's.
    ("D14_vocabulary_not_sorted", SUBMIT_JS_REL,
     "      reasons: body.detector.reasons.slice().sort(),",
     "      reasons: body.detector.reasons.slice(),", "contract"),
    # Absent and empty are different claims: "never reported" vs "looked and
    # met none". Collapsing them makes the front page's coverage line a lie.
    ("D15_absent_vocabulary_stored_anyway", SUBMIT_JS_REL,
     "  if (fields.detector) rec.detector = fields.detector;",
     "  rec.detector = fields.detector;", "contract"),
    # A rename has to be visible as a CHANGE. A row that keeps every name it
    # ever saw can never show one going away.
    ("D16_merge_keeps_the_old_vocabulary", SUBMIT_JS_REL,
     "    detector: incoming.detector",
     "    detector: existing.detector || incoming.detector", "contract"),
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
