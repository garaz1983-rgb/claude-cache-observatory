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
 *   4. bot commit via GitHub Git Data API (a moved ref earns a re-read and a
 *      jittered retry, up to COMMIT_MAX_ATTEMPTS times)
 *                                   -> 502 {ok:false, error:"storage"} if the
 *                                      store could not be read or written
 *                                   -> 409 {ok:false, error:"conflict",
 *                                      retry_after} if it could, and the branch
 *                                      was taken every time
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
 * The dataset became three kinds of file (a fourth arrives in M14.1 below):
 *
 *   data/submissions.json  the INDEX. Same path, so existing links and the
 *                          "one submission is one commit" story survive, but
 *                          the row carries no `daily` array any more — only
 *                          `daily_days` (how many rows its detail file holds)
 *                          and `detail` (where that file is).
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
 *   - the blob endpoint has no 1 MB inline cap, so THAT wall is gone. It is not
 *     true that there is no wall: GitHub truncates a tree listing at 100,000
 *     entries OR 7 MB of response, whichever comes first, and data/subs/
 *     grows one file per submitter. An entry serialises to 257 bytes in the
 *     shape GitHub returns, so the 7 MB cap binds first at roughly 28,000
 *     submitters is where the read path stops being able to find an existing
 *     detail file. Since M14.2 that limit is DETECTED (readState and
 *     readDetailDaily refuse a listing GitHub marked `truncated`) rather than
 *     walked past silently, which is the difference between "returning
 *     submitters start failing and nobody knows why" and "the write path
 *     refuses and says so". Sharding data/subs/ by an id prefix would move the
 *     ceiling to ~25 M; it is not done here — see readDetailDaily();
 *   - a submission writes SEVERAL files and they must land TOGETHER. Sequential
 *     Contents-API PUTs would be one commit each with no atomicity, and a
 *     failure between them leaves the fleet series counting a submission the
 *     index does not list, or a detail file nothing points at. One tree, one
 *     commit, one ref update.
 *
 * ---------- M14.1: the digests leave the index ----------
 *
 * M14 measured what an index row actually costs and the answer was not the one
 * the plan assumed. Measured on the row committed in data/, by giving it a
 * fingerprint and taking the marginal growth of the serialised file:
 *
 *     no identity at all .................  544 bytes
 *     token_hash only (paste path) .......  662 bytes
 *     16 anchor digests (folder path) .... 1857 bytes   <- 1313 B of it, 70.7%,
 *     16 anchor digests, merged .......... 1891 bytes      is the digest block
 *
 * Sixteen 64-character digests are 1,024 characters before any JSON overhead,
 * and the index is the ONE file every visitor downloads. So the row that every
 * reader pays for was two thirds material no reader ever looks at: the digests
 * exist only so this API can recognise a returning submitter.
 *
 * They now live in data/identity.json, keyed by submission id:
 *
 *   data/identity.json     anchor_hashes + token_hash per submission id.
 *                          Read and written by this function on every
 *                          submission. Fetched by NO page — index.html and
 *                          ko/index.html still download exactly two data files.
 *
 * 🔴 This is a SIZE AND BANDWIDTH change, not a privacy one. data/identity.json
 * is in the same public repository as everything else and anyone may open it;
 * nothing about it is hidden, restricted or less exposed than it was inside the
 * index. The only thing that changed is that a visitor no longer downloads it.
 * The replay boundary is exactly where it was and rests on the same property it
 * always did — what is stored is a SECOND hash, so a value copied out of the
 * new file matches nothing, just as it matched nothing in the old one.
 *
 * An index row with no identity entry is a legitimate state, not a broken one:
 * the row committed before M13 has no identity to move, and a row can only ever
 * be merged into by the machine that owns it, never by the absence of a key.
 *
 * The retry is unchanged in spirit: a ref that moved under us (422 on the ref
 * update, the equivalent of the old 409) earns a re-read, and the match, the
 * merge, the identity resolution and the fleet delta all re-run against the
 * FRESH content. Nothing is merged against a stale read.
 *
 * 🔴 Whatever a file claims must add up inside itself, and they must agree with
 * each other: the index row's totals equal the sum of its own detail file,
 * data/daily.json equals the sum across all detail files, and every entry in
 * data/identity.json belongs to a row the index lists.
 * tests/dataset_validate.py is the single definition of that, and the contract
 * test runs it after every accepted submission.
 *
 * ---------- M14.2: several people submitting at the same moment ----------
 *
 * M14 gave the branch a compare-and-swap and exactly ONE retry, which means
 * exactly one loser of any race gets a second chance. Measured through the mock
 * with barrier-released threads, that produced the same answer at every burst
 * size, because each round has one CAS winner and there were only ever two
 * rounds:
 *
 *     simultaneous   accepted   refused
 *          3            2          1 x 502
 *          5            2          3 x 502
 *         10            2          8 x 502     <- 80% of the submissions lost
 *
 * and the refusal said "storage", which is what a GitHub outage says. Three
 * things were wrong and all three are fixed here.
 *
 *   1. THE WINDOW WAS LONGER THAN IT NEEDED TO BE. The read was 9 strictly
 *      sequential round trips and the write 7, with nothing overlapping: 13
 *      calls for a new row and 16 for a merge, ~1.6 s against real GitHub at
 *      ~100 ms a call, essentially all of it inside the conflict window. The
 *      three blob READS are independent of each other and so are the four blob
 *      POSTs, so both now go out as one batch each — 7 calls become 2 waits.
 *      Nothing about atomicity changes: a blob is an unreferenced object until
 *      a commit points at it, so posting four at once cannot publish anything
 *      partial, and the tree, the commit and the ref update stay strictly
 *      ordered because each one needs the sha the previous returned.
 *
 *   2. ONE RETRY IS NOT A BUDGET. It is now COMMIT_MAX_ATTEMPTS with a jittered
 *      wait between attempts, so retriers do not come back in lockstep and
 *      collide again. The wait is scaled by how long the failed attempt ITSELF
 *      took, because that duration is the width of the window they are
 *      colliding inside — a fixed millisecond figure would be far too long
 *      against a fast store and far too short against a slow one. See
 *      backoffDelayMs() for why the bound is where it is.
 *
 *   3. A LOST RACE IS NOT AN OUTAGE. Running out of attempts now answers
 *      409 {ok:false, error:"conflict", retry_after} instead of borrowing the
 *      502 that means "the store did not answer". The distinction is not
 *      cosmetic: one of them is worth retrying immediately and the other is
 *      not, and the check page now retries a 409 once on its own before it
 *      tells a person anything.
 *
 * 🔴 What did NOT change: the ref CAS with force:false. Atomicity rests
 * entirely on it. Every attempt still re-reads all four files and re-resolves
 * everything against that fresh read, and a submission still lands as one
 * commit or not at all.
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
  "anchors", "token", "detector"
];
const TOP_REQUIRED = [
  "plan", "client", "concurrent_sessions",
  "period_start", "period_end", "totals", "daily", "script_version"
];
const TOTALS_FIELDS = ["requests", "confirmed_losses", "probable_losses",
                       "iron_losses", "wasted_tokens", "pmnf_losses"];
