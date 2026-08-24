# Contributing

Small repo, strict invariants. Read this before touching anything.

## Ground rules

- **No build step.** Files are served byte-for-byte. No bundlers, no minification, no frameworks, no transpilation.
- **Zero external requests.** Pages may load only same-origin static files. No CDN scripts, no external fonts, no analytics. The single exception is the user-triggered `POST /api/submit`. The OG image is self-hosted (`assets/og.png`).
- **Privacy is schema-enforced.** The submission schema (see `functions/api/submit.js`) is a whitelist; never add fields that could carry conversation content, session IDs, file paths or per-event timestamps. Undefined fields must return 400, not be dropped.
- **`data/submissions.json` is append-only.** Fixes happen via revert or follow-up commits. Never force-push or rewrite history on this repo.
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
4. `assets/parse.js`, `assets/store.js` and `assets/charts.js` are shared and language-neutral; labels are injected from each page, so page-level label changes never go into the assets.
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

Saving is opt-in: it happens only after the user presses the save button **and** confirms the modal. The confirmation is a custom modal, never `confirm()`/`alert()` (theme, translation, and headless testability).

A stored run keeps the **engine's own UTC buckets**. It must never freeze a local date at save time: the machine's timezone can change and the same profile can be read elsewhere, so the reader's clock is applied on the way to the screen instead (see below).

## Which clock a date is on

Two clocks, deliberately:

- **The engines, the submission payload and the public observatory are UTC.** `parse.js` and `check_cache_loss.py` bucket by the record's own offset, which is UTC for the `...Z` stamps Claude Code writes. Submissions from many countries are summed day by day, so every one of them has to be cut the same way; cutting each by its own local date would scramble the fleet's daily totals. `daily[].date`, `period_start` and `period_end` are UTC and must stay that way — the contract, the existing dataset and `tests/submit_contract_test.py` all depend on it.
- **The personal screens (`check.html`, `ko/check.html`) are the reader's own timezone.** The heatmap's day rows and hour columns, the per-event popover, the daily trend and the observed period are re-cut by `assets/localtime.js`, which is display-only: it moves whole records between buckets and never re-decides anything. Judgment is the idle gap between requests and cannot depend on a timezone.

**Convert in the display layer only.** If a conversion ever needs to reach into `parse.js` or `check_cache_loss.py`, the design is wrong. `tests/localtime_test.py` holds the two invariants that make the split safe: every column sums to the same total after re-bucketing, and at offset 0 the output is byte-identical to the pre-M12 code. Both pages state on screen which clock they are on, and the payload preview says plainly that it is UTC while the screen above it is not.

## Running the tests

All six must exit 0 before a push. Requirements: Python 3.8+, Node (for `parse.js` / `store.js` / `localtime.js` runs), and `npx` (the contract test boots `wrangler pages dev` locally; first run downloads wrangler).

```
python tests/parity_check.py          # parse.js vs CLI on the same fixtures
python tests/submit_contract_test.py  # /api/submit contract (mock GitHub, local KV)
python tests/range_filter_test.py     # submission period picker (filterRange/clampRange/daySpan)
python tests/storage_test.py          # local save: round trip, forbidden fields, increment, overlap
python tests/localtime_test.py        # local-time view: sum preservation, boundary days, offset-0 no-op
python tests/mutation_run.py          # mutation harness over both engines
```

Notes:

- `mutation_run.py` requires a **clean git tree** (it mutates files and restores them via `git checkout`), so commit your work first.
- If node is not on PATH, set `CACHE_OBS_NODE` to the node executable.
- The contract test uses only 127.0.0.1; no real GitHub, no real tokens.

## OG image

`assets/og.png` is a static 1200x630 card (dark background, title, subtitle, disclaimer badge) referenced by the `og:image` tags on all four pages. Regenerate it by rendering an equivalent HTML card at 1200x630 and screenshotting it (e.g. headless Chromium); keep it self-hosted and reference it only by this path.
