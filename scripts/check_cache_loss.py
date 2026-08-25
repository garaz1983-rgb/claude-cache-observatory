#!/usr/bin/env python3
"""Claude Code prompt-cache loss self-check (v2.1 - TTL-aware).

Your Claude Code session logs (~/.claude/projects/**/*.jsonl) contain a
server-generated diagnostic: message.diagnostics.cache_miss_reason.
type == "previous_message_not_found" (PMNF) means the server could not find
the cache entry for your previous turn and re-billed your entire context as
cache_creation tokens.

IMPORTANT: PMNF alone is NOT proof of a bug. The server also stamps it when
an entry legitimately expired (idle longer than the TTL). This script
therefore classifies every PMNF event by the idle gap to the previous
request in the same session:

  - in-TTL  : gap < 30 min for main sessions (1h TTL, with margin for
              response-duration skew), gap < 5 min for subagent sessions
              (5m TTL). These should be impossible per Anthropic's docs.
  - iron    : gap < 5 min. Impossible under ANY TTL tier. The headline.
  - expired : gap over the TTL - legitimate, not counted as loss.

It reads ONLY local metadata and prints ONLY aggregate numbers - no
conversation content, no session ids. Safe to paste publicly.
Dedups by requestId (one response is logged once per content block; naive
counting inflates totals 2-3x).

Requires Python 3.8+, stdlib only. Run:  python check_cache_loss.py

--json prints a machine-readable aggregate (totals + daily[]) instead of the
table, for pasting into the observatory's check page (paste fallback). The
flag only changes the OUTPUT SHAPE - the judgment logic above is identical.
(This flag is an observatory-repo addition on top of the original v2.1
script; the classification rules are unchanged.)

DETECTOR CENSUS: the classification above asks one question per request - is
the reason PMNF? - and every request that answers no disappears into a single
silent bucket. That bucket mixes requests carrying no diagnostic at all (the
normal case) with requests carrying a reason this script does not recognise.
If the server ever renames the reason, every loss lands in the second group
and this script prints a confident zero. The census below counts reason
values and client versions so that case reads as "I no longer know" instead
of "nothing happened". It is counting only: no total or classification above
depends on it. (Observatory-repo addition; v2.1 judgment unchanged.)
"""
import json, glob, os, re, sys
from collections import defaultdict
from datetime import datetime

JSON_MODE = "--json" in sys.argv[1:]

ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")

months = defaultdict(lambda: defaultdict(int))
days = defaultdict(lambda: defaultdict(int))
seen = set()
nfiles = 0

# Census keys can reach a public JSON file through the paste path, so a log
# file must never be able to put free text there: a value survives verbatim
# only if it matches this charset. The three literals carry parentheses,
# which the charset excludes, so no real value can collide with one.
# Mirrors assets/parse.js (same names, same rules).
CENSUS_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CENSUS_MAX_KEYS = 12
KEY_INVALID = "(invalid)"
KEY_MISSING = "(none)"
KEY_OTHER = "(other)"
reason_census = defaultdict(int)
version_census = defaultdict(int)
det = {"diagnosed_requests": 0, "unknown_reasons": 0, "cold_writes": 0}


