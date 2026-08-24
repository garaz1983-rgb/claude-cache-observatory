/*
 * identity.js — the machine fingerprint (M13).
 *
 * Why this exists
 *   The observatory sums every row in data/submissions.json. Before M13 a
 *   person who submitted twice was two rows, so their overlapping days were
 *   counted twice and the "accounts" KPI claimed two submitters where there
 *   was one. It happened in production on 2026-08-24: 188,174 requests and
 *   343 losses shown for a machine that had 153,623 and 228.
 *
 *   There is no login here and there is not going to be one, so identity has
 *   to come from something the machine already has. The session logs carry
 *   `requestId`s: values minted by Anthropic's servers, unguessable, and
 *   already the key the engine dedupes on. A deterministic sample of them,
 *   hashed on this machine, is a stable per-machine pseudonym that survives a
 *   cleared browser store, a private window and a different browser entirely,
 *   and that needs no forced storage at all.
 *
 * What leaves the machine
 *   sha256("cco.anchor.v1|" + requestId) for at most 16 requestIds. Nothing
 *   else. Not the requestId, not the timestamp it came from, not the file it
 *   sat in, not how many there were. The hash is one-way; the observatory
 *   hashes what it receives a SECOND time before storing it, so a reader of
 *   the public dataset holds a value that matches nothing they can send.
 *
 * The sample, and why it is shaped this way
 *   Only the oldest would be wrong: a log cleanup deletes exactly those.
 *   Only the newest would be wrong: they did not exist at the last submission,
 *   so they could never match it. So the sample is HEAD_COUNT earliest plus a
 *   spread of evenly spaced picks across the whole scan, and ANY single
 *   overlap counts as the same machine. The server refreshes the stored set to
 *   the newest one on every update, so the fingerprint tracks the log folder's
 *   drift instead of going stale.
 *
 *   Known limit, stated rather than hidden: the head survives new logs being
 *   appended (it is the same first records), and the spread does not (every
 *   proportional position moves when the record count grows). If a cleanup
 *   removes more than the head, nothing overlaps and the machine reads as new.
 *   Submitting again before that happens is what keeps the chain alive.
 *
 * Ordering
 *   By the record's own instant, then by the id string. Independent of the
 *   order the browser handed the files over, so two scans of the same folder
 *   sample the same records.
 *
 * Reading the lines
 *   By regex over the raw text, not JSON.parse: this module needs an opaque
 *   stable string and an instant, never the record's meaning, and the folder
 *   scan already parses every line twice (engine + census). The prefilter is
 *   the engine's own ('"usage"'), so the sample is drawn from the requests the
 *   engine counted.
 *
 * UMD: Node -> module.exports, browser -> window.CacheObservatoryIdentity.
 * No dependencies, no network, no DOM, no storage.
 */
