#!/usr/bin/env python3
"""Claude Code prompt-cache loss self-check (v3 - billing-shape judgment).

Your Claude Code session logs (~/.claude/projects/**/*.jsonl) record, for
every API request, the two cache fields of the bill itself:

    cache_read_input_tokens      what was read back from prompt cache
    cache_creation_input_tokens  what had to be (re)written

and sometimes a server-side annotation, message.diagnostics.cache_miss_reason.

v2.1 of this script judged on that annotation: reason ==
"previous_message_not_found" (PMNF) plus a short idle gap. Anthropic support
confirmed (2026-08-28) what the docs imply: PMNF does not mean a cache entry
was lost - it means the diagnostic could not compare (fingerprint expired,
beta header absent, other workspace). The reason field describes the state of
the DIAGNOSTIC, not the state of the cache. The billing fields have no such
problem: they are the bill.

v3 therefore judges on billing shape. Every request is classified by the idle
gap to the previous request in the same session file, its cache_read (cr) and
cache_creation (cc):

  confirmed loss : gap < TTL, cr == 0, cc > 0, reason not excused.
                   The whole cache was gone minutes after use. This is
                   Anthropic's own definition of a real miss.
  probable loss  : gap < TTL, cr > 0, cc >= 100,000, reason not excused.
                   The stable prefix still read; the conversation body was
                   re-created at a scale far outside the normal distribution
                   (fleet p99 measured at 83,844; judged-loss median at
                   205,288).
  excused        : either shape above, but the reason declares the prompt
                   prefix really changed (messages/model/system/tools
                   _changed). Re-creating it is correct behaviour, so it is
                   counted separately and never as a loss.
  iron           : a confirmed or probable loss with gap < 5 min. Impossible
                   under ANY TTL tier. The headline.
  otherwise      : session-start cold write (no prior request), legitimate
                   expiry (gap over TTL), or ordinary incremental writes.

An UNKNOWN reason name does not excuse: judgment never depends on knowing a
name, only the four excuse names have any effect, and every name still lands
in the census below so a new one is seen by a human.

The v2.1 series is kept alongside, unchanged (pmnf_losses / per-day pmnf):
it tracked real-world degradation months before v3 existed, and a series
measured with the same ruler over time is worth more than a corrected ruler
with no history.

TTL windows are unchanged from v2.1: gap < 30 min for main sessions (1h TTL
with margin for response-duration skew), gap < 5 min for subagent sessions.

It reads ONLY local metadata and prints ONLY aggregate numbers - no
conversation content, no session ids. Safe to paste publicly.
Dedups by requestId (one response is logged once per content block; naive
counting inflates totals 2-3x).

Requires Python 3.8+, stdlib only. Run:  python check_cache_loss.py

--json prints a machine-readable aggregate (totals + daily[]) instead of the
table, for pasting into the observatory's check page (paste fallback). The
flag only changes the OUTPUT SHAPE - the judgment logic above is identical.

DETECTOR CENSUS: judgment no longer keys on any reason NAME, but the census
below still counts every reason value and client version seen, so a renamed
or brand-new annotation reads as "a name I have not seen" instead of
vanishing. It is counting only: no total above depends on it.
"""
import json, glob, os, re, sys
from collections import defaultdict
from datetime import datetime

JSON_MODE = "--json" in sys.argv[1:]

ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# --- v3 judgment constants (mirrors assets/parse.js) -----------------------
PMNF = "previous_message_not_found"
EXCUSED_REASONS = frozenset(
    ["messages_changed", "model_changed", "system_changed", "tools_changed"])
