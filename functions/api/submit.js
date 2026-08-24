/*
 * /api/submit — the observatory's single Pages Function (Milestone 3).
 *
 * Contract: design-docs/06_FUNCTIONAL_SPEC.md §2 (SSOT — do not drift).
 * Schema:   design-docs/04_DATA_MODEL.md (whitelist — undefined field => 400).
 *
 * Processing order (fixed by the contract):
 *   1. schema whitelist validation  -> 400 {ok:false, error:"schema", detail}
 *   2. sanity validation            -> 400 (same shape)
 *   3. rate limit (KV only)         -> 429 {ok:false, error:"rate_limited", retry_after}
 *   4. bot commit via GitHub Git Data API (ref moved => re-read + one retry)
 *                                   -> 502 {ok:false, error:"storage"} on failure
 *   5. 200 {ok:true, id, commit_url, merged, period_start, period_end[, token]}
 *
 * Hard rules:
 *   - No module-scope mutable state: rate limiting lives in KV (env.RATE_LIMIT).
 *   - No tokens/secrets in code: env.GITHUB_TOKEN / env.GITHUB_REPO only.
 *   - Raw IPs are never stored — sha256(ip) truncated to 16 hex chars.
 *   - Raw nicknames are never stored — see maskNickname() below. The schema's
 *     field list is unchanged; only the value written into `nickname` is.
 *   - env.GITHUB_API_BASE overrides the API base for tests
 *     (default https://api.github.com).
 *   - env.GITHUB_BRANCH names the branch the bot commits to (default master).
 *     The Contents API defaulted to the repository's default branch on its
 *     own; the Git Data API has to be told, so wrangler.toml declares it and
 *     the value stays publicly auditable like every other binding.
 *
 * ---------- M13: one submitter, one row ----------
 *
 * Until M13 every submission appended a row and the observatory summed them,
 * so a person who submitted twice was counted twice. Identity is now resolved
 * before the write, in two layers:
 *
 *   layer 1 (preferred) `anchors` — sha256 of a deterministic sample of the
 *     machine's own requestIds, hashed in the browser (assets/identity.js).
 *     Nothing derived from the browser: a cleared store, a private window or a
 *     different browser all still resolve to the same machine.
 *   layer 2 (fallback)  `token` — a secret this API issued to a submitter who
 *     had no anchors to send (the pasted-CLI path carries aggregates only).
 *
 * A match updates that row IN PLACE. It never appends a superseding row: this
 * file is public and its credibility rests on a reader being able to add up
 * what they see, and a file that only sums correctly if you know a hidden rule
 * is the failure this milestone exists to fix. Git history is the audit trail.
 *
 * 🔴 data/submissions.json is PUBLIC, so nothing a reader of it can copy may
 * be replayable. The record therefore stores a SECOND hash of what the client
 * sends (identity.anchor_hashes = sha256("cco.anchor2.v1|" + anchor)) and only
 * the hash of the token (identity.token_hash). Sending a value lifted out of
 * the public file hashes to something else and matches nothing; forging a
 * match needs the machine's own logs. Both fields are server-generated: a
 * client that puts `identity`, `anchor_hashes`, `token_hash` or `updated_at`
 * in its payload is rejected by the whitelist above, like any unknown field.
 *
 * An update MERGES (mergeRecord below): daily rows are unioned by date with
 * the incoming row winning, the period widens to the union, and totals are
 * recomputed from the merged daily rows so the file's own arithmetic stays
 * true. M10's incremental path depends on this — replacing the row outright
 * would let a 3-day increment wipe a 3-month record.
 *
 * ---------- M14: three files, one commit ----------
 *
 * Until M14 everything above lived in ONE public file, daily rows included,
 * and index.html downloaded all of it to render four headline numbers. One
 * daily row is 99 bytes and a machine-year is ~35 KB, so 1000 submitters would
 * have been a 34 MB download — and long before that the write path itself
 * would have stopped: the Contents API does not return inline `content` for a
 * file over 1 MB, so the read would have parsed empty and every submission
 * would have failed with nothing on screen to explain it. (That limit is
 * GitHub's documented behaviour; it was never reproduced against the live API,
 * because the fix is to leave that endpoint rather than to walk up to it.)
 *
 * The dataset is now three kinds of file:
 *
 *   data/submissions.json  the INDEX. Same path, so existing links and the
 *                          "one submission is one commit" story survive, but
 *                          the row carries no `daily` array any more — only
 *                          `daily_days` (how many rows its detail file holds)
 *                          and `detail` (where that file is). Measured: 508
 *                          bytes for a row with no fingerprint, 1,901 with one
 *                          — about two thirds of that is identity.anchor_hashes,
 *                          16 digests at 64 hex characters, which makes the
 *                          identity block the index's dominant growth term.
 *   data/daily.json        the FLEET series: one row per calendar DATE, summed
 *                          across every submission, plus how many machines
 *                          reported that date. Bounded by the calendar, not by
 *                          the number of submitters — 96.3 bytes a day measured,
 *                          so ~34 KB a year whether 1 person submits or 1000.
 *   data/subs/<id>.json    one submission's per-day detail. Written on every
 *                          submission, linked from the page, fetched by nobody
 *                          automatically. 80.7 bytes a day, ~29 KB per
 *                          machine-year, and unbounded per machine on purpose:
 *                          that history is the record.
 *
 * Storage moved off the Contents API onto the Git Data API (blob -> tree ->
 * commit -> ref) for two reasons, both required:
 *
 *   - the blob endpoint has no 1 MB inline cap, so the wall is gone rather
 *     than pushed further away;
 *   - a submission writes THREE files and they must land TOGETHER. Three
 *     sequential Contents-API PUTs would be three commits with no atomicity,
 *     and a failure between them leaves the fleet series counting a submission
 *     the index does not list, or a detail file nothing points at. One tree,
 *     one commit, one ref update.
 *
 * The single retry is unchanged in spirit: a ref that moved under us (422 on
 * the ref update, the equivalent of the old 409) earns one re-read, and the
 * match, the merge and the fleet delta all re-run against the FRESH content.
 * Nothing is merged against a stale read.
 *
 * 🔴 Whatever a file claims must add up inside itself, and the three must
 * agree with each other: the index row's totals equal the sum of its own
 * detail file, and data/daily.json equals the sum across all detail files.
 * tests/dataset_validate.py is the single definition of that, and the contract
 * test runs it after every accepted submission.
 */

