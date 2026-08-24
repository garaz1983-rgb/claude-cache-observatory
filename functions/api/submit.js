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
 *   4. bot commit via GitHub Contents API (409 => re-GET + one retry)
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

const DATA_PATH = "data/submissions.json";

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

/* ---------- step 4: bot commit via GitHub Contents API ---------- */

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

/* One stored row, always written in the same field order so a diff of the
   public file reads as a diff of the numbers. */
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
  rec.daily = fields.daily;
  rec.script_version = fields.script_version;
  if (identity) rec.identity = identity;
  return rec;
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
function mergeRecord(existing, incoming) {
  const byDate = new Map();
  const exDaily = Array.isArray(existing.daily) ? existing.daily : [];
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

/* The match, the merge and the write are all inside the retry loop on purpose:
   a 409 means somebody else's commit landed in between, and after the re-GET
   the answer to "does this machine already have a row" may have changed. */
async function commitSubmission(env, incoming, ident, issued, now) {
  const token = env.GITHUB_TOKEN;
  const repo = env.GITHUB_REPO;
  if (!token || !repo) return { ok: false };
  const base = (env.GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
  const url = base + "/repos/" + repo + "/contents/" + DATA_PATH;
  const today = now.toISOString().slice(0, 10); // truncated to the day

  // attempt 0 = normal path; attempt 1 = the single re-GET retry after a 409.
  for (let attempt = 0; attempt < 2; attempt++) {
    let sha = null;
    let doc = { schema_version: 1, submissions: [] };
    let got;
    try {
      got = await ghFetch(url, token, { method: "GET" });
    } catch (e) {
      return { ok: false };
    }
    if (got.status === 200) {
      let meta;
      try {
        meta = await got.json();
        doc = JSON.parse(b64DecodeUtf8(meta.content));
      } catch (e) {
        return { ok: false };
      }
      sha = meta.sha;
      if (!isPlainObject(doc) || !Array.isArray(doc.submissions)) return { ok: false };
    } else if (got.status !== 404) {
      return { ok: false }; // 404 = first-ever submission; anything else is storage failure
    }

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

    let record;
    if (merged) {
      const fields = mergeRecord(previous, incoming);
      const submittedAt = typeof previous.submitted_at === "string" &&
        /^\d{4}-\d{2}-\d{2}$/.test(previous.submitted_at) ? previous.submitted_at : today;
      const id = typeof previous.id === "string" && previous.id ? previous.id
        : newSubmissionId(now);
      record = composeRecord(id, submittedAt, today, fields, identity);
      doc.submissions[idx] = record;   // in place: the row keeps its slot and its id
    } else {
      record = composeRecord(newSubmissionId(now), today, "", incoming, identity);
      doc.submissions.push(record);
    }

    const putBody = {
      message: commitMessage(record, merged),
      content: b64EncodeUtf8(JSON.stringify(doc, null, 2) + "\n")
    };
    if (sha) putBody.sha = sha;

    let put;
    try {
      put = await ghFetch(url, token, { method: "PUT", body: JSON.stringify(putBody) });
    } catch (e) {
      return { ok: false };
    }
    if (put.status === 200 || put.status === 201) {
      let out = null;
      try { out = await put.json(); } catch (e) { /* commit landed; url optional */ }
      const commitUrl = out && out.commit && out.commit.html_url ? out.commit.html_url : "";
      return {
        ok: true,
        commitUrl: commitUrl,
        id: record.id,
        merged: merged,
        periodStart: record.period_start,
        periodEnd: record.period_end,
        token: needsToken ? issued.token : ""
      };
    }
    if (put.status !== 409) break; // only a sha conflict earns the single retry
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