const DAILY_FIELDS = ["date", "requests", "losses", "pmnf", "wasted_tokens"];

/* M15 detector vocabulary. VOCABULARY ONLY — no counts.
 *
 * The engines build a full census (how many requests carried each reason, each
 * client version, how many wrote cache without reading any). None of that
 * crosses the wire. Two reasons, and the second is the load-bearing one:
 *
 *   1. Nothing here can contradict `totals`. A submission covers at most 92
 *      days, while the census covers every file the browser scanned, so any
 *      count sent alongside would be a number a reader could not reconcile
 *      with the row it sits in. A list of names has nothing to reconcile.
 *   2. The fleet does not need counts to answer the question this milestone
 *      exists for: has anyone, anywhere, started seeing a reason value this
 *      project does not know? That is a question about vocabulary.
 *
 * Same charset the engines enforce, plus the three bracketed literals they
 * use for missing/unprintable/overflow. The literals carry parentheses, which
 * the tag charset excludes, so a value out of a log file cannot impersonate
 * one. These strings ARE published, so this is the last gate before a string
 * from a stranger's disk lands in a file everyone reads.
 */
const DETECTOR_FIELDS = ["reasons", "versions"];
const CENSUS_KEY_RE = /^(?:[A-Za-z0-9._-]{1,64}|\((?:invalid|none|other)\))$/;
const MAX_CENSUS_KEYS = 16;

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
const IDENTITY_PATH = "data/identity.json";
const SUBS_PREFIX = "data/subs/";
const INDEX_SCHEMA_VERSION = 2;
const FLEET_SCHEMA_VERSION = 1;
const DETAIL_SCHEMA_VERSION = 1;
/* M14.1. Version 1 because the file is new: there has never been an
   data/identity.json with a different shape, and the values inside it are
   byte-for-byte the ones index rows carried under `identity` in M13/M14. */
const IDENTITY_SCHEMA_VERSION = 1;
const SUB_ID_RE = /^sub-[0-9]{14}-[0-9a-f]{4}$/;
const DEFAULT_BRANCH = "master";

/* M14.2. How many times one submission may re-read and try again after losing
   the branch to somebody else's commit.
   Six, and the reasoning is arithmetic rather than taste. One CAS winner exists
   per round, so N simultaneous submissions need N rounds unless the losers
   spread themselves out; the jitter below widens by one attempt-duration per
   round, so round k can fit about k winners, and 1+1+2+3+4+5 = 16 covers a
   burst of ten with room over. The cost of the bound is what a submitter waits
   in the worst case: six attempts plus their waits, which the deadline caps.
   Raising it further buys less each time (the sum grows quadratically while the
   waiting grows linearly) and a browser at the other end is not patient. */
const COMMIT_MAX_ATTEMPTS = 6;
/* Nothing above this, however slow the store is: a wait longer than this is
   worse for the person than being told to press the button again. */
const BACKOFF_CAP_MS = 6000;
/* And nothing below this, however fast it is: at zero the retriers would come
   back in the same lockstep that made them collide. */
const BACKOFF_FLOOR_MS = 25;
/* Stop retrying once the whole commit stage has cost this much, even with
   attempts left. Cloudflare bills CPU rather than wall clock and awaiting a
   fetch costs none, so this bound is about the person waiting, not the platform. */