const MAX_BODY_BYTES = 64 * 1024;
const MAX_NICKNAME = 20;
const MAX_PERIOD_DAYS = 92;
const MAX_DAILY_ENTRIES = 92;
const RATE_LIMIT_MAX = 3; // submissions per ip-hash per hour
const RATE_LIMIT_TTL_SECONDS = 7200; // 2h

const PLAN_ENUM = ["max20x", "max5x", "pro", "team", "api", "unknown"];
const CLIENT_ENUM = ["cli", "ide", "desktop", "web", "mixed", "unknown"];
const SESSIONS_ENUM = ["single", "multi", "unknown"];

const TOP_FIELDS = [
  "nickname", "plan", "client", "concurrent_sessions",
  "period_start", "period_end", "totals", "daily", "script_version",
  "anchors", "token"
];
const TOP_REQUIRED = [
  "plan", "client", "concurrent_sessions",
  "period_start", "period_end", "totals", "daily", "script_version"
];
const TOTALS_FIELDS = ["requests", "in_ttl_losses", "iron_losses", "wasted_tokens"];
const DAILY_FIELDS = ["date", "requests", "losses", "wasted_tokens"];

/* Identity (M13). MAX_ANCHORS mirrors assets/identity.js ANCHOR_COUNT.
   The two prefixes are domain separation, not secrets: they keep an anchor
   hash and a token hash from ever being the same digest of the same string. */
const MAX_ANCHORS = 16;
const HEX64_RE = /^[0-9a-f]{64}$/;
const TOKEN_RE = /^[0-9a-f]{32}$/;
const ANCHOR_STORE_PREFIX = "cco.anchor2.v1|";
const TOKEN_STORE_PREFIX = "cco.token.v1|";
const TOKEN_BYTES = 16;

/* M14 layout. SUB_ID_RE is not cosmetic: the id becomes a file NAME under
   data/subs/, so an id that is not exactly this shape is refused rather than
   sanitised. Every id this file mints matches it by construction; the check
   exists for ids read back out of the public index, which a repo admin can
   hand-edit. */
const INDEX_PATH = "data/submissions.json";
const FLEET_PATH = "data/daily.json";
const SUBS_PREFIX = "data/subs/";
const INDEX_SCHEMA_VERSION = 2;
const FLEET_SCHEMA_VERSION = 1;
const DETAIL_SCHEMA_VERSION = 1;
const SUB_ID_RE = /^sub-[0-9]{14}-[0-9a-f]{4}$/;
const DEFAULT_BRANCH = "master";
const BLOB_MODE = "100644";
const DETAIL_ROW_KEYS = ["date", "requests", "losses", "wasted_tokens"];
const FLEET_ROW_KEYS = ["date", "requests", "losses", "wasted_tokens", "machines"];

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status: status,
    headers: { "Content-Type": "application/json; charset=utf-8" }
  });
}

function schemaError(detail) {
  return jsonResponse(400, { ok: false, error: "schema", detail: detail });
}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function isCount(v) {
  return Number.isSafeInteger(v) && v >= 0;
}

