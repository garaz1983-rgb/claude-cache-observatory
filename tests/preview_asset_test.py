"""assets/preview.js is published data, so it is checked like published data.

scripts/build_preview.js already refuses to WRITE a file carrying a requestId
or a log path. That guard only fires when someone runs the generator. This one
fires on the file that is actually committed, which is the file that actually
gets served — a hand-edit, a partial revert, or a generator someone modified
all reach the site without the build guard ever running again.

Two things are checked, and they are different kinds of wrong:

  identifiers   A published requestId is the worst single value in this repo.
                It is M13's identity anchor: whoever holds one can submit as
                this machine and take over its row. Log paths are next, since
                they name the projects on a person's disk. Hours are published
                here on purpose (see preview.js's header) — identifiers are
                not, and that distinction is the whole reason this file can
                exist at all.

  arithmetic    The headline KPIs are drawn from `totals` while the chart is
                drawn from `DAYS`. Nothing in the page forces them to agree, so
                an edited total would show a number the rows underneath it do
                not add up to — on the one block of the landing page whose
                claim is that it is measured.

Exit codes: 0 = ok, 1 = a violation (each printed), 2 = the file is missing.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
PREVIEW = os.path.join(SITE, "assets", "preview.js")

# Mirrors FORBIDDEN in scripts/build_preview.js. Kept as its own copy on
# purpose: a checker that imports the thing it is checking agrees with it by
# construction, which is not the same as being right.
FORBIDDEN = [
    (re.compile(r"req_[A-Za-z0-9]{6,}"), "a requestId (M13 identity anchor)"),
    (re.compile(r"toolu_[A-Za-z0-9]{6,}"), "a tool-use id"),
    (re.compile(r"msg_[A-Za-z0-9]{6,}"), "a message id"),
    (re.compile(r"\.jsonl"), "a log file path"),
    (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+"), "a Windows user path"),
    (re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"), "a POSIX home path"),
    (re.compile(r"c--[A-Za-z0-9-]+"), "a Claude Code project key"),
    (re.compile(r"\b[0-9a-f]{32,}\b"), "a long hex digest"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}\b"), "a session UUID"),
]

DAY_RE = re.compile(
    r'\{date:"(\d{4}-\d{2}-\d{2})",requests:(\d+),losses:(\d+)\}')
TOTAL_RE = re.compile(r"^\s*(requests|losses|iron|tokens|rate):\s*([0-9.]+)",
                      re.MULTILINE)
EVT_RE = re.compile(r"\{time:\"[^\"]+\",gapMin:([0-9.]+),tokens:(\d+),sub:([01])\}")


def main():
    if not os.path.isfile(PREVIEW):
        print("FATAL: assets/preview.js is missing. Generate it with:")
        print("  node scripts/build_preview.js <logRoot>")
        return 2
    with io.open(PREVIEW, encoding="utf-8", newline="") as fh:
        text = fh.read()

    err = []

    for rx, what in FORBIDDEN:
        m = rx.search(text)
        if m:
            err.append("preview.js contains %s: %r" % (what, m.group(0)[:48]))

    if "MEASURED: true" not in text or "SYNTHETIC: false" not in text:
        err.append("preview.js must declare MEASURED: true and SYNTHETIC: false — "
                   "the page's badge says the numbers are measured and the file "
                   "is what makes that true or false")

    days = DAY_RE.findall(text)
    if not days:
        err.append("preview.js holds no daily rows")
    else:
        dates = [d[0] for d in days]
        if dates != sorted(dates):
            err.append("daily rows are not in date order")
        if len(set(dates)) != len(dates):
            err.append("a date appears twice in the daily rows")

    totals = dict(TOTAL_RE.findall(text))
    for key in ("requests", "losses", "iron", "tokens", "rate"):
        if key not in totals:
            err.append("totals.%s is missing" % key)

    if days and "requests" in totals and "losses" in totals:
        sum_req = sum(int(d[1]) for d in days)
        sum_loss = sum(int(d[2]) for d in days)
        if sum_req != int(totals["requests"]):
            err.append("totals.requests is %s but the rows add up to %d"
                       % (totals["requests"], sum_req))
        if sum_loss != int(totals["losses"]):
            err.append("totals.losses is %s but the rows add up to %d"
                       % (totals["losses"], sum_loss))

    events = EVT_RE.findall(text)
    if "losses" in totals and len(events) != int(totals["losses"]):
        err.append("totals.losses is %s but %d event(s) are listed — every loss "
                   "the KPI counts has to be a cell the visitor can open"
                   % (totals["losses"], len(events)))
    if events and "iron" in totals:
        # iron is the sub-5-minute subset, recounted rather than trusted.
        iron = sum(1 for e in events if float(e[0]) < 5)
        if iron != int(totals["iron"]):
            err.append("totals.iron is %s but %d event(s) have a gap under 5 min"
                       % (totals["iron"], iron))
    if events and "tokens" in totals:
        tok = sum(int(e[1]) for e in events)
        if tok != int(totals["tokens"]):
            err.append("totals.tokens is %s but the events add up to %d"
                       % (totals["tokens"], tok))

    if err:
        for e in err:
            print("  " + e)
        print("PREVIEW_ASSET_FAILED (%d)" % len(err))
        return 1

    print("preview  assets/preview.js: %d day rows, %d loss event(s), "
          "%s requests, %s re-billed tokens"
          % (len(days), len(events), totals["requests"], totals["tokens"]))
    print("  identifiers: none of %d forbidden patterns present" % len(FORBIDDEN))
    print("  arithmetic:  totals equal the rows and events they are drawn from")
    print("PREVIEW_ASSET_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