const CONFLICT_DEADLINE_MS = 25000;
/* Seconds a conflict-exhausted caller is told to wait. Randomised for the same
   reason the backoff is: several callers exhausted by the same burst must not
   all come back together. */
const CONFLICT_RETRY_AFTER_MIN = 2;
const CONFLICT_RETRY_AFTER_MAX = 6;
const BLOB_MODE = "100644";
const DETAIL_ROW_KEYS = ["date", "requests", "losses", "pmnf", "wasted_tokens"];
const FLEET_ROW_KEYS = ["date", "requests", "losses", "pmnf", "wasted_tokens", "machines"];

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
      for (const key of ["requests", "losses", "pmnf", "wasted_tokens"]) {
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

  /* M15 detector. Optional: a submission from an older client carries none,
     and that is not an error — it means "this machine did not report which
     reason names it saw", which the fleet page states rather than guesses. */
  if ("detector" in body) {
    const det = body.detector;
    if (!isPlainObject(det)) {
      errors.push("detector: must be an object");
    } else {
      for (const key of Object.keys(det)) {
        if (DETECTOR_FIELDS.indexOf(key) === -1) {
          errors.push("undefined field: detector." + key);
        }
      }
      for (const key of DETECTOR_FIELDS) {
        validateVocabulary(det[key], "detector." + key, errors);
      }
    }
  }
  return errors;
}

/* A published list of plain tags. The offending value is deliberately NOT
   echoed into the error: the page validates the same rule before sending, so
   a failure here is a bug or an attempt, and neither is worth reflecting a
   stranger's string back out of this endpoint. */
function validateVocabulary(v, label, errors) {
  if (!Array.isArray(v)) {
    errors.push(label + ": must be an array");
    return;
  }
  if (v.length > MAX_CENSUS_KEYS) {
    errors.push(label + ": more than " + MAX_CENSUS_KEYS + " entries");
    return;
  }
  const seen = new Set();
  for (let i = 0; i < v.length; i++) {
    if (typeof v[i] !== "string" || !CENSUS_KEY_RE.test(v[i])) {
      errors.push(label + "[" + i + "]: not a plain tag");
      continue;
    }
    if (seen.has(v[i])) errors.push(label + "[" + i + "]: duplicate entry");
    seen.add(v[i]);
  }
}

/* ---------- step 2: sanity validation ---------- */

