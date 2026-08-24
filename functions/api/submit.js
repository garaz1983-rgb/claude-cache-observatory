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
 *   5. 200 {ok:true, id, commit_url}
 *
 * Hard rules:
 *   - No module-scope mutable state: rate limiting lives in KV (env.RATE_LIMIT).
 *   - No tokens/secrets in code: env.GITHUB_TOKEN / env.GITHUB_REPO only.
 *   - Raw IPs are never stored — sha256(ip) truncated to 16 hex chars.
 *   - env.GITHUB_API_BASE overrides the API base for tests
 *     (default https://api.github.com).
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
  "period_start", "period_end", "totals", "daily", "script_version"
];
const TOP_REQUIRED = [
  "plan", "client", "concurrent_sessions",
  "period_start", "period_end", "totals", "daily", "script_version"
];
const TOTALS_FIELDS = ["requests", "in_ttl_losses", "iron_losses", "wasted_tokens"];
const DAILY_FIELDS = ["date", "requests", "losses", "wasted_tokens"];

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

function randomHex4() {
  const buf = new Uint8Array(2);
  crypto.getRandomValues(buf);
  return Array.from(buf)
    .map(function (b) { return b.toString(16).padStart(2, "0"); })
    .join("");
}

function buildSubmission(body, now) {
  const stamp = now.toISOString().slice(0, 19).replace(/[-T:]/g, ""); // yyyymmddHHMMSS
  const rawNickname = typeof body.nickname === "string" ? body.nickname.trim() : "";
  const nickname = rawNickname === "" ? "anonymous" : escapeHtml(rawNickname);
  return {
    id: "sub-" + stamp + "-" + randomHex4(),
    submitted_at: now.toISOString().slice(0, 10), // truncated to the day
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

function commitMessage(submission) {
  return "data: submission " + submission.id + " — " + submission.nickname +
    ", " + submission.period_start + "~" + submission.period_end +
    ", " + submission.totals.in_ttl_losses + " losses / " +
    submission.totals.requests + " req";
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

async function commitSubmission(env, submission) {
  const token = env.GITHUB_TOKEN;
  const repo = env.GITHUB_REPO;
  if (!token || !repo) return { ok: false };
  const base = (env.GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
  const url = base + "/repos/" + repo + "/contents/" + DATA_PATH;
  const message = commitMessage(submission);

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

    doc.submissions.push(submission);
    const putBody = {
      message: message,
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
      return { ok: true, commitUrl: commitUrl };
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

  const submission = buildSubmission(body, new Date());
  const commit = await commitSubmission(env, submission);
  if (!commit.ok) {
    return jsonResponse(502, { ok: false, error: "storage" });
  }
  return jsonResponse(200, { ok: true, id: submission.id, commit_url: commit.commitUrl });
}