// Strict calendar day: "YYYY-MM-DD" that round-trips. Returns epoch ms or null.
function parseDay(s) {
  if (typeof s !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const t = Date.parse(s + "T00:00:00Z");
  if (Number.isNaN(t)) return null;
  if (new Date(t).toISOString().slice(0, 10) !== s) return null;
  return t;
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* ---------- nickname masking (M11) ----------
 *
 * Masking happens HERE, at storage time, not in the page. data/submissions.json
 * is a public file in a public repo: hiding a nickname while rendering, with the
 * raw value sitting one click away in the JSON, is not protection but the
 * appearance of it. So the raw value reaches neither the stored record, nor the
 * commit message, nor the API response.
 *
 * Rule, applied to the trimmed value over Unicode CODE POINTS (Array.from), so
 * an emoji or a Hangul syllable is never cut into half a surrogate pair:
 *   - 0 code points  -> ""      (caller turns this into "anonymous", as before)
 *   - 1 code point   -> "***"   (keeping it would be keeping the whole value)
 *   - 2 or more      -> first code point + "***"
 *
 * The mask is a FIXED three asterisks, never one per hidden character: a
 * per-character mask would publish the original length, which the rule is
 * meant to withhold together with the string itself.
 *
 * One code point is the minimum a person needs to spot a row as plausibly
 * theirs; the reliable route to "which row is mine" is not the nickname at all
 * but the submission id the browser stored locally (assets/store.js), which the
 * observatory uses to highlight the row.
 *
 * Order matters: mask first, escape after. Escaping first would let the slice
 * cut an entity such as &amp; in half.
 * Idempotent: masking "g***" again yields "g***".
 */
const NICK_MASK = "***";

function maskNickname(raw) {
  const cps = Array.from(raw);
  if (cps.length === 0) return "";
  if (cps.length === 1) return NICK_MASK;
  return cps[0] + NICK_MASK;
}

/* ---------- step 1: whitelist schema validation ---------- */

function validateSchema(body) {
  const errors = [];
  if (!isPlainObject(body)) {
    return ["body must be a JSON object"];
  }
  for (const key of Object.keys(body)) {
    if (TOP_FIELDS.indexOf(key) === -1) {
      errors.push("undefined field: " + key);
    }
  }
  for (const key of TOP_REQUIRED) {
    if (!(key in body)) errors.push("missing field: " + key);
  }
  if (errors.length) return errors;

  if ("nickname" in body) {
    const nickname = body.nickname;
    if (typeof nickname !== "string") {
      errors.push("nickname: must be a string");
    } else if (nickname.length > MAX_NICKNAME) {
      errors.push("nickname: longer than " + MAX_NICKNAME + " characters");
    } else if (/[\u0000-\u001F\u007F]/.test(nickname)) {
      errors.push("nickname: control characters not allowed");
    }
  }
  if (PLAN_ENUM.indexOf(body.plan) === -1) {
    errors.push("plan: must be one of " + PLAN_ENUM.join("/"));
  }
  if (CLIENT_ENUM.indexOf(body.client) === -1) {
    errors.push("client: must be one of " + CLIENT_ENUM.join("/"));
  }
  if (SESSIONS_ENUM.indexOf(body.concurrent_sessions) === -1) {
    errors.push("concurrent_sessions: must be one of " + SESSIONS_ENUM.join("/"));
  }
  if (parseDay(body.period_start) === null) {
    errors.push("period_start: must be a valid YYYY-MM-DD date");
  }
  if (parseDay(body.period_end) === null) {
    errors.push("period_end: must be a valid YYYY-MM-DD date");
  }

  if (!isPlainObject(body.totals)) {
    errors.push("totals: must be an object");
  } else {
    for (const key of Object.keys(body.totals)) {
      if (TOTALS_FIELDS.indexOf(key) === -1) {
        errors.push("undefined field: totals." + key);
      }
    }
    for (const key of TOTALS_FIELDS) {
      if (!isCount(body.totals[key])) {
        errors.push("totals." + key + ": must be a non-negative integer");
      }
    }
  }

  if (!Array.isArray(body.daily)) {
    errors.push("daily: must be an array");
  } else {
    if (body.daily.length > MAX_DAILY_ENTRIES) {
      errors.push("daily: more than " + MAX_DAILY_ENTRIES + " entries");
    }
    body.daily.forEach(function (entry, i) {
      if (!isPlainObject(entry)) {
        errors.push("daily[" + i + "]: must be an object");
        return;
      }
      for (const key of Object.keys(entry)) {
        if (DAILY_FIELDS.indexOf(key) === -1) {
          errors.push("undefined field: daily[" + i + "]." + key);
        }
      }
      if (parseDay(entry.date) === null) {
        errors.push("daily[" + i + "].date: must be a valid YYYY-MM-DD date");
      }
      for (const key of ["requests", "losses", "wasted_tokens"]) {
        if (!isCount(entry[key])) {
          errors.push("daily[" + i + "]." + key + ": must be a non-negative integer");
        }
      }
    });
  }

  if (typeof body.script_version !== "string" ||
      body.script_version.length === 0 ||
      body.script_version.length > 32 ||
      !/^[A-Za-z0-9._-]+$/.test(body.script_version)) {
    errors.push("script_version: must be a short version tag (e.g. web-1.0)");
  }

  /* M13 identity. Both are optional and both are shape-checked hard: an
     anchor is a lowercase sha-256 digest and nothing else, so no free text
     can ride into the public file through this door. The record's own
     identity fields are NOT in TOP_FIELDS, so a client that tries to send a
     stored hash is rejected by the undefined-field loop above. */
  if ("anchors" in body) {
    if (!Array.isArray(body.anchors)) {
      errors.push("anchors: must be an array");
    } else if (body.anchors.length > MAX_ANCHORS) {
      errors.push("anchors: more than " + MAX_ANCHORS + " entries");
    } else {
      body.anchors.forEach(function (a, i) {
        if (typeof a !== "string" || !HEX64_RE.test(a)) {
          errors.push("anchors[" + i + "]: must be a lowercase sha-256 hex digest");
        }
      });
    }
  }
  if ("token" in body) {
    if (typeof body.token !== "string" || !TOKEN_RE.test(body.token)) {
      errors.push("token: must be a 32-character lowercase hex string");
    }
  }
  return errors;
}

/* ---------- step 2: sanity validation ---------- */

function validateSanity(body) {
  const errors = [];
  const t = body.totals;
  if (t.in_ttl_losses > t.requests) {
    errors.push("totals.in_ttl_losses exceeds totals.requests");
  }
  if (t.iron_losses > t.in_ttl_losses) {
    errors.push("totals.iron_losses exceeds totals.in_ttl_losses");
  }

  const start = parseDay(body.period_start);
  const end = parseDay(body.period_end);
  if (start > end) {
    errors.push("period_start is after period_end");
  } else {
    const spanDays = (end - start) / 86400000 + 1;
    if (spanDays > MAX_PERIOD_DAYS) {
      errors.push("period spans " + spanDays + " days (max " + MAX_PERIOD_DAYS + ")");
    }
  }

  if (body.daily.length === 0) {
    errors.push("daily: must have at least 1 entry");
  }
  const seenDates = new Set();
  let sumRequests = 0;
  let sumLosses = 0;
  let sumWasted = 0;
  body.daily.forEach(function (entry, i) {
    const day = parseDay(entry.date);
    if (entry.losses > entry.requests) {
      errors.push("daily[" + i + "].losses exceeds daily[" + i + "].requests");
    }
    if (start <= end && (day < start || day > end)) {
      errors.push("daily[" + i + "].date is outside the submission period");
    }
    if (seenDates.has(entry.date)) {
      errors.push("daily[" + i + "].date duplicates " + entry.date);
    }
    seenDates.add(entry.date);
    sumRequests += entry.requests;
    sumLosses += entry.losses;
    sumWasted += entry.wasted_tokens;
  });
  // daily must sum to totals — blocks inflating totals alone (codex review).
  if (sumRequests !== t.requests) {
    errors.push("daily requests sum " + sumRequests +
      " != totals.requests " + t.requests);
  }
  if (sumLosses !== t.in_ttl_losses) {
    errors.push("daily losses sum " + sumLosses +
      " != totals.in_ttl_losses " + t.in_ttl_losses);
  }
  if (sumWasted !== t.wasted_tokens) {
    errors.push("daily wasted_tokens sum " + sumWasted +
      " != totals.wasted_tokens " + t.wasted_tokens);
  }
  return errors;
}

/* ---------- step 3: rate limit (KV only — no in-memory state) ---------- */

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest))
    .map(function (b) { return b.toString(16).padStart(2, "0"); })
    .join("");
}