function validateSanity(body) {
  const errors = [];
  const t = body.totals;
  const losses = t.confirmed_losses + t.probable_losses;
  if (losses > t.requests) {
    errors.push("totals.confirmed_losses + probable_losses exceeds totals.requests");
  }
  if (t.iron_losses > losses) {
    errors.push("totals.iron_losses exceeds confirmed_losses + probable_losses");
  }
  // The two series overlap but each is bounded by the same requests.
  if (t.pmnf_losses > t.requests) {
    errors.push("totals.pmnf_losses exceeds totals.requests");
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
  let sumPmnf = 0;
  let sumWasted = 0;
  body.daily.forEach(function (entry, i) {
    const day = parseDay(entry.date);
    if (entry.losses > entry.requests) {
      errors.push("daily[" + i + "].losses exceeds daily[" + i + "].requests");
    }
    if (entry.pmnf > entry.requests) {
      errors.push("daily[" + i + "].pmnf exceeds daily[" + i + "].requests");
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
    sumPmnf += entry.pmnf;
    sumWasted += entry.wasted_tokens;
  });
  // daily must sum to totals — blocks inflating totals alone (codex review).
  if (sumRequests !== t.requests) {
    errors.push("daily requests sum " + sumRequests +
      " != totals.requests " + t.requests);
  }
  if (sumLosses !== losses) {
    errors.push("daily losses sum " + sumLosses +
      " != totals confirmed+probable " + losses);
  }
  if (sumPmnf !== t.pmnf_losses) {
    errors.push("daily pmnf sum " + sumPmnf +
      " != totals.pmnf_losses " + t.pmnf_losses);
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

/* ---------- M14.2: backing off after losing the branch ---------- */

/* crypto rather than Math.random, because this jitter is the only thing keeping
   simultaneous retriers from re-colliding, and Math.random carries no promise
   about being independent across concurrent invocations of a Worker. */
function randomFraction() {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return buf[0] / 4294967296;
}

function sleep(ms) {
  if (!(ms > 0)) return Promise.resolve();
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

/* How long to wait before attempt number `attempt + 1`.
 *
 * FULL jitter — uniform over [0, window) rather than window/2 ± something —
 * because the point is to spread callers out, and a narrow band around a fixed
 * delay just moves the pile-up rather than removing it.
 *
 * The window is scaled by how long the attempt that just failed actually took,
 * not by a constant. That duration IS the width of the window two submissions
 * can collide inside: a caller wins only if nobody else's ref update lands
 * between its own read and its own PATCH. So a window of `elapsed` makes room
 * for about one more winner, `2 x elapsed` for about two, and the schedule
 * widens by one attempt-duration per round. It also means this code needs no
 * idea whether it is talking to a store that answers in 1 ms or in 200 ms —
 * against the mock the waits are milliseconds and against GitHub they are
 * seconds, from the same arithmetic.
 */
function backoffDelayMs(attempt, elapsedMs) {
  const unit = Math.max(BACKOFF_FLOOR_MS, elapsedMs);
  const spread = Math.min(BACKOFF_CAP_MS, unit * attempt);
  return Math.floor(randomFraction() * spread);
}

function conflictRetryAfter() {
  const span = CONFLICT_RETRY_AFTER_MAX - CONFLICT_RETRY_AFTER_MIN + 1;
  return CONFLICT_RETRY_AFTER_MIN + Math.floor(randomFraction() * span);
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
      confirmed_losses: body.totals.confirmed_losses,
      probable_losses: body.totals.probable_losses,
      iron_losses: body.totals.iron_losses,
      wasted_tokens: body.totals.wasted_tokens,
      pmnf_losses: body.totals.pmnf_losses
    },
    daily: body.daily.map(function (d) {
      return {
        date: d.date,
        requests: d.requests,
        losses: d.losses,
        pmnf: d.pmnf,
        wasted_tokens: d.wasted_tokens
      };
    }),
    script_version: body.script_version,
    // Sorted here, not trusted from the client: the published order is this
    // server's, so a diff of the public file stays readable whatever order a
    // submitter happened to send.
    detector: isPlainObject(body.detector) ? {
      reasons: body.detector.reasons.slice().sort(),
      versions: body.detector.versions.slice().sort()
    } : null
  };
}

/* One stored INDEX row, always written in the same field order so a diff of the
   public file reads as a diff of the numbers.

   M14: `daily` is gone from here and `daily_days` + `detail` take its place.
   The day count is not decoration — it is what lets a reader check that the
   detail file at that path is complete without the index having to carry it.

   M14.1: `identity` is gone from here too, to data/identity.json. Every field
   left in this record is one the page renders or a reader reads; the row no
   longer carries 1,313 bytes of digests that only this file ever looks at.
   The row's id is what joins it to its identity entry, exactly as it is what
   joins it to its detail file. */
function composeRecord(id, submittedAt, updatedAt, fields) {
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
  // Last, and omitted entirely when the client sent none: absent and empty are
  // different claims. Absent = this machine never reported which names it saw.
  // Empty = it looked and found none, which is the case worth noticing.
  if (fields.detector) rec.detector = fields.detector;
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

/* data/identity.json: one LINE per submission id, for the same reason the two
   row files are written that way. `git log -p data/identity.json` then reads as
   "this submitter's fingerprint changed" instead of a twenty-line block moving
   under a diff, which matters because a fingerprint refreshes on every merge.

   Ids are sorted rather than left in index order, so the file has one canonical
   form: an id is a timestamp followed by four hex characters, so sorting them
   is also chronological order. Without this a submission that merely reordered
   the index would rewrite this whole file as a spurious diff. */
function serializeIdentity(doc) {
  const ids = Object.keys(doc.identities).sort();
  const out = ["{", "  \"schema_version\": " +
    JSON.stringify(doc.schema_version) + ","];
  if (!ids.length) {
    out.push("  \"identities\": {}");
  } else {
    out.push("  \"identities\": {");
    for (let i = 0; i < ids.length; i++) {
      out.push("    " + JSON.stringify(ids[i]) + ": " +
        JSON.stringify(doc.identities[ids[i]]) +
        (i === ids.length - 1 ? "" : ","));
    }
    out.push("  }");
  }
  out.push("}");
  return out.join("\n") + "\n";
}

function commitMessage(submission, merged) {
  return "data: " + (merged ? "update" : "submission") + " " + submission.id +
    " — " + submission.nickname +
    ", " + submission.period_start + "~" + submission.period_end +
    ", " + (submission.totals.confirmed_losses +
            submission.totals.probable_losses) + " losses / " +
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

/* M14.1: one submission's identity entry, looked up by id in the map read out
   of data/identity.json. `null` means the row has no identity, which is a
   legitimate state — the row committed before M13 has none — and is treated
   the same way an empty identity block was: it matches nothing.

   hasOwnProperty rather than a plain `identities[id]`: the ids come out of a
   PUBLIC file a repo admin can hand-edit, and a row whose id was "__proto__"
   or "constructor" would otherwise resolve to something off Object.prototype
   instead of to an entry. isPlainObject() would reject the result either way,
   but the lookup should not reach the prototype chain in the first place. */
function identityEntry(identities, id) {
  if (!isPlainObject(identities) || typeof id !== "string" || !id) return null;
  if (!Object.prototype.hasOwnProperty.call(identities, id)) return null;
  const entry = identities[id];
  return isPlainObject(entry) ? entry : null;
}

function storedAnchors(entry) {
  if (!entry || !Array.isArray(entry.anchor_hashes)) return [];
  return entry.anchor_hashes.filter(function (h) {
    return typeof h === "string" && HEX64_RE.test(h);
  });
}

function storedTokenHash(entry) {
  if (!entry || typeof entry.token_hash !== "string" ||
      !HEX64_RE.test(entry.token_hash)) return "";
  return entry.token_hash;
}

/* Which row this submission belongs to, or -1.
   Fingerprint first, browser token second: the fingerprint is anchored to the
   machine whose logs are being reported, the token only to a browser. ANY
   single anchor overlap is the same machine — the sample drifts as logs are
   written and rotated, so requiring more than one would lose the link on a
   normal week of use.

   M14.1: the walk is still over the INDEX, in index order, and the identity
   map is only ever consulted by the id of the row being examined. That is what
   keeps the answer an index position — a match found by scanning the identity
   file directly could name an id the index does not list, and there would be no
   row to merge into. */
function matchIndex(subs, identities, anchorHashes, tokenHash) {
  if (!Array.isArray(subs)) return -1;
  if (anchorHashes.length) {
    const want = new Set(anchorHashes);
    for (let i = 0; i < subs.length; i++) {
      const have = storedAnchors(identityEntry(identities, subs[i] && subs[i].id));
      for (let j = 0; j < have.length; j++) {
        if (want.has(have[j])) return i;
      }
    }
  }
  if (tokenHash) {
    for (let i = 0; i < subs.length; i++) {
      if (storedTokenHash(identityEntry(identities, subs[i] && subs[i].id)) === tokenHash) return i;
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
  if (!isCount(d.requests) || !isCount(d.losses) || !isCount(d.pmnf) ||
      !isCount(d.wasted_tokens)) return null;
  return { date: d.date, requests: d.requests, losses: d.losses,
           pmnf: d.pmnf, wasted_tokens: d.wasted_tokens };
}

/* Union the daily rows by date and recompute the totals from the result.
 *
 * The incoming row wins for a date both cover: it is the fresher measurement
 * of the same day. Two totals cannot be recomputed from the merged rows —
 * there is no per-day iron column and no per-day tier column — so both are
 * carried conservatively, by the same formula:
 *
 *     kept = max(0, existing.count - losses on the superseded days)
 *
 * which is EXACT in both flows that matter (a disjoint increment supersedes
 * nothing, so all of it is kept; a full re-scan supersedes every existing day,
 * so none of it is and the fresh count stands alone) and never over-claims in
 * between. `probable` is then the remainder against the merged loss sum, so
 * confirmed + probable == sum(daily.losses) holds by construction rather than
 * by luck. Under-counting confirmed (the stronger claim) and iron (the worst
 * subset) is the direction that cannot flatter the site's own headline.
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
                           losses: row.losses, pmnf: row.pmnf,
                           wasted_tokens: row.wasted_tokens });
  }
  const daily = Array.from(byDate.values()).sort(function (a, b) {
    return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0);
  });

  const totals = { requests: 0, confirmed_losses: 0, probable_losses: 0,
                   iron_losses: 0, wasted_tokens: 0, pmnf_losses: 0 };
  let mergedLosses = 0;
  for (let i = 0; i < daily.length; i++) {
    totals.requests += daily[i].requests;
    mergedLosses += daily[i].losses;
    totals.pmnf_losses += daily[i].pmnf;
    totals.wasted_tokens += daily[i].wasted_tokens;
  }
  const exTotals = isPlainObject(existing.totals) ? existing.totals : {};
  let exIron = isCount(exTotals.iron_losses) ? exTotals.iron_losses : 0;
  if (exIron > existingLosses) exIron = existingLosses;
  const keptIron = Math.max(0, exIron - supersededLosses);
  totals.iron_losses = Math.min(keptIron + incoming.totals.iron_losses,
                                mergedLosses);
  let exConf = isCount(exTotals.confirmed_losses) ? exTotals.confirmed_losses : 0;
  if (exConf > existingLosses) exConf = existingLosses;
  const keptConf = Math.max(0, exConf - supersededLosses);
  totals.confirmed_losses = Math.min(keptConf + incoming.totals.confirmed_losses,
                                     mergedLosses);
  totals.probable_losses = mergedLosses - totals.confirmed_losses;

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
    script_version: incoming.script_version,
    // Incoming wins, like nickname and script_version: the row states what
    // this machine's LATEST scan saw. A union would never be able to forget a
    // value the server had stopped sending, which is the change this whole
    // milestone is built to make visible in the first place.
    detector: incoming.detector
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
    pmnf: isCount(d.pmnf) ? d.pmnf : 0,
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
  /* 🔴 M14.2: the SAME filter mergeRecord() puts these rows through. They used
     to arrive raw here, so the two functions disagreed about which rows exist —
     one dropped a hand-edited row and the other tried to read a `.date` off it,
     which skipped the subtraction on seven malformed shapes and threw outright
     on a literal null. readDetailDaily() now refuses such a file before it ever
     reaches this point (a lost date cannot be subtracted, so refusing is the
     only honest answer), which makes this filter unreachable from the write
     path. It stays because this function's contract is the same as
     fleetRowOf()'s directly below the loop above: hand-edited input must not
     poison the series, and a guard that depends on a caller's diligence is not
     a guard. */
  const outgoing = Array.isArray(oldDaily) ? oldDaily : [];
  for (let i = 0; i < outgoing.length; i++) {
    const r = dailyRowOf(outgoing[i]);
    if (!r) continue;              // a hand-edited file must not poison the series
    const cur = byDate.get(r.date);
    if (!cur) continue;            // never covered by the series; nothing to take out
    cur.requests -= r.requests;
    cur.losses -= r.losses;
    cur.pmnf -= r.pmnf;
    cur.wasted_tokens -= r.wasted_tokens;
    cur.machines -= 1;
  }
  for (let i = 0; i < newDaily.length; i++) {
    const r = newDaily[i];
    let cur = byDate.get(r.date);
    if (!cur) {
      cur = { date: r.date, requests: 0, losses: 0, pmnf: 0,
              wasted_tokens: 0, machines: 0 };
      byDate.set(r.date, cur);
    }
    cur.requests += r.requests;
    cur.losses += r.losses;
    cur.pmnf += r.pmnf;
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
      pmnf: Math.max(0, d.pmnf),
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

/* 🔴 M14.2. GitHub truncates a tree listing at 100,000 entries OR 7 MB of
   response, whichever comes first, and says so with
   this flag, and the reply still LOOKS complete: it is a well-formed list of
   entries, just not all of them. A caller that ignores the flag therefore never
   sees an error — it sees treeEntry() return null and concludes the file is not
   there. In readState that would mean "this repository has no dataset" and in
   readDetailDaily "this submitter has no history", and both of those are wrong
   answers that write. So a truncated listing is treated as a failed read.

   The ceiling this puts on the design is real and worth stating plainly, and
   it is NOT the entry count. data/subs/ holds one file per submitter, and an
   entry serialises to 257 bytes in the shape GitHub returns (path, mode,
   type, sha, size, blob url), so 7 MB binds first at roughly 28,000 - the
   100,000 figure would be 24.5 MB of response. This is arithmetic on
   GitHub's two documented caps, not a measurement against the live API. Below
   it nothing changes; at it every returning submitter is refused with a 502
   until someone shards the directory. That is a far better failure than the one
   this replaces, but it is not "no limit", and the M14 note that the wall was
   "gone" was only ever true of the 1 MB inline-content cap. */
function treeTruncated(body) {
  return !!(body && body.truncated === true);
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
   ref -> commit -> root tree -> data/ tree -> the blobs this write needs.
   Returns null on any failure. A file that is simply absent is not a failure:
   an empty index, an empty fleet series and NO data/identity.json at all are
   what a repository looks like before its first submission. That last one is
   not hypothetical — it is the state of this repository at the M14.1 commit,
   so the first submission after it takes exactly that path. */
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
  if (treeTruncated(root.body)) return null;

  let indexSha = "", fleetSha = "", identitySha = "", subsSha = "";
  const dataDir = treeEntry(root.body, "data", "tree");
  if (dataDir) {
    const dataTree = await ghJson(api + "/git/trees/" + dataDir.sha, token);
    if (dataTree.status !== 200) return null;
    if (treeTruncated(dataTree.body)) return null;
    const i = treeEntry(dataTree.body, "submissions.json", "blob");
    const f = treeEntry(dataTree.body, "daily.json", "blob");
    const n = treeEntry(dataTree.body, "identity.json", "blob");
    const s = treeEntry(dataTree.body, "subs", "tree");
    if (i) indexSha = i.sha;
    if (f) fleetSha = f.sha;
    if (n) identitySha = n.sha;
    if (s) subsSha = s.sha;
  }

  /* 🔴 M14.2: the three blobs are read TOGETHER. Their shas are all known by
     now and none of them is derived from another, so reading them one after the
     other was three sequential round trips of pure waiting — and every one of
     them was time this submission spent inside the window somebody else can
     move the branch in. Nothing else about the read changes: the guards below
     are the same guards, applied to the same three answers. */
  const [indexBlob, fleetBlob, identityBlob] = await Promise.all([
    indexSha ? readJsonBlob(api, token, indexSha) : null,
    fleetSha ? readJsonBlob(api, token, fleetSha) : null,
    identitySha ? readJsonBlob(api, token, identitySha) : null
  ]);

  let index = { schema_version: INDEX_SCHEMA_VERSION, submissions: [] };
  if (indexSha) {
    index = indexBlob;
    if (!isPlainObject(index) || !Array.isArray(index.submissions)) return null;
  }
  let fleet = { schema_version: FLEET_SCHEMA_VERSION, days: [] };
  if (fleetSha) {
    fleet = fleetBlob;
    if (!isPlainObject(fleet) || !Array.isArray(fleet.days)) return null;
  }
  /* Absent => nobody has an identity yet, which is a valid dataset and the one
     this repository is in at the M14.1 commit. PRESENT BUT UNREADABLE is a hard
     failure, exactly as it is for the other two: continuing there would resolve
     every submission as a stranger, append a second row for a machine that
     already has one, and then overwrite the file that would have said so. */
  let identities = {};
  if (identitySha) {
    const doc = identityBlob;
    if (!isPlainObject(doc) || !isPlainObject(doc.identities)) return null;
    identities = doc.identities;
  }
  return {
    headSha: headSha,
    rootTreeSha: rootTreeSha,
    subsSha: subsSha,
    index: index,
    fleet: fleet,
    identities: identities
  };
}

/* The daily rows of the row being merged into. Returns null on ANY doubt —
   missing directory, missing file, unreadable file, an id that is not the id
   the file claims, a listing GitHub truncated, or a row that is not a daily
   row. The caller turns null into a 502 instead of merging, because a merge
   against daily rows that failed to load would recompute the row's totals from
   the incoming submission alone and silently delete that machine's history from
   a public file.

   🔴 M14.2 added the last two of those, and the row check is the one that
   matters most. Before it, a single hand-edited row was enough to make this API
   PUBLISH an inconsistent dataset: mergeRecord() drops a malformed row (it runs
   every row through dailyRowOf) but applyFleetDelta received the same array raw
   and could not read a `.date` off it, so the date left the detail file and
   stayed in data/daily.json — measured across eight malformed shapes, seven of
   which returned HTTP 200 over a dataset that no longer added up, while the
   eighth (a literal null) threw and escaped as a 500. Making the two agree
   after the fact is not enough either: once a row's date is unreadable, WHICH
   day to take back out of the fleet series is simply not recoverable from the
   file. So the honest answer is the same one a missing detail file already got
   — refuse the write, publish nothing, and leave the hand-edit for a human,
   with tests/dataset_validate.py to find it.

   The `truncated` guard is where the ~28,000-submitter ceiling lands, and
   sharding this directory (data/subs/<first 2 hex of the id's suffix>/<id>.json,
   256 buckets, ~25 M) is the obvious way to remove it. Deliberately NOT done
   here: `detail` is a published path in every existing index row, so sharding
   is a migration of the public dataset plus a permanent second shape for old
   rows, and it adds one more sequential tree GET to the read path — the exact
   cost M14.2 spent its effort removing. At one submitter, buying a 25 M ceiling
   with a slower and more contended write path is the wrong trade; the point of
   detecting truncation is that whoever eventually approaches it will be told,
   rather than watching returning submitters fail for no visible reason. */
async function readDetailDaily(api, token, subsSha, id) {
  if (!subsSha) return null;
  const listing = await ghJson(api + "/git/trees/" + subsSha, token);
  if (listing.status !== 200) return null;
  if (treeTruncated(listing.body)) return null;
  const entry = treeEntry(listing.body, id + ".json", "blob");
  if (!entry) return null;
  const detail = await readJsonBlob(api, token, entry.sha);
  if (!isPlainObject(detail) || detail.id !== id || !Array.isArray(detail.daily)) {
    return null;
  }
  if (!detail.daily.length) return null;   // a row with no history is not one
  const rows = [];
  for (let i = 0; i < detail.daily.length; i++) {
    const row = dailyRowOf(detail.daily[i]);
    if (!row) return null;
    rows.push(row);
  }
  return rows;
}

/* blobs -> tree -> commit -> ref, in that order, which is what makes every file
   this submission touches ONE commit — four of them since M14.1 put the
   identity map in its own file. `base_tree` is the tree the read came from, so every path
   this commit does not mention is carried over unchanged and GitHub resolves
   the nested directories for us.
   Returns {url} on success, {moved:true} when the ref moved under us (the
   caller's retry), or null for any other failure. */
async function writeCommit(api, token, branch, state, files, message) {
  /* 🔴 M14.2: the four blobs are POSTed TOGETHER. A blob is a content-addressed
     object that no tree, commit or branch points at until the three ordered
     calls below say so, so writing four at once cannot publish anything partial
     — it is the same four objects arriving in a different order. What follows
     stays strictly sequential because it has to: each call needs the sha the
     previous one returned, and the ref update is the compare-and-swap the whole
     design rests on. */
  const posted = await Promise.all(files.map(function (file) {
    return ghJson(api + "/git/blobs", token, {
      method: "POST",
      body: JSON.stringify({
        content: b64EncodeUtf8(file.text),
        encoding: "base64"
      })
    });
  }));
  const entries = [];
  for (let i = 0; i < files.length; i++) {
    const blob = posted[i];
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
  if (isRefConflict(upd)) return { moved: true };
  return null;
}

/* Did the ref update fail because somebody else got there first?
 *
 * The entire retry hangs off this one answer, and the mapping it rests on —
 * GitHub returns 422 with "Update is not a fast forward" — has never been
 * exercised against the live API. The contract test pins it, but what the test
 * pins is the MOCK's definition of it, so pinning it narrowly would only prove
 * the mock agrees with itself.
 *
 * So the recognition is deliberately wider than the one status the mock
 * returns: 409 is the other status a git host plausibly uses for this refusal
 * (it is the one the Contents API used before M14), and the message is checked
 * on its own so a host that changes the status while keeping the sentence still
 * earns a retry. The two directions of being wrong are not symmetric. Treating
 * some other failure as a conflict costs a wasted re-read and ends in the same
 * refusal. Treating a real conflict as a hard failure loses the submission and
 * tells the person a storage outage happened — which is precisely the defect
 * this milestone exists to fix. */
function isRefConflict(res) {
  if (res.status === 422 || res.status === 409) return true;
  const message = res.body && typeof res.body.message === "string"
    ? res.body.message.toLowerCase() : "";
  return message.indexOf("fast forward") !== -1 ||
    message.indexOf("fast-forward") !== -1;
}

/* The read, the match, the merge, the identity map, the fleet delta and the
   write are all inside the retry loop on purpose: a moved ref means somebody
   else's commit landed in between, and after the re-read the answer to "does
   this machine already have a row" — and what that row contains, and which
   identity entries exist — may all have changed. Nothing computed before the
   conflict is carried across it; readState() re-reads all four files and every
   line below re-resolves against that fresh read.

   Returns {ok:true, …} · {ok:false, conflict:true} when the branch was taken
   every time (the store answered; this submission simply never got its turn) ·
   {ok:false} for everything else. The caller renders those as three different
   things, because they are three different things. */
async function commitSubmission(env, incoming, ident, issued, now) {
  const token = env.GITHUB_TOKEN;
  const repo = env.GITHUB_REPO;
  if (!token || !repo) return { ok: false };
  const base = (env.GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
  const api = base + "/repos/" + repo;
  const branch = env.GITHUB_BRANCH || DEFAULT_BRANCH;
  const today = now.toISOString().slice(0, 10); // truncated to the day
  const startedAt = Date.now();
  let conflicts = 0;

  for (let attempt = 1; attempt <= COMMIT_MAX_ATTEMPTS; attempt++) {
    const attemptStarted = Date.now();
    const state = await readState(api, token, branch);
    if (!state) return { ok: false };
    const doc = state.index;

    const idx = matchIndex(doc.submissions, state.identities,
      ident.anchorHashes, ident.tokenHash);
    const merged = idx !== -1;
    const previous = merged ? doc.submissions[idx] : null;
    const prevIdentity = previous
      ? identityEntry(state.identities, previous.id) : null;
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

    const record = composeRecord(id, submittedAt, updatedAt, fields);
    if (merged) {
      doc.submissions[idx] = record;   // in place: the row keeps its slot and its id
    } else {
      doc.submissions.push(record);
    }
    doc.schema_version = INDEX_SCHEMA_VERSION;

    /* M14.1: the identity map, rebuilt from the index that was just written.
       Driving it off the index rather than off the file read from disk is what
       keeps the two in step by construction — an entry is written only for a
       row the index actually lists, so the "entry with no row" the validator
       rejects cannot be produced here at all.

       A null-prototype map because the keys are ids out of a PUBLIC file: on a
       plain {} an id of "__proto__" would hit a setter instead of adding a key.

       An id whose identity resolves to null gets NO entry rather than an empty
       one. That is the same "explicitly none" state the pre-M13 row is in, and
       writing `{}` instead would make a row that has no fingerprint look like a
       row whose fingerprint was lost. */
    const identities = Object.create(null);
    for (let i = 0; i < doc.submissions.length; i++) {
      const row = doc.submissions[i];
      const rowId = row && typeof row.id === "string" ? row.id : "";
      if (!rowId || rowId === id) continue;
      const carried = identityEntry(state.identities, rowId);
      if (carried) identities[rowId] = carried;
    }
    if (identity) identities[id] = identity;

    const files = [
      { path: INDEX_PATH, text: serializeIndex(doc) },
      { path: IDENTITY_PATH,
        text: serializeIdentity({ schema_version: IDENTITY_SCHEMA_VERSION,
                                  identities: identities }) },
      { path: FLEET_PATH,
        text: serializeFleet(applyFleetDelta(state.fleet, previousDaily, fields.daily)) },
      { path: detailPath(id), text: serializeDetail(buildDetail(id, fields)) }
    ];

    const written = await writeCommit(api, token, branch, state, files,
      commitMessage(record, merged));
    if (written === null) return { ok: false };
    if (written.moved) {
      // Somebody else's commit landed between our read and our PATCH. Nothing
      // of ours was published — the blobs and the commit exist as unreferenced
      // objects that no branch points at — so waiting and starting over is
      // safe, and it is the only thing that is: everything computed above was
      // resolved against a state that is now stale.
      conflicts++;
      const elapsed = Date.now() - attemptStarted;
      if (attempt >= COMMIT_MAX_ATTEMPTS ||
          Date.now() - startedAt + elapsed > CONFLICT_DEADLINE_MS) break;
      await sleep(backoffDelayMs(attempt, elapsed));
      continue;      // re-read, re-resolve, re-merge, write once more
    }
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
  return { ok: false, conflict: conflicts > 0 };
}

/* ---------- entry point ---------- */

/* 🔴 M14.2. Nothing below may reach a caller as an unhandled throw. It used to
   be able to: neither this function nor commitSubmission() had a try/catch, so
   one malformed row in a hand-edited detail file (a literal `null`, which
   `.date` cannot be read off) escaped as HTTP 500 with a stack instead of the
   502 the contract documents. The specific throw is fixed at its source, but a
   handler whose failure mode depends on nobody ever writing another one is not
   a contract. So the whole request is wrapped, and an unexpected throw fails
   CLOSED — into the documented storage refusal, having published nothing,
   because every write in here is gated behind a ref update that either happened
   or did not. */
export async function onRequestPost(context) {
  try {
    return await handleSubmit(context);
  } catch (e) {
    return jsonResponse(502, { ok: false, error: "storage" });
  }
}

async function handleSubmit(context) {
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
    /* Two different failures, and they were one status until M14.2. A caller
       told "storage" has no reason to try again soon and no way to tell this
       apart from GitHub being down; a caller told "conflict" knows the store
       answered every time, that nothing was written, and roughly how long to
       wait. The check page acts on exactly that difference. */
    if (commit.conflict) {
      return jsonResponse(409, { ok: false, error: "conflict",
                                 retry_after: conflictRetryAfter() });
    }
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
