#!/usr/bin/env python3
"""Cross-file consistency validator for the split public dataset (M14, M14.1).

Until M14 the dataset was one file, and "does this add up" was a question you
could answer by reading it. The split into an index, a fleet-wide daily series,
one detail file per submission and (M14.1) an identity map buys bounded
downloads at the cost of that property: four files can now disagree with each
other while each one looks internally fine.

This module is the answer to that. It is the single definition of what "the
dataset adds up" means, and it has two callers on purpose:

  * `python tests/dataset_validate.py` — checks the files committed in data/.
  * tests/submit_contract_test.py — checks the mock repository's files after
    EVERY accepted submission, so a merge, an increment, a re-scan and a
    ref-moved retry each have to leave a dataset that still adds up.

One implementation, both callers: a validator the live write path is not
checked against is a validator that drifts.

What is checked (all of it is arithmetic a reader could redo by hand):

  index      every row has a well-formed id, a detail path derived from that
             id, no `daily` array of its own, no `identity` block of its own,
             and totals whose iron subset cannot exceed the in-TTL count it is
             a subset of.
  index↔identity
             every index row has an identity entry OR EXPLICITLY NONE — a row
             with no fingerprint is a legitimate state, not a defect, and the
             row committed before M13 is exactly that. What is not legitimate
             is an identity entry keyed by an id the index does not list: that
             is a fingerprint belonging to no row, which means either a row was
             dropped without its identity or the write path invented a key. An
             entry that exists must also be well formed — at most 16 anchor
             digests, each a lowercase sha-256, a token hash of the same shape,
             and at least one of the two present.
  index↔detail
             exactly one detail file per row and no orphans; the detail file
             names the row it belongs to; the row's totals are EXACTLY the sum
             of that detail file's own daily rows; the day count the index
             advertises is the number of rows the detail file really holds; the
             period the row claims contains every day the detail file lists.
  daily      the fleet series equals the sum ACROSS all detail files, date by
             date, for requests, losses and re-billed tokens, and `machines`
             is the number of detail files that carry that date. Its set of
             dates is exactly the union of the detail files' dates: a date in
             the fleet series that no submission covers is a fabricated day.

Exit codes: 0 = valid, 1 = one or more violations (each printed), 2 = the
files could not be read at all.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
DATA_DIR = os.path.join(SITE_DIR, "data")

INDEX_PATH = "data/submissions.json"
DAILY_PATH = "data/daily.json"
IDENTITY_PATH = "data/identity.json"
SUBS_DIR = "data/subs"

INDEX_SCHEMA_VERSION = 2
DAILY_SCHEMA_VERSION = 1
DETAIL_SCHEMA_VERSION = 1
IDENTITY_SCHEMA_VERSION = 1

# Mirrors MAX_ANCHORS / HEX64_RE in functions/api/submit.js.
MAX_ANCHORS = 16
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Mirrors SUB_ID_RE in functions/api/submit.js. The id becomes a file NAME, so
# anything outside this alphabet is refused rather than sanitised: a lenient id
# is a path-traversal primitive in the one code path that writes files.
SUB_ID_RE = re.compile(r"^sub-[0-9]{14}-[0-9a-f]{4}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOTALS_FIELDS = ("requests", "in_ttl_losses", "iron_losses", "wasted_tokens")
DAILY_FIELDS = ("requests", "losses", "wasted_tokens")
# fleet-series column -> the per-day column it sums
FLEET_COLUMNS = (("requests", "requests"),
                 ("losses", "losses"),
                 ("wasted_tokens", "wasted_tokens"))


def detail_path(sub_id):
    return "%s/%s.json" % (SUBS_DIR, sub_id)


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_day(v):
    return isinstance(v, str) and bool(DATE_RE.match(v))


def _identity_entries(identity_doc, err):
    """The {id -> entry} map out of data/identity.json, or None if the document
    itself is unusable. `None` for the whole document means the file is absent,
    which is the pre-M14.1 state and is valid: nobody has an identity yet."""
    if identity_doc is None:
        return {}
    if not isinstance(identity_doc, dict):
        err.append("identity: not a JSON object")
        return None
    if identity_doc.get("schema_version") != IDENTITY_SCHEMA_VERSION:
        err.append("identity: schema_version %r, want %d"
                   % (identity_doc.get("schema_version"), IDENTITY_SCHEMA_VERSION))
    entries = identity_doc.get("identities")
    if not isinstance(entries, dict):
        err.append("identity: identities is not an object")
        return None
    return entries


def _check_identity_entry(sub_id, entry, err):
    """An entry that exists has to be usable as a fingerprint. A malformed one
    is worse than none: it silently stops matching, and the machine it belonged
    to quietly opens a second row on its next submission — the exact double
    count M13 removed."""
    if not isinstance(entry, dict):
        err.append("identity/%s: entry is not an object" % sub_id)
        return
    extra = set(entry) - {"anchor_hashes", "token_hash"}
    if extra:
        err.append("identity/%s: undefined field(s) %s"
                   % (sub_id, ", ".join(sorted(extra))))
    anchors = entry.get("anchor_hashes")
    if "anchor_hashes" in entry:
        if not isinstance(anchors, list) or not anchors:
            err.append("identity/%s: anchor_hashes %r is not a non-empty array"
                       % (sub_id, anchors))
            anchors = []
        elif len(anchors) > MAX_ANCHORS:
            err.append("identity/%s: %d anchor hashes, max %d"
                       % (sub_id, len(anchors), MAX_ANCHORS))
        for h in anchors if isinstance(anchors, list) else []:
            if not isinstance(h, str) or not HEX64_RE.match(h):
                err.append("identity/%s: %r is not a lowercase sha-256 digest"
                           % (sub_id, h))
    if "token_hash" in entry:
        th = entry.get("token_hash")
        if not isinstance(th, str) or not HEX64_RE.match(th):
            err.append("identity/%s: token_hash %r is not a lowercase sha-256 "
                       "digest" % (sub_id, th))
    if not entry.get("anchor_hashes") and not entry.get("token_hash"):
        err.append("identity/%s: the entry holds neither anchor_hashes nor a "
                   "token_hash — an empty entry says 'this row's fingerprint "
                   "was lost', which is not the same as having none" % sub_id)


def validate(index_doc, daily_doc, details, identity_doc=None):
    """index_doc:    parsed data/submissions.json
       daily_doc:    parsed data/daily.json
       details:      {submission id -> parsed data/subs/<id>.json}
       identity_doc: parsed data/identity.json, or None when the file is absent
       Returns a list of human-readable violations; empty means valid."""
    err = []

    if not isinstance(index_doc, dict):
        return ["index: not a JSON object"]
    if index_doc.get("schema_version") != INDEX_SCHEMA_VERSION:
        err.append("index: schema_version %r, want %d"
                   % (index_doc.get("schema_version"), INDEX_SCHEMA_VERSION))
    rows = index_doc.get("submissions")
    if not isinstance(rows, list):
        return err + ["index: submissions is not an array"]

    identities = _identity_entries(identity_doc, err)

    seen_ids = set()
    # date -> [summed requests, losses, wasted, machines]
    fleet = {}

    for row in rows:
        if not isinstance(row, dict):
            err.append("index: a submission row is not an object")
            continue
        sub_id = row.get("id")
        if not isinstance(sub_id, str) or not SUB_ID_RE.match(sub_id):
            err.append("index: id %r is not sub-{14 digits}-{4 hex}" % (sub_id,))
            continue
        if sub_id in seen_ids:
            err.append("index/%s: the same id appears twice" % sub_id)
            continue
        seen_ids.add(sub_id)

        if "daily" in row:
            err.append("index/%s: the index row still carries a daily array — "
                       "the per-day detail belongs in %s"
                       % (sub_id, detail_path(sub_id)))
        # M14.1. The index is the file every visitor downloads, and the digests
        # are 70.7% of a fingerprinted row while being the one part of it no
        # reader ever looks at. A row that carries them again is a regression
        # in what a visitor pays to load the page.
        if "identity" in row:
            err.append("index/%s: the index row still carries an identity block "
                       "— the anchor digests and the token hash belong in %s"
                       % (sub_id, IDENTITY_PATH))
        # An index row with NO identity entry is legitimate and deliberately not
        # an error: sub-20260824115135-75fb predates M13 and has no fingerprint
        # to carry. Only a malformed entry, or an entry with no row, is a fault.
        if identities is not None and sub_id in identities:
            _check_identity_entry(sub_id, identities[sub_id], err)
        want_detail = detail_path(sub_id)
        if row.get("detail") != want_detail:
            err.append("index/%s: detail %r, want %r"
                       % (sub_id, row.get("detail"), want_detail))

        totals = row.get("totals")
        if not isinstance(totals, dict):
            err.append("index/%s: totals is not an object" % sub_id)
            continue
        for f in TOTALS_FIELDS:
            if not _is_count(totals.get(f)):
                err.append("index/%s: totals.%s %r is not a non-negative integer"
                           % (sub_id, f, totals.get(f)))
        if _is_count(totals.get("iron_losses")) and _is_count(totals.get("in_ttl_losses")) \
                and totals["iron_losses"] > totals["in_ttl_losses"]:
            err.append("index/%s: iron_losses %d exceeds in_ttl_losses %d — iron "
                       "is a SUBSET of in-TTL, so it can never be larger"
                       % (sub_id, totals["iron_losses"], totals["in_ttl_losses"]))

        ps, pe = row.get("period_start"), row.get("period_end")
        if not _is_day(ps) or not _is_day(pe):
            err.append("index/%s: period %r..%r is not two calendar days"
                       % (sub_id, ps, pe))
        elif ps > pe:
            err.append("index/%s: period_start %s is after period_end %s"
                       % (sub_id, ps, pe))

        detail = details.get(sub_id)
        if detail is None:
            err.append("index/%s: no detail file at %s — the row's totals cannot "
                       "be checked against anything" % (sub_id, want_detail))
            continue
        if not isinstance(detail, dict):
            err.append("%s: not a JSON object" % want_detail)
            continue
        if detail.get("schema_version") != DETAIL_SCHEMA_VERSION:
            err.append("%s: schema_version %r, want %d"
                       % (want_detail, detail.get("schema_version"),
                          DETAIL_SCHEMA_VERSION))
        if detail.get("id") != sub_id:
            err.append("%s: names id %r but sits at the path of %r"
                       % (want_detail, detail.get("id"), sub_id))

        daily = detail.get("daily")
        if not isinstance(daily, list) or not daily:
            err.append("%s: daily is missing or empty" % want_detail)
            continue

        dates = []
        ok_rows = True
        for d in daily:
            if not isinstance(d, dict) or not _is_day(d.get("date")):
                err.append("%s: a daily row has no valid date" % want_detail)
                ok_rows = False
                break
            for f in DAILY_FIELDS:
                if not _is_count(d.get(f)):
                    err.append("%s/%s: %s %r is not a non-negative integer"
                               % (want_detail, d["date"], f, d.get(f)))
                    ok_rows = False
            if d.get("losses", 0) > d.get("requests", 0):
                err.append("%s/%s: losses %r exceed requests %r"
                           % (want_detail, d["date"], d.get("losses"),
                              d.get("requests")))
                ok_rows = False
            dates.append(d["date"])
        if not ok_rows:
            continue

        if dates != sorted(dates):
            err.append("%s: daily rows are not in date order" % want_detail)
        if len(set(dates)) != len(dates):
            err.append("%s: a date appears more than once" % want_detail)
        if _is_day(ps) and _is_day(pe) and (dates[0] < ps or dates[-1] > pe):
            err.append("%s: daily rows %s..%s fall outside the period %s..%s the "
                       "index claims" % (want_detail, dates[0], dates[-1], ps, pe))
        if detail.get("period_start") != ps or detail.get("period_end") != pe:
            err.append("%s: period %r..%r disagrees with the index's %r..%r"
                       % (want_detail, detail.get("period_start"),
                          detail.get("period_end"), ps, pe))

        days_claimed = row.get("daily_days")
        if days_claimed != len(daily):
            err.append("index/%s: daily_days says %r but %s holds %d rows"
                       % (sub_id, days_claimed, want_detail, len(daily)))

        # 🔴 the reader-checkable equality: the row's totals ARE the sum of its
        # own detail file. Nothing else in the dataset needs to be trusted to
        # verify this one.
        for total_key, daily_key in (("requests", "requests"),
                                     ("in_ttl_losses", "losses"),
                                     ("wasted_tokens", "wasted_tokens")):
            got = sum(d[daily_key] for d in daily)
            if isinstance(totals.get(total_key), int) and got != totals[total_key]:
                err.append("index/%s: totals.%s says %d but %s sums to %d"
                           % (sub_id, total_key, totals[total_key],
                              want_detail, got))
        dt = detail.get("totals")
        if dt != totals:
            err.append("%s: its own totals %r differ from the index row's %r"
                       % (want_detail, dt, totals))

        for d in daily:
            slot = fleet.setdefault(d["date"], [0, 0, 0, 0])
            slot[0] += d["requests"]
            slot[1] += d["losses"]
            slot[2] += d["wasted_tokens"]
            slot[3] += 1

    orphans = sorted(set(details) - seen_ids)
    for oid in orphans:
        err.append("%s: a detail file no index row points at" % detail_path(oid))

    # 🔴 The asymmetry is the point. A row with no identity entry is fine; an
    # entry with no row is not. The write path derives this file from the index
    # it just wrote, so an orphan here means either a row was dropped without
    # its fingerprint or a key was invented — and a fingerprint that belongs to
    # no row is an overwrite key aimed at nothing, sitting in a public file.
    if identities is not None:
        for oid in sorted(set(identities) - seen_ids):
            err.append("identity/%s: an identity entry the index has no row for"
                       % oid)

    # -- the fleet series --------------------------------------------------
    if not isinstance(daily_doc, dict):
        return err + ["daily: not a JSON object"]
    if daily_doc.get("schema_version") != DAILY_SCHEMA_VERSION:
        err.append("daily: schema_version %r, want %d"
                   % (daily_doc.get("schema_version"), DAILY_SCHEMA_VERSION))
    days = daily_doc.get("days")
    if not isinstance(days, list):
        return err + ["daily: days is not an array"]

    seen_days = []
    for d in days:
        if not isinstance(d, dict) or not _is_day(d.get("date")):
            err.append("daily: a row has no valid date")
            continue
        date = d["date"]
        seen_days.append(date)
        for f in ("requests", "losses", "wasted_tokens", "machines"):
            if not _is_count(d.get(f)):
                err.append("daily/%s: %s %r is not a non-negative integer"
                           % (date, f, d.get(f)))
        want = fleet.get(date)
        if want is None:
            err.append("daily/%s: the fleet series carries a day no submission "
                       "covers" % date)
            continue
        for i, (col, _src) in enumerate(FLEET_COLUMNS):
            if d.get(col) != want[i]:
                err.append("daily/%s: %s says %r but the detail files sum to %d"
                           % (date, col, d.get(col), want[i]))
        if d.get("machines") != want[3]:
            err.append("daily/%s: machines says %r but %d detail file(s) carry "
                       "that date" % (date, d.get("machines"), want[3]))

    if seen_days != sorted(seen_days):
        err.append("daily: rows are not in date order")
    if len(set(seen_days)) != len(seen_days):
        err.append("daily: a date appears more than once")
    for missing in sorted(set(fleet) - set(seen_days)):
        err.append("daily/%s: a day the detail files cover is missing from the "
                   "fleet series" % missing)
    return err


# ---------------------------------------------------------------------------
# on-disk entry point
# ---------------------------------------------------------------------------

def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_from_disk(site_dir=SITE_DIR):
    index_doc = _read_json(os.path.join(site_dir, "data", "submissions.json"))
    daily_doc = _read_json(os.path.join(site_dir, "data", "daily.json"))
    # Absent is valid and is not turned into an empty document here: the
    # validator is told "there is no file", not "there is a file saying nobody
    # has an identity", because only one of those two is the truth on disk.
    identity_path = os.path.join(site_dir, "data", "identity.json")
    identity_doc = _read_json(identity_path) if os.path.isfile(identity_path) else None
    details = {}
    subs_dir = os.path.join(site_dir, "data", "subs")
    if os.path.isdir(subs_dir):
        for name in sorted(os.listdir(subs_dir)):
            if not name.endswith(".json"):
                continue
            details[name[:-5]] = _read_json(os.path.join(subs_dir, name))
    return index_doc, daily_doc, details, identity_doc


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    try:
        index_doc, daily_doc, details, identity_doc = load_from_disk()
    except (OSError, ValueError) as exc:
        print("FATAL: could not read the dataset: %s" % exc)
        return 2
    errors = validate(index_doc, daily_doc, details, identity_doc)
    if errors:
        print("DATASET_INVALID: %d violation(s)" % len(errors))
        for e in errors:
            print("  - %s" % e)
        return 1
    rows = index_doc["submissions"]
    days = daily_doc["days"]
    total_daily_rows = sum(len(d["daily"]) for d in details.values())
    entries = (identity_doc or {}).get("identities", {})
    without = [r["id"] for r in rows if r["id"] not in entries]
    print("index      %s: %d submission(s)" % (INDEX_PATH, len(rows)))
    print("fleet      %s: %d calendar day(s)%s"
          % (DAILY_PATH, len(days),
             ", %s..%s" % (days[0]["date"], days[-1]["date"]) if days else ""))
    print("detail     %s/: %d file(s), %d daily row(s) in total"
          % (SUBS_DIR, len(details), total_daily_rows))
    print("identity   %s: %s, %d entr(y/ies), %d row(s) with none"
          % (IDENTITY_PATH,
             "absent" if identity_doc is None else "present",
             len(entries), len(without)))
    print("DATASET_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
