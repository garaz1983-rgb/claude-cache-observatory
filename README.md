# Cache Eviction Observatory

Self-check your Claude Code prompt-cache losses in the browser, then (optionally) share the aggregate so many machines can be compared in one place. One machine is one row: a returning submitter updates its own row instead of being counted twice.

Live site: **https://claude-cache-observatory.pages.dev** (English) · **/ko/** (한국어)

Community tool. **Not affiliated with Anthropic.** Evidence thread: [anthropics/claude-code#87966](https://github.com/anthropics/claude-code/issues/87966)

## What it does

Claude Code's local session logs (`~/.claude/projects/**/*.jsonl`) contain a server-stamped diagnostic, `message.diagnostics.cache_miss_reason = previous_message_not_found`, written when the server could not find the prompt-cache entry for your previous turn and re-billed your whole context. Most raw hits are legitimate expiries after a long idle period; this project counts only losses **inside the cache TTL** (idle gap under 30 minutes for main sessions, under 5 minutes for subagents) and calls out the subset under 5 minutes ("iron" losses) that no published TTL can explain.

## Repository map

| Path | What it is |
|---|---|
| `index.html`, `ko/index.html` | The observatory (fleet dashboard): KPIs, daily loss-rate trend, submissions table, methodology and FAQ |
| `check.html`, `ko/check.html` | Self-check page: folder pick / drag-and-drop / CLI-JSON paste, personal dashboard, opt-in submission card |
| `assets/parse.js` | The browser judgment engine (v2.1 rules). No network, no DOM, UMD; also runnable under Node for tests |
| `assets/charts.js` | Canvas charts (fleet trend, daily bars, usage heatmap). Render only, no logic |
| `assets/store.js` | Opt-in `localStorage` layer for the check page: whitelist-only serialisation, incremental period, overlap detection, and the two M13 identity values (the machine's anchor hashes, the link token). No network, no DOM, UMD |
| `assets/identity.js` | The machine fingerprint: reads request IDs out of the loaded logs, samples at most 16 deterministically (a short scan yields fewer, since the spread picks collide with the head and dedup away) and SHA-256s them in the browser. Only digests leave. No network, no DOM, no storage, UMD |
| `assets/localtime.js` | Display-only: re-cuts the check page's day rows, hour columns, per-event times and observed period into the reader's own timezone. Moves whole records between buckets, never re-judges one. No network, no DOM, UMD |
| `assets/og.png` | Static share card referenced by the OG meta tags (self-hosted, no external requests) |
| `data/submissions.json` | The entire public dataset. Written only by bot commits from the submit function. One row is one machine: a returning submitter's row is updated in place, never duplicated |
| `functions/api/submit.js` | The single Pages Function: schema whitelist, sanity checks, rate limit, identity match, merge, GitHub bot commit |
| `scripts/check_cache_loss.py` | CLI self-check (v2.1, the judgment SSOT). `--json` prints the aggregate for the site's paste fallback |
| `tests/` | `parity_check.py` (browser vs CLI judgment parity), `submit_contract_test.py` (API contract), `range_filter_test.py` (period picker), `storage_test.py` (local save: round trip, forbidden fields, increment, overlap), `localtime_test.py` (local-time view: sum preservation, boundary days, offset-0 no-op, loss-vs-usage cell attribution in `:30`/`:45` zones, the offsets the pages print, and the payload's UTC provenance), `identity_test.py` (the machine fingerprint: digests only, sample shape, determinism, and the pinned overlap measurements across appended / trimmed / rotated / foreign log sets), `mutation_run.py` (mutation harness over the engines, `localtime.js`, both check pages, `identity.js` and the M13 identity/merge branches) |
| `wrangler.toml` | Cloudflare Pages configuration (KV binding for the rate-limit counter), public in-repo |

## Trust model

- **Parsing is local.** Your log files are parsed inside your browser tab and never leave your machine. Verify it yourself: open the F12 Network tab while running a diagnosis; no request appears.
- **Transmission is opt-in and previewed.** Nothing is sent until you press the share button, and what is sent is exactly the aggregate JSON shown in the preview: period, totals, daily counts, optional nickname, and the machine fingerprint described below. The schema cannot carry conversation content, session IDs, file paths or timestamps; undefined fields are rejected with a 400, not dropped.
- **One machine is one row, and the fingerprint is one-way.** The observatory adds every row together, so a person who submitted twice used to be counted twice — on 2026-08-24 that put 188,174 requests and 343 losses on screen for a machine that had 153,623 and 228. There is no login, so identity is derived from the logs themselves: `assets/identity.js` samples up to 16 `requestId`s (8 earliest plus an even spread across the scan; a scan under ~120 requests yields fewer, because a spread pick lands inside the head window and dedups away), SHA-256s each in your browser, and sends only the digests. A returning submission that shares even one digest updates its existing row instead of adding another. The pasted-CLI path carries no request IDs, so for that path only the API issues a random link token, the page stores it, and a later paste presents it; the folder-scan path is never asked to store anything.
- **Nothing in the public dataset can be replayed.** `data/submissions.json` is world-readable, so it holds a *second* hash of every anchor (`sha256("cco.anchor2.v1|" + anchor)`) and only the hash of a token. A value copied out of that file hashes to something else and matches nothing; forging a match needs the machine's own logs. `identity`, `anchor_hashes`, `token_hash` and `updated_at` are server-generated and rejected by the same whitelist as any unknown field. `tests/submit_contract_test.py` case13 sends a published hash back and asserts the target row comes back byte-identical.
- **An update merges, it never clobbers.** Daily rows are unioned by date with the incoming row winning (it is the fresher measurement of that day), the period widens to the union, and the totals are recomputed from the merged rows so the file's own arithmetic stays true. A 3-day increment can therefore be sent against a 3-month record without losing it. The one total that cannot be recomputed is `iron_losses` — there is no per-day iron column — so it carries `max(0, previous_iron - losses on the superseded days)` plus the incoming count: exact for a disjoint increment and for a full re-scan, and never over-claiming in between.
- **Nicknames are masked before they are stored.** `functions/api/submit.js` keeps the first character and replaces the rest with a fixed `***` (a one-character nickname keeps nothing), so neither the string nor its length reaches `data/submissions.json`, the bot's commit message or the API response. Masking at storage time rather than at render time is the whole point: the dataset is a public file, so hiding a name only in the page would be the appearance of protection and not protection. Because the row can no longer be found by reading it, the observatory marks *your* row from the submission id your own browser stored (`assets/store.js`); with no local record, nothing is marked.
- **Local storage is opt-in and stays local.** By default a diagnosis lives only in the open tab. Pressing "Save to this browser" and confirming the modal writes totals, daily rows, the hourly census, the machine's anchor digests and the last submission's period into that browser's `localStorage` (`assets/store.js`) — never to a server, and "Clear saved results" removes it immediately. The single write that is not opt-in is the link token, and only on the pasted-CLI path where no fingerprint is possible; the check page says so before the submit button and the same Clear button deletes it. The stored object is built field by field from a whitelist, so file paths, session IDs, requestIds and conversation text cannot enter it, matching the submission schema's exclusions. `tests/storage_test.py` asserts that.
- **Two clocks, both stated on screen.** The submission payload and this observatory cut days by **UTC**, because submissions from many countries are summed day by day and cutting each by its own local date would scramble the fleet's daily totals. The check page is one person's own diagnosis, so its charts, heatmap and per-event times are drawn in **the reader's own timezone**, detected from the browser and named on the page. Same requests, same totals; only the day boundary moves, so a request near midnight can appear on one date there and the neighbouring date here. The payload preview says so before you send. A heatmap cell is one whole UTC hour on your clock — in a `:30`/`:45` zone such as India or Nepal it therefore starts at `:30`/`:45` past the hour it is drawn under, which the heatmap says on screen for those readers — and a loss is always drawn in the same cell its own request was counted in, so a red cell can never sit over an idle background or on a day with no requests. `tests/localtime_test.py` holds the totals equal across the move, holds a UTC reader's screen byte-identical to what it was before the conversion existed, and holds that the payload is built from the engine's UTC rows rather than from the localised screen.
- **The running code is this repo.** There is no build step, no bundler, no external CDN, no analytics. What is served is byte-for-byte what you can read here. The only server code is `functions/api/submit.js`.
- **Submission history is commit history.** Every accepted submission is a bot commit on `data/submissions.json` whose message names it (`data: submission …` for a new row, `data: update …` for a merge into an existing one). The file itself is not append-only any more — a returning machine's row is rewritten in place — but the history is, and every version of every row stays readable in it. Removal of bad data happens by public revert commits, never by history rewrite.
- **Configuration is public.** The Cloudflare setup lives in `wrangler.toml` in this repo. Secrets (the bot's fine-grained token) exist only as Cloudflare environment secrets, never in code.
- **Running it does not touch your Claude account.** The tools read log files; they make no contact with your Anthropic account or the API.

## Self-check

**Web (no install):** open [the check page](https://claude-cache-observatory.pages.dev/check.html) and drop your `~/.claude/projects` folder (Chrome/Edge can also use the folder picker). Everything renders locally.

**CLI (Python 3.8+, stdlib only):**

```
python scripts/check_cache_loss.py          # human-readable monthly table
python scripts/check_cache_loss.py --json   # aggregate JSON (totals + daily)
```

The `--json` output can be pasted into the check page's paste fallback to get the same dashboard and the same submission path. Note: the `--json` flag is an addition made in this repo's copy of the script for that fallback; the judgment logic is unchanged v2.1.

## Submission schema (summary)

One submission is one JSON object. Whitelist enforced server-side; anything not listed here is a 400.

| Field | Constraint |
|---|---|
| `nickname` | optional, string, max 20 chars (the only free text). Validated at full length, then **stored masked**: first code point + `***`, or `***` alone for a single character, or `anonymous` when empty |
| `plan` / `client` / `concurrent_sessions` | fixed enums (`unknown` allowed everywhere) |
| `period_start` / `period_end` | `YYYY-MM-DD`, span at most 92 days |
| `totals` | `requests`, `in_ttl_losses`, `iron_losses`, `wasted_tokens` (non-negative integers, `losses <= requests`, `iron <= losses`) |
| `daily[]` | 1..92 entries of `date`, `requests`, `losses`, `wasted_tokens`; dates unique and inside the period; per-field sums must equal `totals` |
| `script_version` | short tag, e.g. `web-1.0` or `cli-2.1` |
| `anchors[]` | optional, at most 16 lowercase 64-char SHA-256 hex digests. The machine fingerprint. Any single digest matching a stored row updates that row |
| `token` | optional, 32 lowercase hex chars. A link token this API previously issued to a submission it could not fingerprint |

Server-generated fields on the stored row, which a client may **not** send (they are rejected as undefined fields): `id`, `submitted_at`, `updated_at`, `identity.anchor_hashes`, `identity.token_hash`.

Full definition: `functions/api/submit.js` (validation) and `design-docs/04_DATA_MODEL.md` in the project workspace.

## Rate limiting is best-effort

The submit endpoint limits each IP hash to 3 submissions per hour using a Cloudflare KV counter. KV read-modify-write is **not atomic**, so concurrent bursts can exceed the limit; this is a known, accepted property, and no hard cap is promised. The real backstops are the schema/sanity validation and the fact that every write is a public commit that can be publicly reverted.

## Known limitations

- **Loose-file drops lose the subagent marker.** Subagent logs are recognized by a `subagents` folder segment in the path. If you drop individual `.jsonl` files instead of the folder, everything is classified as a main session (30-minute TTL), which can over-count subagent losses. The page warns about this; drop or pick the whole folder for an accurate verdict.
- **Records without any ID share one dedup slot.** Deduplication uses `requestId` (falling back to `message.id`). Records carrying neither are collapsed into a single slot per scan, which can under-count requests in malformed logs.
- **The fingerprint drifts if the log folder rotates.** The 8 earliest anchors survive new logs being appended; the 8 spread anchors do not, because every proportional position moves when the record count grows. Measured in `tests/identity_test.py`: appending 100 records to 500 keeps 8 of 16, deleting the 3 oldest keeps 6, and deleting the oldest 150 while appending 150 keeps 1 (a coincidence, not a property). If a cleanup removes more than the head between two submissions, the machine reads as new and opens a second row — the old double count, at a much lower rate. The server refreshes the stored set to the newest sample on every update, so the chain holds as long as submissions are not separated by a full rotation.
- **Identity is per machine, and the fingerprint is only as private as the logs.** One person with a laptop and a desktop is two rows and nothing here can tell. A shared or synced `~/.claude` folder is one row for two people. And because the anchors come from the logs, anyone who can read a machine's log files can compute its anchors and rewrite its row; the dataset's defence against that is the same as against any bad submission — the schema, the rate limit, and a public revert.
- **A machine that submits by folder once and by paste later is not linked.** The paste path has no request IDs to fingerprint and, on a first paste, no token yet. It opens a second row. The reverse direction is covered: a folder scan that also carries the browser's stored token adopts the paste row.
- **Same rules, two engines.** `assets/parse.js` and `scripts/check_cache_loss.py` implement the same v2.1 rules and are held together by `tests/parity_check.py`; if you change one, you must change the other (see CONTRIBUTING).

## 한국어 요약

닉네임은 저장 시점에 마스킹된다(첫 글자 + 고정 `***`, 한 글자면 `***`, 비우면 `anonymous`). 공개 데이터 파일에 원본이 남으면 화면만 가리는 것은 보호가 아니라 보호하는 시늉이기 때문이다. 그래서 제출 후 자기 행을 찾는 경로는 닉네임이 아니라, 브라우저에 남은 제출 id로 해당 행을 표시하는 방식으로 바뀌었다. 로컬 기록이 없으면 아무 표시도 하지 않는다.

Claude Code 세션 로그에 서버가 직접 남기는 진단 필드(`previous_message_not_found`)를 근거로, 캐시 TTL 안쪽에서 일어난 프롬프트 캐시 유실만 집계하는 자가진단 + 공개 관측소다. 파싱은 전부 브라우저 로컬에서 일어나고 로그 파일은 기기를 떠나지 않는다. 전송되는 것은 사용자가 미리보기로 승인한 집계 숫자(기간·총계·일별 건수·선택 닉네임)와 아래의 단방향 지문 해시뿐이고, 정의되지 않은 필드는 서버가 400으로 거부한다. 빌드 스텝이 없어 실행 코드가 이 repo 파일 그대로이며, 제출 이력 전체가 커밋 이력으로 남는다. 설정은 `wrangler.toml`로 공개돼 있다. 기계 1대가 1행이다. 로그인이 없으므로 로그 안의 `requestId` 16개를 정해진 규칙으로 골라 브라우저에서 SHA-256해 다이제스트만 보내고(`assets/identity.js`), 그중 하나라도 같으면 새 행을 만들지 않고 기존 행을 갱신한다. 갱신은 덮어쓰기가 아니라 병합이다. 날짜별로 합치고 중복된 날은 최신 측정값을 쓰며 총계는 병합 결과에서 다시 계산된다. 공개 파일에는 받은 값을 한 번 더 해시한 값만 들어가므로, 그 파일에서 복사한 값으로는 아무 행도 고칠 수 없다. 붙여넣기 경로는 request ID가 없어 지문을 만들 수 없으므로 그 경로에만 서버가 연결 토큰을 발급해 브라우저가 저장한다. 한계도 적어 둔다. 오래된 로그가 대량으로 삭제되면 연결이 끊겨 새 행이 생길 수 있고, 한 사람이 컴퓨터 2대를 쓰면 2행이며, 로그 파일을 읽을 수 있는 사람은 그 기계의 행을 고칠 수 있다. CLI는 `python scripts/check_cache_loss.py`(표) 또는 `--json`(붙여넣기용 집계)으로 실행한다. `--json` 플래그는 이 repo 사본에 추가된 것이고 판정 로직은 v2.1 그대로다. rate limit은 KV 카운터 기반의 best-effort라 동시 버스트에서 상한을 넘을 수 있으며, 최종 방어선은 스키마 검증과 공개 revert다. 낱개 파일 드롭은 subagents 폴더 판별이 불가능해 전부 메인 세션(30분 TTL)으로 분류되니 폴더째 드롭을 권장한다. 커뮤니티 도구이며 Anthropic과 무관하다. 논의: [anthropics/claude-code#87966](https://github.com/anthropics/claude-code/issues/87966)
