# Cache Eviction Observatory

Self-check your Claude Code prompt-cache losses in the browser, then (optionally) share the aggregate so multiple accounts can be compared in one place.

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
| `assets/og.png` | Static share card referenced by the OG meta tags (self-hosted, no external requests) |
| `data/submissions.json` | The entire public dataset. Written only by bot commits from the submit function |
| `functions/api/submit.js` | The single Pages Function: schema whitelist, sanity checks, rate limit, GitHub bot commit |
| `scripts/check_cache_loss.py` | CLI self-check (v2.1, the judgment SSOT). `--json` prints the aggregate for the site's paste fallback |
| `tests/` | `parity_check.py` (browser vs CLI judgment parity), `submit_contract_test.py` (API contract), `mutation_run.py` (mutation harness over both) |
| `wrangler.toml` | Cloudflare Pages configuration (KV binding for the rate-limit counter), public in-repo |

## Trust model

- **Parsing is local.** Your log files are parsed inside your browser tab and never leave your machine. Verify it yourself: open the F12 Network tab while running a diagnosis; no request appears.
- **Transmission is opt-in and previewed.** Nothing is sent until you press the share button, and what is sent is exactly the aggregate JSON shown in the preview: period, totals, daily counts, optional nickname. The schema cannot carry conversation content, session IDs, file paths or timestamps; undefined fields are rejected with a 400, not dropped.
- **The running code is this repo.** There is no build step, no bundler, no external CDN, no analytics. What is served is byte-for-byte what you can read here. The only server code is `functions/api/submit.js`.
- **Submission history is commit history.** Every accepted submission is appended to `data/submissions.json` by a bot commit whose message names the submission. Removal of bad data happens by public revert commits, never by history rewrite.
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
| `nickname` | optional, string, max 20 chars (the only free text) |
| `plan` / `client` / `concurrent_sessions` | fixed enums (`unknown` allowed everywhere) |
| `period_start` / `period_end` | `YYYY-MM-DD`, span at most 92 days |
| `totals` | `requests`, `in_ttl_losses`, `iron_losses`, `wasted_tokens` (non-negative integers, `losses <= requests`, `iron <= losses`) |
| `daily[]` | 1..92 entries of `date`, `requests`, `losses`, `wasted_tokens`; dates unique and inside the period; per-field sums must equal `totals` |
| `script_version` | short tag, e.g. `web-1.0` or `cli-2.1` |

Full definition: `functions/api/submit.js` (validation) and `design-docs/04_DATA_MODEL.md` in the project workspace.

## Rate limiting is best-effort

The submit endpoint limits each IP hash to 3 submissions per hour using a Cloudflare KV counter. KV read-modify-write is **not atomic**, so concurrent bursts can exceed the limit; this is a known, accepted property, and no hard cap is promised. The real backstops are the schema/sanity validation and the fact that every write is a public commit that can be publicly reverted.

## Known limitations

- **Loose-file drops lose the subagent marker.** Subagent logs are recognized by a `subagents` folder segment in the path. If you drop individual `.jsonl` files instead of the folder, everything is classified as a main session (30-minute TTL), which can over-count subagent losses. The page warns about this; drop or pick the whole folder for an accurate verdict.
- **Records without any ID share one dedup slot.** Deduplication uses `requestId` (falling back to `message.id`). Records carrying neither are collapsed into a single slot per scan, which can under-count requests in malformed logs.
- **Same rules, two engines.** `assets/parse.js` and `scripts/check_cache_loss.py` implement the same v2.1 rules and are held together by `tests/parity_check.py`; if you change one, you must change the other (see CONTRIBUTING).

## 한국어 요약

Claude Code 세션 로그에 서버가 직접 남기는 진단 필드(`previous_message_not_found`)를 근거로, 캐시 TTL 안쪽에서 일어난 프롬프트 캐시 유실만 집계하는 자가진단 + 공개 관측소다. 파싱은 전부 브라우저 로컬에서 일어나고 로그 파일은 기기를 떠나지 않는다. 전송되는 것은 사용자가 미리보기로 승인한 집계 숫자(기간·총계·일별 건수·선택 닉네임)뿐이고, 정의되지 않은 필드는 서버가 400으로 거부한다. 빌드 스텝이 없어 실행 코드가 이 repo 파일 그대로이며, 제출 이력 전체가 커밋 이력으로 남는다. 설정은 `wrangler.toml`로 공개돼 있다. CLI는 `python scripts/check_cache_loss.py`(표) 또는 `--json`(붙여넣기용 집계)으로 실행한다. `--json` 플래그는 이 repo 사본에 추가된 것이고 판정 로직은 v2.1 그대로다. rate limit은 KV 카운터 기반의 best-effort라 동시 버스트에서 상한을 넘을 수 있으며, 최종 방어선은 스키마 검증과 공개 revert다. 낱개 파일 드롭은 subagents 폴더 판별이 불가능해 전부 메인 세션(30분 TTL)으로 분류되니 폴더째 드롭을 권장한다. 커뮤니티 도구이며 Anthropic과 무관하다. 논의: [anthropics/claude-code#87966](https://github.com/anthropics/claude-code/issues/87966)