async function checkRateLimit(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") ||
    (request.headers.get("X-Forwarded-For") || "").split(",")[0].trim() ||
    "unknown";
  const ipHash = (await sha256Hex(ip)).slice(0, 16); // raw IP never stored
  const now = new Date();
  const hourKey = now.toISOString().slice(0, 13).replace(/[-T]/g, ""); // yyyymmddhh (UTC)
  const key = "rl:" + ipHash + ":" + hourKey;
  const retryAfter = 3600 - (Math.floor(now.getTime() / 1000) % 3600);
  const count = parseInt((await env.RATE_LIMIT.get(key)) || "0", 10) || 0;
  if (count >= RATE_LIMIT_MAX) {
    return { allowed: false, retryAfter: retryAfter };
  }
  await env.RATE_LIMIT.put(key, String(count + 1), { expirationTtl: RATE_LIMIT_TTL_SECONDS });
  // KV read-modify-write is not atomic; bursts can exceed the limit. Accepted
  // limitation (defense = schema + public revert). Narrowed by post-increment
  // re-check.
  const recheck = parseInt((await env.RATE_LIMIT.get(key)) || "0", 10) || 0;
  if (recheck > RATE_LIMIT_MAX) {
    return { allowed: false, retryAfter: retryAfter };
  }
  return { allowed: true };
}

/* ---------- step 4: bot commit via the GitHub Git Data API ---------- */