# Fixed absolute threshold, never a per-scan percentile: the same request must
# classify the same way no matter what else is in the folder, or no result is
# reproducible. Rationale for the value: fleet p99 measured 83,844 (so this is
# outside the top 1% of normal writes) and judged-loss median measured 205,288
# (so it is well inside the loss distribution). 2026-08-28 measurement.
PROBABLE_CC_MIN = 100_000
# Census bookkeeping only (mirrors assets/parse.js): the names whose
# appearance is ordinary. Anything else is counted as unknown - under v3 an
# unknown name cannot hide a loss, but it could be a new excuse-type
# declaration whose rebuilds are counted instead of excused.
KNOWN_REASONS = frozenset([PMNF, "unavailable"]) | EXCUSED_REASONS

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
    """Coerce token counts so a hostile log cannot poison a sum or a census.
    v3 note: the JUDGMENT path now also consumes coerced numbers (cc/cr), the
    same coercion parse.js applies at record time - the two engines must agree
    on a hostile log instead of diverging."""
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
    byrid = {}
    meta = {}  # rid -> version; census only, never judgment
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
                # The reason may sit on a later record of the same requestId
                # - keep the first record, fill in a missing reason (v2.1
                # behaviour, unchanged).
                prev = byrid.get(rid)
                if prev and prev[3] is None and rtype:
                    byrid[rid] = (prev[0], prev[1], prev[2], rtype)
                continue
            seen.add(rid)
            dt = parse_ts(o.get("timestamp") or "")
            if not dt:
                continue
            byrid[rid] = (dt,
                          census_num(u.get("cache_creation_input_tokens", 0)),
                          census_num(u.get("cache_read_input_tokens", 0)),
                          rtype)
            meta[rid] = o.get("version")
    # Census pass. Its own loop over the same deduped records on purpose:
    # counting can never feed back into classification. rtype here is the
    # back-filled final value. Order does not matter - census_out() sorts.
    for rid, (_dt, _cc, _cr, _rtype) in byrid.items():
        census_add(version_census, meta.get(rid), True)
        if _rtype is not None:
            census_add(reason_census, _rtype, False)
            det["diagnosed_requests"] += 1
            if _rtype not in KNOWN_REASONS:
                det["unknown_reasons"] += 1
        # A full context write with nothing read back. Normal at session
        # start; context for the reader, never a loss BY ITSELF (v3 judges the
        # same shape as a loss only when a prior request sits minutes before).
        if _cc > 0 and _cr == 0:
            det["cold_writes"] += 1
    # Sort by (time, cc) ONLY - parse.js sorts by exactly these two keys and
    # leaves further ties in insertion order (stable sort on both sides).
    # v2.1 compared whole tuples, which silently used the reason string as a
    # third key; on a same-instant tie the engines could disagree.
    reqs = sorted(byrid.values(), key=lambda r: (r[0], r[1]))
    ttl = 300 if is_sub else 1800
    for i, (dt, cc, cr, rtype) in enumerate(reqs):
        mo = dt.strftime("%Y-%m")
        d = months[mo]
        dd = days[dt.strftime("%Y-%m-%d")]  # daily bucket for --json (same dt)
        d["req"] += 1
        d["cc"] += cc
        dd["req"] += 1
        gap = (dt - reqs[i - 1][0]).total_seconds() if i > 0 else None

        # --- v2.1 legacy series, rule unchanged: PMNF and gap under TTL ---
        if rtype == PMNF:
            d["pmnf_raw"] += 1
            if gap is not None and gap < ttl:
                d["pmnf"] += 1
                dd["pmnf"] += 1

        # --- v3 judgment: billing shape; reason names only ever excuse ---
        if gap is None or gap >= ttl:
            continue
        loss_shape = (cr == 0 and cc > 0) or (cr > 0 and cc >= PROBABLE_CC_MIN)
        if not loss_shape:
            continue
        if rtype in EXCUSED_REASONS:
            d["excused"] += 1
            dd["excused"] += 1
            continue
        tier = "conf" if cr == 0 else "prob"
        d[tier] += 1
        dd[tier] += 1
        d["loss_cc"] += cc
        dd["loss_cc"] += cc
        if gap < 300:
            d["iron"] += 1
            d["iron_cc"] += cc
            dd["iron"] += 1