def census_num(v):
    """The web engine coerces token counts at storage time; this script does
    not, because its judgment path is the untouched v2.1 code. Coerce here so
    the two censuses agree on a hostile log instead of diverging."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return v if v >= 0 else 0


def census_add(bucket, raw, missing_is_key):
    if raw is None:
        if not missing_is_key:
            return
        key = KEY_MISSING
    else:
        key = raw if isinstance(raw, str) and CENSUS_KEY_RE.match(raw) else KEY_INVALID
    bucket[key] += 1


def census_out(bucket):
    """Deterministic top-N: count desc, then key asc. Sorting rather than
    truncating on insertion order is what lets this and parse.js agree - the
    two walk files in different orders and only the tally is shared."""
    pairs = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
    out = {}
    other = 0
    for i, (k, v) in enumerate(pairs):
        if i < CENSUS_MAX_KEYS:
            out[k] = v
        else:
            other += v
    if other:
        out[KEY_OTHER] = out.get(KEY_OTHER, 0) + other
    return out

def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

for f in glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True):
    nfiles += 1
    is_sub = (os.sep + "subagents" + os.sep) in f
    reqs = []
    byrid = {}
    meta = {}  # rid -> (cache_read, version); census only, never judgment
    try:
        fh = open(f, encoding="utf-8", errors="replace")
    except OSError:
        continue
    with fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            msg = o.get("message") or {}
            u = msg.get("usage")
            if not (u and isinstance(u, dict)):
                continue
            rid = o.get("requestId") or msg.get("id")
            diag = (msg.get("diagnostics") or {}).get("cache_miss_reason")
            rtype = diag.get("type") if isinstance(diag, dict) else diag
            if rid in seen:
                # v2.1: the reason may sit on a later record of the same
                # requestId - keep the first record, fill in a missing reason.
                prev = byrid.get(rid)
                if prev and prev[2] is None and rtype:
                    byrid[rid] = (prev[0], prev[1], rtype)
                continue
            seen.add(rid)
            dt = parse_ts(o.get("timestamp") or "")
            if not dt:
                continue
            byrid[rid] = (dt, u.get("cache_creation_input_tokens", 0) or 0, rtype)
            meta[rid] = (census_num(u.get("cache_read_input_tokens", 0)),
                         o.get("version"))
    # Census pass. Its own loop over the same deduped records on purpose: the
    # judgment loop below is the SSOT and stays untouched, so counting can
    # never feed back into classification. rtype here is the back-filled final
    # value. Order does not matter - census_out() sorts.
    for rid, (_dt, _cc, _rtype) in byrid.items():
        cr, ver = meta.get(rid, (0, None))
        census_add(version_census, ver, True)
        if _rtype is not None:
            census_add(reason_census, _rtype, False)
            det["diagnosed_requests"] += 1
            if _rtype != "previous_message_not_found":
                det["unknown_reasons"] += 1
        # A full context write with nothing read back. Normal at session
        # start; context for the reader, never a loss.
        if census_num(_cc) > 0 and cr == 0:
            det["cold_writes"] += 1
    reqs = sorted(byrid.values())
    for i, (dt, cc, rtype) in enumerate(reqs):
        mo = dt.strftime("%Y-%m")
        d = months[mo]
        dd = days[dt.strftime("%Y-%m-%d")]  # daily bucket for --json (same dt)
        d["req"] += 1
        d["cc"] += cc
        dd["req"] += 1
        if rtype != "previous_message_not_found":
            continue
        d["pmnf_raw"] += 1
        gap = (dt - reqs[i - 1][0]).total_seconds() if i > 0 else None
        if gap is None:
            continue
        in_ttl = gap < (300 if is_sub else 1800)
        if in_ttl:
            d["loss"] += 1
            d["loss_cc"] += cc
            dd["loss"] += 1
            dd["loss_cc"] += cc
            if gap < 300:
                d["iron"] += 1
                d["iron_cc"] += cc
                dd["iron"] += 1

if JSON_MODE:
    # Output shaping only - buckets above were filled by the unchanged
    # judgment loop. Shape mirrors the web engine (assets/parse.js).
    out = {
        "script_version": "cli-2.1",
        "totals": {
            "requests": sum(d["req"] for d in days.values()),
            "in_ttl_losses": sum(d["loss"] for d in days.values()),
            "iron_losses": sum(d["iron"] for d in days.values()),
            "wasted_tokens": sum(d["loss_cc"] for d in days.values()),
        },
        "daily": [
            {"date": k, "requests": days[k]["req"], "losses": days[k]["loss"],
             "wasted_tokens": days[k]["loss_cc"]}
            for k in sorted(days)
        ],
        "detector": {
            "diagnosed_requests": det["diagnosed_requests"],
            "unknown_reasons": det["unknown_reasons"],
            "cold_writes": det["cold_writes"],
            "reasons": census_out(reason_census),
            "versions": census_out(version_census),
        },
    }
    print(json.dumps(out, indent=2))
    sys.exit(0)

print("Claude Code prompt-cache loss self-check (TTL-aware)")
print(f"scanned {nfiles} session files, {len(seen)} unique API requests\n")
hdr = (f"{'month':7} {'requests':>9} {'cache_write_tok':>16} {'PMNF_raw':>8} "
       f"{'inTTL_loss':>10} {'inTTL_wasted':>13} {'share':>6} {'iron<5m':>8} {'iron_wasted':>12}")
print(hdr)
print("-" * len(hdr))
tot = defaultdict(int)
for m in sorted(months):
    d = months[m]
    share = 100 * d["loss_cc"] / d["cc"] if d["cc"] else 0
    print(f"{m:7} {d['req']:>9,} {d['cc']:>16,} {d['pmnf_raw']:>8} "
          f"{d['loss']:>10} {d['loss_cc']:>13,} {share:>5.1f}% {d['iron']:>8} {d['iron_cc']:>12,}")
    for k in d:
        tot[k] += d[k]
share = 100 * tot["loss_cc"] / tot["cc"] if tot["cc"] else 0
print("-" * len(hdr))
print(f"{'TOTAL':7} {tot['req']:>9,} {tot['cc']:>16,} {tot['pmnf_raw']:>8} "
      f"{tot['loss']:>10} {tot['loss_cc']:>13,} {share:>5.1f}% {tot['iron']:>8} {tot['iron_cc']:>12,}")
# Detector census. Kept to two-token lines on purpose: tests/parity_check.py
# reads this table by token count, and a 9-token line here would be mistaken
# for a data row.
reasons = census_out(reason_census)
versions = census_out(version_census)
print()
print("detector census (counting only - no total above depends on it)")
if not reasons:
    print("  cache_miss_reason: none present in any scanned record")
else:
    for k in sorted(reasons, key=lambda x: (-reasons[x], x)):
        mark = "" if k == "previous_message_not_found" else "   <- UNRECOGNISED"
        print("  reason %s = %d%s" % (k, reasons[k], mark))
for k in sorted(versions, key=lambda x: (-versions[x], x)):
    print("  version %s = %d" % (k, versions[k]))
print("  cold_writes = %d" % det["cold_writes"])
if det["unknown_reasons"]:
    print("""
WARNING: %d request(s) carry a cache_miss_reason this script does not know.
The loss figures above are computed from 'previous_message_not_found' alone,
so they are almost certainly TOO LOW. Report the unrecognised value."""
          % det["unknown_reasons"])
elif det["diagnosed_requests"] == 0 and tot["req"] > 0:
    print("""
NOTE: not one scanned record carried a cache_miss_reason. Either nothing
missed, or the diagnostic moved. This script cannot tell those two apart, so
read the zero above as 'no evidence', not as 'no losses'. cold_writes above
is the number of requests that wrote cache and read none back.""")

print("""
inTTL_loss = server-stamped 'previous_message_not_found' with idle gap
UNDER the cache TTL (30min margin for main sessions / 5min for subagents).
Per Anthropic's docs these should not happen. iron<5m = idle under 5 minutes,
impossible under ANY TTL tier. PMNF_raw includes legitimate expirations after
long idle - do NOT quote that column as a bug figure.
If inTTL_loss is non-zero, consider posting this output at:
https://github.com/anthropics/claude-code/issues/87966
If it is zero, that is valuable data too.""")