function b64EncodeUtf8(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function b64DecodeUtf8(b64) {
  const binary = atob(String(b64).replace(/\s+/g, ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

function randomHex(bytes) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return Array.from(buf)
    .map(function (b) { return b.toString(16).padStart(2, "0"); })
    .join("");
}

function newSubmissionId(now) {
  const stamp = now.toISOString().slice(0, 19).replace(/[-T:]/g, ""); // yyyymmddHHMMSS
  return "sub-" + stamp + "-" + randomHex(2);
}

/* The validated payload, normalised. No id and no submitted_at: whether this
   becomes a new row or is merged into one is decided against the file that
   comes back from GitHub, inside the commit loop. */
function buildIncoming(body) {
  // The raw nickname is read, masked and dropped. It is never assigned to the
  // record, so nothing downstream (commit message included) can reach it.
  const masked = maskNickname(typeof body.nickname === "string" ? body.nickname.trim() : "");
  const nickname = masked === "" ? "anonymous" : escapeHtml(masked);
  return {
    nickname: nickname,
    plan: body.plan,
    client: body.client,
    concurrent_sessions: body.concurrent_sessions,
    period_start: body.period_start,
    period_end: body.period_end,
    totals: {
      requests: body.totals.requests,
      in_ttl_losses: body.totals.in_ttl_losses,
      iron_losses: body.totals.iron_losses,
      wasted_tokens: body.totals.wasted_tokens
    },
    daily: body.daily.map(function (d) {
      return {
        date: d.date,
        requests: d.requests,
        losses: d.losses,
        wasted_tokens: d.wasted_tokens
      };
    }),
    script_version: body.script_version
  };
}

/* One stored INDEX row, always written in the same field order so a diff of the
   public file reads as a diff of the numbers.

   M14: `daily` is gone from here and `daily_days` + `detail` take its place.
   The day count is not decoration — it is what lets a reader check that the
   detail file at that path is complete without the index having to carry it. */
function composeRecord(id, submittedAt, updatedAt, fields, identity) {
  const rec = { id: id, submitted_at: submittedAt };
  if (updatedAt) rec.updated_at = updatedAt;
  rec.nickname = fields.nickname;
  rec.plan = fields.plan;
  rec.client = fields.client;
  rec.concurrent_sessions = fields.concurrent_sessions;
  rec.period_start = fields.period_start;
  rec.period_end = fields.period_end;
  rec.totals = fields.totals;
  rec.daily_days = fields.daily.length;
  rec.detail = detailPath(id);
  rec.script_version = fields.script_version;
  if (identity) rec.identity = identity;
  return rec;
}

function detailPath(id) {
  return SUBS_PREFIX + id + ".json";
}

/* One submission's per-day detail. It repeats the period and the totals on
   purpose: a reader who opens only this file can still add its own rows up and
   check them, and a reader who has both can check the two against each other. */
function buildDetail(id, fields) {
  return {
    schema_version: DETAIL_SCHEMA_VERSION,
    id: id,
    period_start: fields.period_start,
    period_end: fields.period_end,
    totals: fields.totals,
    daily: fields.daily
  };
}

/* ---------- serialisation ----------
 *
 * The index is ordinary 2-space JSON: it holds one record per submitter and
 * those are read as records.
 *
 * The two files that grow with TIME are written one row per line instead. Two
 * reasons, both about the reader: `git log -p data/daily.json` then reads as a
 * list of the days that changed rather than a five-line block per day, and the
 * file is about a quarter smaller than the same rows pretty-printed (measured
 * on the migrated dataset: 89 days, 11,779 bytes pretty vs 8,575 as lines —
 * 96 bytes a day). It is still plain JSON; nothing needs a custom parser.
 */
function inlineRow(obj, keys) {
  const parts = [];
  for (let i = 0; i < keys.length; i++) {
    parts.push(JSON.stringify(keys[i]) + ": " + JSON.stringify(obj[keys[i]]));
  }
  return "{" + parts.join(", ") + "}";
}

/* head: [[key, already-encoded value], ...] written at 2-space indent, then
   `arrayKey` with one encoded row per line. */
function serializeRowFile(head, arrayKey, rows, rowKeys) {
  const out = ["{"];
  for (let i = 0; i < head.length; i++) {
    out.push("  " + JSON.stringify(head[i][0]) + ": " + head[i][1] + ",");
  }
  if (!rows.length) {
    out.push("  " + JSON.stringify(arrayKey) + ": []");
  } else {
    out.push("  " + JSON.stringify(arrayKey) + ": [");
    for (let i = 0; i < rows.length; i++) {
      out.push("    " + inlineRow(rows[i], rowKeys) +
        (i === rows.length - 1 ? "" : ","));
    }
    out.push("  ]");
  }
  out.push("}");
  return out.join("\n") + "\n";
}

function serializeIndex(doc) {
  return JSON.stringify(doc, null, 2) + "\n";
}

function serializeDetail(detail) {
  return serializeRowFile([
    ["schema_version", JSON.stringify(detail.schema_version)],
    ["id", JSON.stringify(detail.id)],
    ["period_start", JSON.stringify(detail.period_start)],
    ["period_end", JSON.stringify(detail.period_end)],
    ["totals", inlineRow(detail.totals, TOTALS_FIELDS)]
  ], "daily", detail.daily, DETAIL_ROW_KEYS);
}

function serializeFleet(fleet) {
  return serializeRowFile([
    ["schema_version", JSON.stringify(fleet.schema_version)]
  ], "days", fleet.days, FLEET_ROW_KEYS);
}

function commitMessage(submission, merged) {
  return "data: " + (merged ? "update" : "submission") + " " + submission.id +
    " — " + submission.nickname +
    ", " + submission.period_start + "~" + submission.period_end +
    ", " + submission.totals.in_ttl_losses + " losses / " +
    submission.totals.requests + " req";
}

/* ---------- M13: identity resolution ---------- */

/* What the client sent, hashed once more. `anchorHashes` is what gets stored
   and compared; the anchors themselves are never written anywhere. */
async function identityOf(body) {
  const anchorHashes = [];
  const anchors = Array.isArray(body.anchors) ? body.anchors : [];
  for (let i = 0; i < anchors.length && i < MAX_ANCHORS; i++) {
    anchorHashes.push(await sha256Hex(ANCHOR_STORE_PREFIX + anchors[i]));
  }
  const token = typeof body.token === "string" ? body.token : "";
  const tokenHash = token ? await sha256Hex(TOKEN_STORE_PREFIX + token) : "";
  return { anchorHashes: anchorHashes, tokenHash: tokenHash };
}

function storedAnchors(record) {
  const id = record && isPlainObject(record.identity) ? record.identity : null;
  if (!id || !Array.isArray(id.anchor_hashes)) return [];
  return id.anchor_hashes.filter(function (h) {
    return typeof h === "string" && HEX64_RE.test(h);
  });
}

function storedTokenHash(record) {
  const id = record && isPlainObject(record.identity) ? record.identity : null;
  if (!id || typeof id.token_hash !== "string" || !HEX64_RE.test(id.token_hash)) return "";
  return id.token_hash;
}

/* Which row this submission belongs to, or -1.
   Fingerprint first, browser token second: the fingerprint is anchored to the
   machine whose logs are being reported, the token only to a browser. ANY
   single anchor overlap is the same machine — the sample drifts as logs are
   written and rotated, so requiring more than one would lose the link on a
   normal week of use. */
function matchIndex(subs, anchorHashes, tokenHash) {
  if (!Array.isArray(subs)) return -1;
  if (anchorHashes.length) {
    const want = new Set(anchorHashes);
    for (let i = 0; i < subs.length; i++) {
      const have = storedAnchors(subs[i]);
      for (let j = 0; j < have.length; j++) {
        if (want.has(have[j])) return i;
      }
    }
  }
  if (tokenHash) {
    for (let i = 0; i < subs.length; i++) {
      if (storedTokenHash(subs[i]) === tokenHash) return i;
    }
  }
  return -1;
}

/* Anchors refresh to the newest set on every update, so the fingerprint tracks
   the log folder's drift instead of going stale. A submission with no anchors
   (the pasted path) leaves the stored ones alone rather than erasing them. */
function buildIdentity(previous, ident, issuedTokenHash) {
  const prevAnchors = previous && Array.isArray(previous.anchor_hashes)
    ? previous.anchor_hashes.filter(function (h) {
        return typeof h === "string" && HEX64_RE.test(h);
      })
    : [];
  const anchors = ident.anchorHashes.length ? ident.anchorHashes : prevAnchors;
  const prevToken = previous && typeof previous.token_hash === "string" &&
    HEX64_RE.test(previous.token_hash) ? previous.token_hash : "";
  const tokenHash = prevToken || issuedTokenHash || "";
  if (!anchors.length && !tokenHash) return null;
  const out = {};
  if (anchors.length) out.anchor_hashes = anchors;
  if (tokenHash) out.token_hash = tokenHash;
  return out;
}

/* ---------- M13: the merge ---------- */

function dailyRowOf(d) {
  if (!isPlainObject(d) || parseDay(d.date) === null) return null;
  if (!isCount(d.requests) || !isCount(d.losses) || !isCount(d.wasted_tokens)) return null;
  return { date: d.date, requests: d.requests, losses: d.losses, wasted_tokens: d.wasted_tokens };
}

/* Union the daily rows by date and recompute the totals from the result.
 *
 * The incoming row wins for a date both cover: it is the fresher measurement
 * of the same day. `iron_losses` is the one total that cannot be recomputed —
 * there is no per-day iron column — so it is carried conservatively:
 *
 *     kept = max(0, existing.iron_losses - losses on the superseded days)
 *
 * which is EXACT in both flows that matter (a disjoint increment supersedes
 * nothing, so all of it is kept; a full re-scan supersedes every existing day,
 * so none of it is and the fresh count stands alone) and never over-claims in
 * between. Iron is the worst subset of the losses, so under-counting it is the
 * direction that cannot flatter the site's own headline.
 */
/* M14: the existing daily rows arrive as their own argument, because they no
   longer live on the index row — they live in data/subs/<id>.json, which the
   caller has to read before it may merge. Passing them in rather than reaching
   for `existing.daily` is what makes it impossible to "merge" against a row
   whose history was never loaded and silently rewrite its totals downward. */
function mergeRecord(existing, existingDaily, incoming) {
  const byDate = new Map();
  const exDaily = Array.isArray(existingDaily) ? existingDaily : [];
  let existingLosses = 0;
  for (let i = 0; i < exDaily.length; i++) {
    const row = dailyRowOf(exDaily[i]);
    if (!row) continue;               // a hand-edited file must not poison the merge
    byDate.set(row.date, row);
    existingLosses += row.losses;
  }
  let supersededLosses = 0;
  for (let i = 0; i < incoming.daily.length; i++) {
    const row = incoming.daily[i];
    const prev = byDate.get(row.date);
    if (prev) supersededLosses += prev.losses;
    byDate.set(row.date, { date: row.date, requests: row.requests,
                           losses: row.losses, wasted_tokens: row.wasted_tokens });
  }
  const daily = Array.from(byDate.values()).sort(function (a, b) {
    return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0);
  });

  const totals = { requests: 0, in_ttl_losses: 0, iron_losses: 0, wasted_tokens: 0 };
  for (let i = 0; i < daily.length; i++) {
    totals.requests += daily[i].requests;
    totals.in_ttl_losses += daily[i].losses;
    totals.wasted_tokens += daily[i].wasted_tokens;
  }
  const exTotals = isPlainObject(existing.totals) ? existing.totals : {};
  let exIron = isCount(exTotals.iron_losses) ? exTotals.iron_losses : 0;
  if (exIron > existingLosses) exIron = existingLosses;
  const keptIron = Math.max(0, exIron - supersededLosses);
  totals.iron_losses = Math.min(keptIron + incoming.totals.iron_losses,
                                totals.in_ttl_losses);

  // The period widens to the union of both, never narrows: a 3-day increment
  // must not shrink a 3-month record.
  const starts = [incoming.period_start];
  const ends = [incoming.period_end];
  if (parseDay(existing.period_start) !== null) starts.push(existing.period_start);
  if (parseDay(existing.period_end) !== null) ends.push(existing.period_end);
  if (daily.length) {
    starts.push(daily[0].date);
    ends.push(daily[daily.length - 1].date);
  }
  starts.sort();
  ends.sort();

  return {
    nickname: incoming.nickname,
    plan: incoming.plan,
    client: incoming.client,
    concurrent_sessions: incoming.concurrent_sessions,
    period_start: starts[0],
    period_end: ends[ends.length - 1],
    totals: totals,
    daily: daily,
    script_version: incoming.script_version
  };
}

/* ---------- M14: the fleet-wide daily series ----------
 *
 * data/daily.json is the sum across every submission, one row per calendar
 * date. It is maintained as a DELTA, not recomputed: recomputing it would mean
 * reading every submitter's detail file on every submission, which is the exact
 * cost this milestone exists to remove. Applying a delta is O(this row's days)
 * no matter how many people have submitted.
 *
 * The delta is exact because both halves are known at the same instant: the
 * row's OLD daily rows (just read from its detail file, empty for a new row)
 * come out, its NEW daily rows go in, and `machines` moves by -1/+1 per date.
 * The merge unions dates, so a date a row already covered nets to zero and a
 * genuinely new date nets to +1.
 *
 * Delta arithmetic is inductive: it keeps the file correct if the file was
 * correct. tests/dataset_validate.py is the check that closes that induction —
 * it recomputes the whole series from the detail files and compares.
 */
function fleetRowOf(d) {
  if (!isPlainObject(d) || parseDay(d.date) === null) return null;
  return {
    date: d.date,
    requests: isCount(d.requests) ? d.requests : 0,
    losses: isCount(d.losses) ? d.losses : 0,
    wasted_tokens: isCount(d.wasted_tokens) ? d.wasted_tokens : 0,
    machines: isCount(d.machines) ? d.machines : 0
  };
}

function applyFleetDelta(fleet, oldDaily, newDaily) {
  const byDate = new Map();
  const days = fleet && Array.isArray(fleet.days) ? fleet.days : [];
  for (let i = 0; i < days.length; i++) {
    const row = fleetRowOf(days[i]);
    if (!row) continue;            // a hand-edited file must not poison the series
    byDate.set(row.date, row);
  }
  for (let i = 0; i < oldDaily.length; i++) {
    const r = oldDaily[i];
    const cur = byDate.get(r.date);
    if (!cur) continue;            // never covered by the series; nothing to take out
    cur.requests -= r.requests;
    cur.losses -= r.losses;
    cur.wasted_tokens -= r.wasted_tokens;
    cur.machines -= 1;
  }
  for (let i = 0; i < newDaily.length; i++) {
    const r = newDaily[i];
    let cur = byDate.get(r.date);
    if (!cur) {
      cur = { date: r.date, requests: 0, losses: 0, wasted_tokens: 0, machines: 0 };
      byDate.set(r.date, cur);
    }
    cur.requests += r.requests;
    cur.losses += r.losses;
    cur.wasted_tokens += r.wasted_tokens;
    cur.machines += 1;
  }
  const out = [];
  byDate.forEach(function (d) {
    // A date no submission covers any more is dropped rather than left at zero:
    // a row reading "0 requests, 0 machines" is a claim about a day nobody
    // observed. The clamps below can only fire on a file that was already
    // wrong, and a negative count in a public file would be worse than a low one.
    if (d.machines <= 0) return;
    out.push({
      date: d.date,
      requests: Math.max(0, d.requests),
      losses: Math.max(0, d.losses),
      wasted_tokens: Math.max(0, d.wasted_tokens),
      machines: d.machines
    });
  });
  out.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });
  return { schema_version: FLEET_SCHEMA_VERSION, days: out };
}

