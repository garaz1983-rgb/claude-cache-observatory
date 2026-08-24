# Contributing

Small repo, strict invariants. Read this before touching anything.

## Ground rules

- **No build step.** Files are served byte-for-byte. No bundlers, no minification, no frameworks, no transpilation.
- **Zero external requests.** Pages may load only same-origin static files. No CDN scripts, no external fonts, no analytics. The single exception is the user-triggered `POST /api/submit`. The OG image is self-hosted (`assets/og.png`).
- **Privacy is schema-enforced.** The submission schema (see `functions/api/submit.js`) is a whitelist; never add fields that could carry conversation content, session IDs, file paths or per-event timestamps. Undefined fields must return 400, not be dropped. The M13 identity fields are hard-shaped for the same reason: `anchors[]` is at most 16 lowercase sha-256 digests and `token` is 32 lowercase hex, so no free text can enter the public file through them.
- **`data/submissions.json` holds one row per machine.** It is no longer append-only: a submission whose identity matches an existing row REWRITES that row in place (M13). What stays absolute is that the file must mean exactly what it looks like — a reader adding up the rows by hand must get the KPIs — so a superseding row appended next to a stale one is not an acceptable alternative, and neither is a renderer that knows to skip something. Fixes happen via revert or follow-up commits. Never force-push or rewrite git history on this repo.
- **Nothing published may be replayable.** Anything a reader of `data/submissions.json` can copy is, by definition, something they can send back. So the file stores a second hash of the client's anchors and only the hash of a link token, and `identity`/`anchor_hashes`/`token_hash`/`updated_at` are server-generated and rejected by the client whitelist. If you add a field that participates in deciding WHICH row a submission may write, it belongs under the same rule and needs a mutant in `tests/mutation_run.py`.
- **No "official" wording.** This is a community tool with no Anthropic affiliation, and every page must keep saying so.

## en/ko 4-file sync checklist

The four HTML pages are language-duplicated by design (hardcoded text, no i18n framework). Any content or markup change must be applied to all affected files in the same commit:

- [ ] `index.html` (en observatory)
- [ ] `ko/index.html` (ko observatory)
- [ ] `check.html` (en self-check)
- [ ] `ko/check.html` (ko self-check)

Checklist for a page change:

1. Identify whether the change touches the observatory pair, the check pair, or all four.
2. Apply the same structural change (IDs, classes, element order) to both languages; only the human-readable text differs.
3. Keep element IDs identical across languages (the inline scripts rely on them).
4. `assets/parse.js`, `assets/store.js`, `assets/identity.js` and `assets/charts.js` are shared and language-neutral; labels are injected from each page, so page-level label changes never go into the assets.
5. Korean copy: do not use the em-dash (—); use Korean punctuation (comma, period, colon, `·`) instead.

## parse.js and CLI judgment parity

`assets/parse.js` (browser) and `scripts/check_cache_loss.py` (CLI, the v2.1 SSOT) must implement identical judgment rules:

- PMNF candidates only (`message.diagnostics.cache_miss_reason` resolving to `previous_message_not_found`);
- dedup by `requestId` falling back to `message.id`, with later records of a seen request only back-filling a missing reason (same file only);
- idle gap to the previous request in the same file: in-TTL when under 1800 s (main) / 300 s (subagent, marked by a `subagents` path segment); iron when under 300 s; anything longer is a legitimate expiry and not a loss;
- wasted tokens = `cache_creation_input_tokens` of each in-TTL-lost request.