(function (root, factory) {
  if (typeof module === "object" && module !== null && module.exports) {
    module.exports = factory();
  } else {
    root.CacheObservatoryIdentity = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var ANCHOR_COUNT = 16;   // how many hashes are sent
  var HEAD_COUNT = 8;      // of those, how many are the earliest records
  var ANCHOR_PREFIX = "cco.anchor.v1|";  // domain separation, not a secret

  // `"id"` cannot match inside `"message_id"`: the quote before `id` is
  // required, and there the preceding character is an underscore.
  var RID_RE = /"requestId"\s*:\s*"([^"\\]{1,200})"/;
  var MSG_ID_RE = /"id"\s*:\s*"(msg_[^"\\]{1,200})"/;
  var TS_RE = /"timestamp"\s*:\s*"([^"\\]{1,64})"/;

  var HEX64 = /^[0-9a-f]{64}$/;

  function globalObj() {
    if (typeof globalThis !== "undefined") return globalThis;
    if (typeof self !== "undefined") return self;
    return null;
  }

  /* True when this environment can hash at all. crypto.subtle is absent on an
     insecure origin (a page opened from file://), where /api/submit could not
     be reached either — the caller degrades to "no fingerprint" and says so. */
  function available() {
    var g = globalObj();
    return !!(g && g.crypto && g.crypto.subtle && typeof TextEncoder !== "undefined");
  }

  function toHex(buffer) {
    var bytes = new Uint8Array(buffer);
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      out += bytes[i].toString(16).padStart(2, "0");
    }
    return out;
  }

  function sha256Hex(text) {
    var g = globalObj();
    if (g && g.crypto && g.crypto.subtle && typeof TextEncoder !== "undefined") {
      return g.crypto.subtle
        .digest("SHA-256", new TextEncoder().encode(text))
        .then(toHex);
    }
    // Node without a global WebCrypto (pre-18). Never reached in a browser.
    if (typeof require === "function") {
      try {
        var nodeCrypto = require("crypto");
        return Promise.resolve(
          nodeCrypto.createHash("sha256").update(text, "utf8").digest("hex"));
      } catch (e) { /* fall through to the rejection below */ }
    }
    return Promise.reject(new Error("sha-256 is not available here"));
  }

  // Same instant arithmetic as parse.js parseTimestamp(): the record's own
  // offset when it carries one, UTC when it does not. Only used for ordering.
  function instantOf(ts) {
    if (typeof ts !== "string" || ts === "") return null;
    var hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/.test(ts);
    var ms = Date.parse(hasOffset ? ts : ts + "Z");
    return isNaN(ms) ? null : ms;
  }

  /* Every request id in the scan, deduped, ordered by instant then id.
     files: [{name, text}] — the same array the engine is handed. */
  function collect(files) {
    var seen = Object.create(null);
    var rows = [];
    if (!Array.isArray(files)) return [];
    for (var f = 0; f < files.length; f++) {
      var file = files[f];
      var text = (file && typeof file.text === "string") ? file.text : "";
      if (!text) continue;
      var start = 0;
      var len = text.length;
      while (start <= len) {
        var nl = text.indexOf("\n", start);
        var end = nl === -1 ? len : nl;
        var line = text.slice(start, end);
        if (nl === -1) start = len + 1; else start = nl + 1;
        if (line.length === 0) continue;
        if (line.indexOf('"usage"') === -1) continue;
        var m = RID_RE.exec(line);
        if (!m) m = MSG_ID_RE.exec(line);
        if (!m) continue;
        var id = m[1];
        if (seen[id] !== undefined) continue;
        var t = TS_RE.exec(line);
        var ms = t ? instantOf(t[1]) : null;
        if (ms === null) continue;   // unorderable: leaving it out stays deterministic
        seen[id] = 1;
        rows.push({ id: id, ms: ms });
      }
    }
    rows.sort(function (a, b) {
      if (a.ms !== b.ms) return a.ms - b.ms;
      return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
    });
    return rows.map(function (r) { return r.id; });
  }

  /* HEAD_COUNT earliest + an even spread over the whole scan, deduped.
     The spread picks bucket midpoints, so neither the very first nor the very
     last record is what the spread rests on. */
  function sample(ids) {
    if (!Array.isArray(ids)) return [];
    var n = ids.length;
    if (n <= ANCHOR_COUNT) return ids.slice();
    var out = [];
    var seen = Object.create(null);
    function take(i) {
      if (i < 0) i = 0;
      if (i > n - 1) i = n - 1;
      var v = ids[i];
      if (seen[v] === undefined) { seen[v] = 1; out.push(v); }
    }
    var head = HEAD_COUNT < n ? HEAD_COUNT : n;
    for (var i = 0; i < head; i++) take(i);
    var spread = ANCHOR_COUNT - head;
    for (var j = 0; j < spread; j++) take(Math.round((j + 0.5) * n / spread));
    return out;
  }

  function anchorsOf(ids) {
    var picked = sample(ids);
    return Promise.all(picked.map(function (id) {
      return sha256Hex(ANCHOR_PREFIX + id);
    }));
  }

  /* The one call the page makes. Resolves to {count, sampled, anchors};
     anchors is [] when this environment cannot hash, which the page discloses
     rather than papering over. Never rejects: a missing fingerprint is a
     degraded submission, not a failed diagnosis. */
  function fingerprint(files) {
    var ids;
    try {
      ids = collect(files);
    } catch (e) {
      ids = [];
    }
    if (!ids.length || !available()) {
      return Promise.resolve({ count: ids.length, sampled: 0, anchors: [] });
    }
    return anchorsOf(ids).then(function (anchors) {
      return { count: ids.length, sampled: anchors.length, anchors: anchors };
    }, function () {
      return { count: ids.length, sampled: 0, anchors: [] };
    });
  }

  // Whitelist for anything read back from storage or handed in from a page.
  function sanitizeAnchors(list) {
    if (!Array.isArray(list)) return [];
    var out = [];
    for (var i = 0; i < list.length && out.length < ANCHOR_COUNT; i++) {
      if (typeof list[i] === "string" && HEX64.test(list[i])) out.push(list[i]);
    }
    return out;
  }

  return {
    ANCHOR_COUNT: ANCHOR_COUNT,
    HEAD_COUNT: HEAD_COUNT,
    ANCHOR_PREFIX: ANCHOR_PREFIX,
    available: available,
    sha256Hex: sha256Hex,
    collect: collect,
    sample: sample,
    anchorsOf: anchorsOf,
    fingerprint: fingerprint,
    sanitizeAnchors: sanitizeAnchors
  };
});