function ghFetch(url, token, options) {
  const headers = {
    "Authorization": "Bearer " + token,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "claude-cache-observatory-bot"
  };
  if (options.body) headers["Content-Type"] = "application/json";
  return fetch(url, { method: options.method, headers: headers, body: options.body });
}

/* Every GitHub call goes through here so a transport failure and an error
   status arrive in the same shape. status 0 = the fetch itself threw. */
async function ghJson(url, token, options) {
  let res;
  try {
    res = await ghFetch(url, token, options || { method: "GET" });
  } catch (e) {
    return { status: 0, body: null };
  }
  let body = null;
  try { body = await res.json(); } catch (e) { /* status is what matters */ }
  return { status: res.status, body: body };
}

/* One entry of a NON-recursive tree listing, where `path` is the bare name.
   The walk is deliberately not `?recursive=1`: that would pull every path in
   the repository into the worker on every submission, and data/subs/ is the
   one directory that grows with the number of submitters. Walked a level at a
   time, the listing of that directory is fetched only when a row is actually
   being merged into. */
function treeEntry(tree, name, type) {
  const list = tree && Array.isArray(tree.tree) ? tree.tree : [];
  for (let i = 0; i < list.length; i++) {
    const e = list[i];
    if (e && e.path === name && e.type === type) return e;
  }
  return null;
}