**If you change a rule in one engine you must change the other in the same commit**, and `tests/parity_check.py` must pass. Output-only changes (like the CLI's `--json` shaping) are fine as long as the judgment loop is untouched, but state that explicitly in the commit message.

The check page's paste fallback consumes the CLI's `--json` output (`script_version`, `totals`, `daily`). If that shape changes, update the paste validator in both `check.html` and `ko/check.html` and this note.

## assets/store.js and the local save

`assets/store.js` is the check page's optional `localStorage` layer. Two rules are load-bearing:

- **Every storage access is wrapped in try/catch**, including resolving the `localStorage` property itself, which throws in a private window or with site data blocked. A storage failure disables saving and must never break the diagnosis.
- **Serialisation is whitelist-only.** `buildRun()` constructs the stored object field by field; nothing is spread or copied from the engine result. File paths, session IDs, `requestId`s, raw timestamps and conversation text must not be storable, the same exclusions the submission schema enforces. `tests/storage_test.py` greps the serialised blob for strings lifted out of the fixtures, so widening the whitelist fails the test rather than passing quietly.

- **M13 adds exactly two identity values, and no third.** `run.anchors` (the machine's sha-256 digests, so a restored run is still recognisable as this machine and M10's increment path keeps working) and `state.link_token`. Both are validated on the way in and on the way out. `link_token` is the one value the page writes without being asked, only on the pasted-CLI path, and `clear()` deletes it with everything else — which is why the check page shows the Clear button when a token exists even with nothing else saved.

Saving is opt-in: it happens only after the user presses the save button **and** confirms the modal. The confirmation is a custom modal, never `confirm()`/`alert()` (theme, translation, and headless testability).

A stored run keeps the **engine's own UTC buckets**. It must never freeze a local date at save time: the machine's timezone can change and the same profile can be read elsewhere, so the reader's clock is applied on the way to the screen instead (see below).

## assets/identity.js and the one-row rule

The observatory sums every row, so identity is a correctness feature, not a convenience. Three rules are load-bearing:

- **Fingerprint first, token only where a fingerprint is impossible.** `assets/identity.js` derives the machine's pseudonym from `requestId`s already in the logs, so it survives a cleared store, a private window and a different browser, and it needs no forced storage. The link token exists only for the pasted-CLI path, which carries aggregates with no per-request detail. **Do not extend token storage to the folder-scan path** — "this page writes nothing unless you opt in" is the reason this design was chosen over a token-only one.
- **The sample shape is the guarantee.** `HEAD_COUNT` earliest records plus an even spread across the scan, ordered by the record's own instant, any single overlap counting as the same machine, and the stored set refreshed to the newest sample on every update. Only-the-oldest dies to a log cleanup; only-the-newest cannot match anything that came before. `tests/identity_test.py` pins the overlap *numbers*, not just bounds — if you change the sampler, re-measure and update `EXPECTED_OVERLAP` and the drift bullet in README.md in the same commit.
- **Digests, never ids.** The requestIds must not reach the network, the public file or `localStorage`. `tests/identity_test.py` greps the module's output for them and `tests/storage_test.py` greps the serialised store; the prefix and the hash are pinned by an independently recomputed digest, because `functions/api/submit.js` compares against digests already stored under the old rule.

The merge (`mergeRecord` in `functions/api/submit.js`) unions daily rows by date with the incoming row winning, widens the period, and recomputes the totals from the merged rows. `iron_losses` is the exception: there is no per-day iron column, so it carries `max(0, previous_iron - losses on the superseded days)` plus the incoming count — exact for a disjoint increment and for a full re-scan, conservative in between. If a daily iron column is ever added, this is the first thing that should become an exact sum.

## Which clock a date is on

Two clocks, deliberately:

- **The engines, the submission payload and the public observatory are UTC.** `parse.js` and `check_cache_loss.py` bucket by the record's own offset, which is UTC for the `...Z` stamps Claude Code writes. Submissions from many countries are summed day by day, so every one of them has to be cut the same way; cutting each by its own local date would scramble the fleet's daily totals. `daily[].date`, `period_start` and `period_end` are UTC and must stay that way — the contract, the existing dataset and `tests/submit_contract_test.py` all depend on it.
- **The personal screens (`check.html`, `ko/check.html`) are the reader's own timezone.** The heatmap's day rows and hour columns, the per-event popover, the daily trend and the observed period are re-cut by `assets/localtime.js`, which is display-only: it moves whole records between buckets and never re-decides anything. Judgment is the idle gap between requests and cannot depend on a timezone.

**One grid, one attribution rule.** The hourly census carries counts per whole UTC hour and nothing finer — there are no per-request timestamps for the requests that did not lose — so a UTC hour that straddles a local half-hour boundary cannot be split and is attributed whole to the local hour it begins in. **An event is drawn in the cell its own UTC hour was attributed to**, never in the cell its exact instant falls in. Placing the two by different rules is what let a loss at UTC+5:30 land on a date the census did not have: it was drawn nowhere at all and its day row read `losses:1` against `requests:0`. The exact local clock is still printed on the event, so in a `:30`/`:45` zone it can fall just outside the round hour the cell is drawn under, and the page says so on screen rather than leaving it to be discovered.

**Every offset is read at the instant it describes.** Both placement and labelling ask `hostOffsetAt` at that moment (`offsetAtLocal` for a cell, whose local wall time fixes the instant), never once at page load. A February row in New York prints UTC-5 on a page opened in August.

**Convert in the display layer only.** If a conversion ever needs to reach into `parse.js` or `check_cache_loss.py`, the design is wrong. `tests/localtime_test.py` holds the invariants that make the split safe: every column sums to the same total after re-bucketing; at offset 0 the output is byte-identical to the pre-M12 code; every event sits in a cell the census has, with at least as many requests as losses; no day row shows more losses than requests; and — lifted verbatim out of both pages between their own `SUBMIT-PAYLOAD` / `LOCALTIME-LABELS` markers — the payload is built from the engine's UTC rows and the printed labels carry per-instant offsets. Keep those markers when editing the blocks between them. Since M13 the same lifted block is also driven with a stub fingerprint, so a page that quietly stops attaching `anchors`/`token` fails there — `/api/submit` would accept such a payload and simply append a second row, which is the double count M13 removed. Both pages state on screen which clock they are on, and the payload preview says plainly that it is UTC while the screen above it is not.

## Running the tests

All seven must exit 0 before a push. Requirements: Python 3.8+, Node (for `parse.js` / `store.js` / `localtime.js` / `identity.js` runs), and `npx` (the contract test boots `wrangler pages dev` locally; first run downloads wrangler).

```
python tests/parity_check.py          # parse.js vs CLI on the same fixtures
python tests/submit_contract_test.py  # /api/submit contract (mock GitHub, local KV) incl. identity + merge
python tests/range_filter_test.py     # submission period picker (filterRange/clampRange/daySpan)
python tests/storage_test.py          # local save: round trip, forbidden fields, increment, overlap, link token
python tests/identity_test.py         # machine fingerprint: digests only, sample shape, determinism, drift measurements
python tests/localtime_test.py        # local-time view: sums, boundary days, offset-0 no-op, cell attribution, printed labels, payload provenance
python tests/mutation_run.py          # mutation harness over the engines, localtime.js, both check pages, identity.js and the identity/merge branches
```

Notes:

- `mutation_run.py` requires a **clean git tree** (it mutates files and restores them via `git checkout`), so commit your work first.
- If node is not on PATH, set `CACHE_OBS_NODE` to the node executable.
- The contract test uses only 127.0.0.1; no real GitHub, no real tokens.
- On Windows, run the suites from PowerShell rather than Git Bash: Git Bash's POSIX `PATH` hides `node` from the Windows child process, and the suite then fails as `FATAL(setup) exit 2`, which looks like a real failure and is not.

## OG image

`assets/og.png` is a static 1200x630 card (dark background, title, subtitle, disclaimer badge) referenced by the `og:image` tags on all four pages. Regenerate it by rendering an equivalent HTML card at 1200x630 and screenshotting it (e.g. headless Chromium); keep it self-hosted and reference it only by this path.