if JSON_MODE:
    # Output shaping only - buckets above were filled by the judgment loop.
    # Shape mirrors the web engine (assets/parse.js).
    out = {
        "script_version": "cli-3.0",
        "totals": {
            "requests": sum(d["req"] for d in days.values()),
            "confirmed_losses": sum(d["conf"] for d in days.values()),
            "probable_losses": sum(d["prob"] for d in days.values()),
            "iron_losses": sum(d["iron"] for d in days.values()),
            "wasted_tokens": sum(d["loss_cc"] for d in days.values()),
            "pmnf_losses": sum(d["pmnf"] for d in days.values()),
            "excused_rebuilds": sum(d["excused"] for d in days.values()),
        },
        "daily": [
            {"date": k, "requests": days[k]["req"],
             "losses": days[k]["conf"] + days[k]["prob"],
             "pmnf": days[k]["pmnf"],
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

print("Claude Code prompt-cache loss self-check (v3 - billing-shape judgment)")
print(f"scanned {nfiles} session files, {len(seen)} unique API requests\n")
# Data rows are exactly 9 tokens (month + 8 numbers) on purpose:
# tests/parity_check.py reads this table by token count.
hdr = (f"{'month':7} {'requests':>9} {'cache_write_tok':>16} {'confirmed':>9} "
       f"{'probable':>8} {'wasted_tok':>12} {'iron<5m':>8} {'excused':>8} {'pmnf_v21':>8}")
print(hdr)
print("-" * len(hdr))
tot = defaultdict(int)
for m in sorted(months):
    d = months[m]
    print(f"{m:7} {d['req']:>9,} {d['cc']:>16,} {d['conf']:>9} "
          f"{d['prob']:>8} {d['loss_cc']:>12,} {d['iron']:>8} {d['excused']:>8} {d['pmnf']:>8}")
    for k in d:
        tot[k] += d[k]
print("-" * len(hdr))
print(f"{'TOTAL':7} {tot['req']:>9,} {tot['cc']:>16,} {tot['conf']:>9} "
      f"{tot['prob']:>8} {tot['loss_cc']:>12,} {tot['iron']:>8} {tot['excused']:>8} {tot['pmnf']:>8}")
# Detector census. Kept to two-token lines on purpose: tests/parity_check.py
# reads the table above by token count, and a 9-token line here would be
# mistaken for a data row.
reasons = census_out(reason_census)
versions = census_out(version_census)
print()
print("detector census (counting only - no total above depends on it)")
if not reasons:
    print("  cache_miss_reason: none present in any scanned record")
else:
    for k in sorted(reasons, key=lambda x: (-reasons[x], x)):
        if k == PMNF or k in EXCUSED_REASONS or k == "unavailable":
            mark = ""
        else:
            mark = "   <- UNSEEN-BEFORE NAME"
        print("  reason %s = %d%s" % (k, reasons[k], mark))
for k in sorted(versions, key=lambda x: (-versions[x], x)):
    print("  version %s = %d" % (k, versions[k]))
print("  cold_writes = %d" % det["cold_writes"])

print("""
confirmed = cache_read 0 with a prior request under the TTL (30min margin for
main sessions / 5min for subagents): the whole cache was gone minutes after
use - Anthropic's own definition of a real miss. probable = the stable prefix
read back but >= 100k tokens were re-created inside the TTL with no declared
prompt change. excused = the same shapes where the reason says the prompt
really changed (messages/model/system/tools) - correct behaviour, never
counted as loss. iron<5m = a counted loss with idle under 5 minutes,
impossible under ANY TTL tier. pmnf_v21 = the v2.1 series (reason-based),
kept for continuity - do not add it to the v3 columns, they overlap.
If confirmed or probable is non-zero, consider posting this output at:
https://github.com/anthropics/claude-code/issues/87966
If both are zero, that is valuable data too.""")