/* A blob, parsed. `null` means "should be there and is not readable", which is
   always a hard failure; a caller that can tolerate absence checks the sha
   before calling. The blob endpoint carries content up to 100 MB, which is the
   whole reason this file left the Contents API and its 1 MB inline cap. */
async function readJsonBlob(api, token, sha) {
  const got = await ghJson(api + "/git/blobs/" + sha, token);
  if (got.status !== 200 || !got.body || typeof got.body.content !== "string") {
    return null;
  }
  try {
    return JSON.parse(b64DecodeUtf8(got.body.content));
  } catch (e) {
    return null;
  }
}

/* Read the whole write-relevant state of the repository at HEAD:
   ref -> commit -> root tree -> data/ tree -> the two blobs the page reads.
   Returns null on any failure. A file that is simply absent is not a failure:
   an empty index and an empty fleet series are what a repository looks like
   before its first submission. */
async function readState(api, token, branch) {
  const ref = await ghJson(api + "/git/ref/heads/" + branch, token);
  if (ref.status !== 200 || !ref.body || !isPlainObject(ref.body.object)) return null;
  const headSha = ref.body.object.sha;
  if (typeof headSha !== "string" || !headSha) return null;

  const commit = await ghJson(api + "/git/commits/" + headSha, token);
  if (commit.status !== 200 || !commit.body || !isPlainObject(commit.body.tree)) {
    return null;
  }
  const rootTreeSha = commit.body.tree.sha;
  if (typeof rootTreeSha !== "string" || !rootTreeSha) return null;

  const root = await ghJson(api + "/git/trees/" + rootTreeSha, token);
  if (root.status !== 200) return null;

  let indexSha = "", fleetSha = "", subsSha = "";
  const dataDir = treeEntry(root.body, "data", "tree");
  if (dataDir) {
    const dataTree = await ghJson(api + "/git/trees/" + dataDir.sha, token);
    if (dataTree.status !== 200) return null;
    const i = treeEntry(dataTree.body, "submissions.json", "blob");
    const f = treeEntry(dataTree.body, "daily.json", "blob");
    const s = treeEntry(dataTree.body, "subs", "tree");
    if (i) indexSha = i.sha;
    if (f) fleetSha = f.sha;
    if (s) subsSha = s.sha;
  }

  let index = { schema_version: INDEX_SCHEMA_VERSION, submissions: [] };
  if (indexSha) {
    index = await readJsonBlob(api, token, indexSha);
    if (!isPlainObject(index) || !Array.isArray(index.submissions)) return null;
  }
  let fleet = { schema_version: FLEET_SCHEMA_VERSION, days: [] };
  if (fleetSha) {
    fleet = await readJsonBlob(api, token, fleetSha);
    if (!isPlainObject(fleet) || !Array.isArray(fleet.days)) return null;
  }
  return {
    headSha: headSha,
    rootTreeSha: rootTreeSha,
    subsSha: subsSha,
    index: index,
    fleet: fleet
  };
}

/* The daily rows of the row being merged into. Returns null on ANY doubt —
   missing directory, missing file, unreadable file, an id that is not the id
   the file claims. The caller turns null into a 502 instead of merging, because
   a merge against daily rows that failed to load would recompute the row's
   totals from the incoming submission alone and silently delete that machine's
   history from a public file. */
async function readDetailDaily(api, token, subsSha, id) {
  if (!subsSha) return null;
  const listing = await ghJson(api + "/git/trees/" + subsSha, token);
  if (listing.status !== 200) return null;
  const entry = treeEntry(listing.body, id + ".json", "blob");
  if (!entry) return null;
  const detail = await readJsonBlob(api, token, entry.sha);
  if (!isPlainObject(detail) || detail.id !== id || !Array.isArray(detail.daily)) {
    return null;
  }
  return detail.daily;
}

/* blobs -> tree -> commit -> ref, in that order, which is what makes the three
   files ONE commit. `base_tree` is the tree the read came from, so every path
   this commit does not mention is carried over unchanged and GitHub resolves
   the nested directories for us.
   Returns {url} on success, {moved:true} when the ref moved under us (the
   caller's one retry), or null for any other failure. */
async function writeCommit(api, token, branch, state, files, message) {
  const entries = [];
  for (let i = 0; i < files.length; i++) {
    const blob = await ghJson(api + "/git/blobs", token, {
      method: "POST",
      body: JSON.stringify({
        content: b64EncodeUtf8(files[i].text),
        encoding: "base64"
      })
    });
    if (blob.status !== 200 && blob.status !== 201) return null;
    if (!blob.body || typeof blob.body.sha !== "string") return null;
    entries.push({
      path: files[i].path,
      mode: BLOB_MODE,
      type: "blob",
      sha: blob.body.sha
    });
  }

  const tree = await ghJson(api + "/git/trees", token, {
    method: "POST",
    body: JSON.stringify({ base_tree: state.rootTreeSha, tree: entries })
  });
  if (tree.status !== 200 && tree.status !== 201) return null;
  if (!tree.body || typeof tree.body.sha !== "string") return null;

  const commit = await ghJson(api + "/git/commits", token, {
    method: "POST",
    body: JSON.stringify({
      message: message,
      tree: tree.body.sha,
      parents: [state.headSha]
    })
  });
  if (commit.status !== 200 && commit.status !== 201) return null;
  if (!commit.body || typeof commit.body.sha !== "string") return null;

  // force stays false: a submission may only fast-forward the branch. If
  // somebody else's commit landed after our read, this is the 422 that used to
  // be the Contents API's 409, and nothing has been published yet — the blobs
  // and the commit exist as unreferenced objects and no branch points at them.
  const upd = await ghJson(api + "/git/refs/heads/" + branch, token, {
    method: "PATCH",
    body: JSON.stringify({ sha: commit.body.sha, force: false })
  });
  if (upd.status === 200) {
    return { url: typeof commit.body.html_url === "string" ? commit.body.html_url : "" };
  }
  if (upd.status === 422) return { moved: true };
  return null;
}

