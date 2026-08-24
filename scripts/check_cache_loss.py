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
"""
import json, glob, os, sys
from collections import defaultdict
from datetime import datetime

JSON_MODE = "--json" in sys.argv[1:]

ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")

months = defaultdict(lambda: defaultdict(int))
days = defaultdict(lambda: defaultdict(int))
seen = set()
nfiles = 0

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
print("""
inTTL_loss = server-stamped 'previous_message_not_found' with idle gap
UNDER the cache TTL (30min margin for main sessions / 5min for subagents).
Per Anthropic's docs these should not happen. iron<5m = idle under 5 minutes,
impossible under ANY TTL tier. PMNF_raw includes legitimate expirations after
long idle - do NOT quote that column as a bug figure.
If inTTL_loss is non-zero, consider posting this output at:
https://github.com/anthropics/claude-code/issues/87966
If it is zero, that is valuable data too.""")