/* The read, the match, the merge, the fleet delta and the write are all inside
   the retry loop on purpose: a moved ref means somebody else's commit landed in
   between, and after the re-read the answer to "does this machine already have
   a row" — and what that row contains — may both have changed. Nothing computed
   before the conflict is carried across it. */
async function commitSubmission(env, incoming, ident, issued, now) {
  const token = env.GITHUB_TOKEN;
  const repo = env.GITHUB_REPO;
  if (!token || !repo) return { ok: false };
  const base = (env.GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
  const api = base + "/repos/" + repo;
  const branch = env.GITHUB_BRANCH || DEFAULT_BRANCH;
  const today = now.toISOString().slice(0, 10); // truncated to the day

  // attempt 0 = normal path; attempt 1 = the single re-read retry.
  for (let attempt = 0; attempt < 2; attempt++) {
    const state = await readState(api, token, branch);
    if (!state) return { ok: false };
    const doc = state.index;

    const idx = matchIndex(doc.submissions, ident.anchorHashes, ident.tokenHash);
    const merged = idx !== -1;
    const previous = merged ? doc.submissions[idx] : null;
    const prevIdentity = previous && isPlainObject(previous.identity)
      ? previous.identity : null;
    // A token is issued only where a fingerprint is impossible, and only when
    // the row does not already carry one. "Impossible" is decided in exactly
    // one place — the mint in onRequestPost, which leaves issued.tokenHash
    // empty on the fingerprinted path — so there is a single guard to get
    // wrong rather than two that can disagree.
    const needsToken = issued.tokenHash !== "" &&
      !(prevIdentity && typeof prevIdentity.token_hash === "string" &&
        HEX64_RE.test(prevIdentity.token_hash));
    const identity = buildIdentity(prevIdentity, ident,
      needsToken ? issued.tokenHash : "");

    let id, submittedAt, updatedAt, fields, previousDaily;
    if (merged) {
      // The id addresses a FILE now, so a row whose id is not the shape this
      // API mints is not merged into at all. Minting a fresh id here instead
      // would orphan the detail file the old id points at and split one
      // machine across two rows — exactly what M13 removed.
      id = typeof previous.id === "string" && SUB_ID_RE.test(previous.id)
        ? previous.id : "";
      if (!id) return { ok: false };
      previousDaily = await readDetailDaily(api, token, state.subsSha, id);
      if (previousDaily === null) return { ok: false };
      fields = mergeRecord(previous, previousDaily, incoming);
      submittedAt = typeof previous.submitted_at === "string" &&
        /^\d{4}-\d{2}-\d{2}$/.test(previous.submitted_at) ? previous.submitted_at : today;
      updatedAt = today;
    } else {
      id = newSubmissionId(now);
      previousDaily = [];
      fields = incoming;
      submittedAt = today;
      updatedAt = "";
    }

    const record = composeRecord(id, submittedAt, updatedAt, fields, identity);
    if (merged) {
      doc.submissions[idx] = record;   // in place: the row keeps its slot and its id
    } else {
      doc.submissions.push(record);
    }
    doc.schema_version = INDEX_SCHEMA_VERSION;

    const files = [
      { path: INDEX_PATH, text: serializeIndex(doc) },
      { path: FLEET_PATH,
        text: serializeFleet(applyFleetDelta(state.fleet, previousDaily, fields.daily)) },
      { path: detailPath(id), text: serializeDetail(buildDetail(id, fields)) }
    ];

    const written = await writeCommit(api, token, branch, state, files,
      commitMessage(record, merged));
    if (written === null) return { ok: false };
    if (written.moved) continue;      // re-read, re-resolve, re-merge, write once more
    return {
      ok: true,
      commitUrl: written.url,
      id: record.id,
      merged: merged,
      periodStart: record.period_start,
      periodEnd: record.period_end,
      token: needsToken ? issued.token : ""
    };
  }
  return { ok: false };
}

/* ---------- entry point ---------- */

export async function onRequestPost(context) {
  const request = context.request;
  const env = context.env;

  const declared = parseInt(request.headers.get("Content-Length") || "", 10);
  if (declared > MAX_BODY_BYTES) {
    return schemaError(["body exceeds " + MAX_BODY_BYTES + " bytes"]);
  }
  const raw = await request.text();
  if (new TextEncoder().encode(raw).length > MAX_BODY_BYTES) {
    return schemaError(["body exceeds " + MAX_BODY_BYTES + " bytes"]);
  }

  let body;
  try {
    body = JSON.parse(raw);
  } catch (e) {
    return schemaError(["body is not valid JSON"]);
  }

  const schemaErrors = validateSchema(body);
  if (schemaErrors.length) return schemaError(schemaErrors);

  const sanityErrors = validateSanity(body);
  if (sanityErrors.length) return schemaError(sanityErrors);

  const rl = await checkRateLimit(request, env);
  if (!rl.allowed) {
    return jsonResponse(429, { ok: false, error: "rate_limited", retry_after: rl.retryAfter });
  }

  const now = new Date();
  const incoming = buildIncoming(body);
  const ident = await identityOf(body);
  // Minted once, before the loop, so a 409 retry cannot hand the browser a
  // token the stored hash does not correspond to. Used only if the resolved
  // row turns out to need one.
  const issued = { token: "", tokenHash: "" };
  if (ident.anchorHashes.length === 0) {
    issued.token = randomHex(TOKEN_BYTES);
    issued.tokenHash = await sha256Hex(TOKEN_STORE_PREFIX + issued.token);
  }

  const commit = await commitSubmission(env, incoming, ident, issued, now);
  if (!commit.ok) {
    return jsonResponse(502, { ok: false, error: "storage" });
  }
  const out = {
    ok: true,
    id: commit.id,
    commit_url: commit.commitUrl,
    merged: commit.merged,
    period_start: commit.periodStart,
    period_end: commit.periodEnd
  };
  if (commit.token) out.token = commit.token;
  return jsonResponse(200, out);
}
