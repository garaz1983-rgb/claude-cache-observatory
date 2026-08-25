#!/usr/bin/env python3
"""Contract test for functions/api/submit.js (06_FUNCTIONAL_SPEC.md section 2).

Self-contained: boots `npx wrangler pages dev` against the site directory
(KV binding RATE_LIMIT, GitHub API base pointed at a local mock), runs a mock
GitHub **Git Data** API in-process — a real content-addressed object store with
blobs, nested trees, commits and a fast-forward-only branch ref — and drives
the contract cases:

  case1  valid submission        -> 200, ONE commit carrying all three files
  case2  undefined field         -> 400 (reject, not drop)
  case3  losses > requests       -> 400
  case4  period > 92 days        -> 400
  case5  nickname 21 chars       -> 400
  case6  4th submit, same IP     -> 429 with retry_after
  case7  ref moved (422) once    -> re-read + one retry -> 200, and the retry
                                    merges against the commit that landed in
                                    between rather than its own stale read
  case8  GitHub 5xx              -> 502 storage (no commit, no retry)
  case9  daily []                -> 400 (inflated totals AND all-zero totals)
  case10 daily sums != totals    -> 400 (requests / losses / wasted each)
  case11 nickname masking        -> the raw nickname reaches neither the stored
                                    record nor the commit message; boundary
                                    inputs (1 char, hangul, astral emoji,
                                    padding, already-masked, HTML, empty,
                                    whitespace) all survive the rule

M13 (one submitter, one row) adds:

  case12 same machine twice      -> ONE row, merged in place, period widened,
                                    totals recomputed, id kept, anchors
                                    refreshed to the newest set
  case13 replay from the file    -> a value copied out of the public dataset
                                    modifies nothing; the target row comes back
                                    byte-identical
  case14 server-only fields      -> identity / anchor_hashes / token_hash /
                                    updated_at / id are rejected by the same
                                    whitelist as any unknown field, and
                                    malformed anchors and tokens are 400 too
  case15 increment               -> a disjoint later window is ADDED to the row,
                                    never replaces it
  case16 overlapping re-scan     -> the incoming rows win for the days both
                                    cover; the older ones survive elsewhere
  case17 paste path              -> no anchors, so the API issues a token; the
                                    next submission presenting it updates the
                                    same row, and one without it appends
  case18 paste then folder       -> a fingerprinted submission carrying the
                                    stored token adopts the paste row and from
                                    then on matches by fingerprint alone
  case19 masking on update       -> a merge cannot smuggle a raw nickname into
                                    the record or the commit message

M14 (three files, one commit) adds:

  case20 atomicity               -> every accepted submission lands the index,
                                    the identity map, the fleet series and its
                                    own detail file in exactly ONE commit, and
                                    touches nothing else
  case21 fleet series            -> data/daily.json is the sum across detail
                                    files date by date, `machines` is how many
                                    cover each date, and a merge moves it by the
                                    delta rather than double counting
  case22 missing detail file     -> a row whose detail file cannot be read is
                                    NOT merged into: 502, and the public files
                                    are left exactly as they were

M14.1 (the digests leave the index) adds:

  case23 no identity file yet    -> the first submission over a repository that
                                    has no data/identity.json at all creates it.
                                    Not hypothetical: it is the state of the
                                    live repo at the M14.1 commit, and it is the
                                    state _reset_repo() leaves behind, so every
                                    stage's first submission takes this path
  case24 a row with no identity  -> an index row that carries no fingerprint is
                                    a valid dataset, not a violation, and the
                                    ref-moved retry re-resolves the identity map
                                    against the fresh read without inventing an
                                    entry for it
  (validator)                    -> the other half of that relation — an entry
                                    keyed by an id the index does not list — is
                                    an error, proven by handing the validator
                                    one, because the write path cannot make one

M14.2 (simultaneous submissions, and the failures the mock could not make) adds:

  case25 a hand-edited detail row -> a row that is not a daily row makes the
                                    whole file unmergeable: 502, nothing
                                    committed. Before this, seven malformed
                                    shapes returned 200 over a dataset that no
                                    longer added up and an eighth escaped as 500
  case26 the branch never free   -> 409 {ok:false, error:"conflict",
                                    retry_after} after the full retry budget,
                                    NOT the 502 that means "storage is down";
                                    and 409 is honoured as a conflict status
                                    alongside GitHub's 422, so a contract
                                    difference degrades into a retry
  case27 a tree listing fails    -> 502. Removing either tree status guard used
                                    to survive every suite while turning a
                                    failed read into "this repository is empty"
  case28 the commit POST fails   -> 502, and the ref is never updated
  case29 June, then May          -> a submitter whose logs are older than the
                                    series leaves it in date order
  case30 a truncated listing     -> 502. GitHub truncates a tree at 100,000
                                    entries and data/subs/ is one file per
                                    submitter
  case31 a hand-edited series    -> a machines:0 day is dropped and a negative
                                    delta is clamped, so no public file carries
                                    a count below zero

🔴 After every accepted submission the whole dataset is re-checked with
tests/dataset_validate.py — the same validator that runs against the committed
files in data/. An index row's totals must equal the sum of its own detail
file, data/daily.json must equal the sum across all of them, and every entry in
data/identity.json must belong to a row the index lists.

All pass -> prints CONTRACT_OK as the last line, exit 0.
Contract violation -> exit 1. Setup/infra failure -> exit 2 (the mutation
harness treats exit 1 as KILLED and exit 2 as fatal, so infra problems can
never masquerade as a defended mutation).

No real GitHub, no real tokens, no network beyond 127.0.0.1.
"""
import base64
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dataset_validate

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
SUBMIT_JS = os.path.join(SITE_DIR, "functions", "api", "submit.js")

PORT = int(os.environ.get("CACHE_OBS_SUBMIT_PORT", "8789"))
BASE = "http://127.0.0.1:%d" % PORT
MOCK_REPO = "mockowner/mockrepo"
MOCK_TOKEN = "test-token-local"  # dummy value, never a real credential
WRANGLER_SPEC = os.environ.get("CACHE_OBS_WRANGLER_SPEC", "wrangler@4")
BOOT_DEADLINE_SECONDS = 420

SCRATCH_PREFIX = "cacheobs_submit_"
SCRATCH_MARKER = ".cacheobs_scratch"

NPX_FALLBACKS = [
    r"C:\Program Files\nodejs\npx.cmd",
    "/usr/local/bin/npx",
    "/usr/bin/npx",
]


def find_npx():
    for name in ("npx.cmd", "npx"):
        which = shutil.which(name)
        if which:
            return os.path.abspath(which)
    for cand in NPX_FALLBACKS:
        if os.path.isfile(cand):
            return cand
    return None


class SetupFail(Exception):
    pass


class ContractFail(Exception):
    pass


# ---------------------------------------------------------------------------
# scratch dir (wrangler --persist-to + logs), DELETION_GUARD-compliant cleanup
# ---------------------------------------------------------------------------

def make_scratch():
    path = tempfile.mkdtemp(prefix=SCRATCH_PREFIX)
    with open(os.path.join(path, SCRATCH_MARKER), "w", encoding="utf-8") as fh:
        fh.write("scratch dir created by submit_contract_test.py\n")
    return path


def _rm_no_follow(path):
    """Recursive delete that never follows junctions/symlinks."""
    st = os.lstat(path)
    is_reparse = bool(getattr(st, "st_file_attributes", 0) &
                      getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if stat.S_ISLNK(st.st_mode) or is_reparse:
        # remove the link itself, never its target
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(path)
        else:
            os.unlink(path)
        return
    if stat.S_ISDIR(st.st_mode):
        with os.scandir(path) as entries:
            children = [e.path for e in entries]
        for child in children:
            _rm_no_follow(child)
        os.rmdir(path)
    else:
        os.unlink(path)


def guarded_delete_scratch(path):
    """Deletes only the path make_scratch() returned. Guards live in here:
    temp-root whitelist + prefix + marker check + no junction following.

    The marker is deleted last, after every other entry is gone. Windows keeps
    wrangler's state files locked for a moment after taskkill, so the first
    sweep can fail partway; leaving the marker until the end means the retry
    can still prove ownership instead of being refused for a missing marker."""
    if not path:
        raise RuntimeError("refusing delete: empty path")
    real = os.path.realpath(path)
    tmp_root = os.path.realpath(tempfile.gettempdir())
    if not real.startswith(tmp_root + os.sep):
        raise RuntimeError("refusing delete outside temp root: %r" % real)
    if not os.path.basename(real).startswith(SCRATCH_PREFIX):
        raise RuntimeError("refusing delete: prefix mismatch: %r" % real)
    marker = os.path.join(real, SCRATCH_MARKER)
    if not os.path.isfile(marker):
        raise RuntimeError("refusing delete: scratch marker missing: %r" % real)
    with os.scandir(real) as entries:
        children = [e.path for e in entries
                    if os.path.basename(e.path) != SCRATCH_MARKER]
    for child in children:
        _rm_no_follow(child)
    os.unlink(marker)
    os.rmdir(real)


# ---------------------------------------------------------------------------
# mock GitHub Git Data API (M14)
# ---------------------------------------------------------------------------
#
# Not a stub that says yes: a small content-addressed object store with real
# blobs, real nested trees, real commits and a real branch ref. Two properties
# of the write path only exist if the mock behaves like git, and both are what
# M14 is about:
#
#   * ATOMICITY. Three files land as one tree under one commit or not at all.
#     A mock that recorded "path X was written" could not tell that apart from
#     three separate writes; this one records the commit each path landed in.
#   * FAST-FORWARD ONLY. A commit whose parent is no longer the branch tip is
#     rejected with 422, exactly as GitHub rejects it, because the mock checks
#     the parent rather than being told to fail. That is the conflict the one
#     retry exists for, and `conflict_once` produces it by LANDING A REAL
#     COMMIT — so a retry that merged against its stale read would be caught.

INDEX_PATH = "data/submissions.json"
FLEET_PATH = "data/daily.json"
IDENTITY_PATH = "data/identity.json"
EMPTY_INDEX = {"schema_version": 2, "submissions": []}
EMPTY_FLEET = {"schema_version": 1, "days": []}
MOCK_BRANCH = "master"


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class MockState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}               # path -> text, the branch tip's snapshot
        self.blobs = {}               # blob sha -> text
        self.trees = {}               # tree sha -> {name: (type, sha)}
        self.tree_files = {}          # tree sha -> {path: text} (flat snapshot)
        self.commits = {}             # commit sha -> {tree, parents, message}
        self.head = None              # commit sha at refs/heads/master
        # normal | conflict_once | conflict_always | server_error
        self.mode = "normal"
        self.conflict_fired = False
        # M14.2. The mock could previously fail exactly one thing: a blob POST
        # (`server_error`). Two guards in the read path and one in the write
        # path therefore had nothing that could exercise them, and the mutation
        # harness measured them as survivors. These four dials are what a real
        # GitHub can do and this mock could not:
        #   fail_tree      -> GET /git/trees/<sha> answers 500 for the root, the
        #                     data/ tree or the data/subs/ tree, by position
        #   truncate_tree  -> the same listing comes back with truncated:true,
        #                     which is what GitHub does past 100,000 entries and
        #                     which means "entries are MISSING from this reply"
        #   fail_commit_post -> POST /git/commits answers 500
        #   conflict_status  -> the status a non-fast-forward PATCH answers with
        #                     (GitHub's is 422; 409 is the plausible alternative
        #                     the write path must also tolerate)
        self.fail_tree = None         # None | "root" | "data" | "subs"
        self.truncate_tree = None     # None | "root" | "data" | "subs"
        self.fail_commit_post = False
        self.conflict_status = 422
        self.conflict_message = "Update is not a fast forward"
        # Emulated per-call network latency. Applied OUTSIDE the state lock, so
        # concurrent submissions really do overlap; a sleep inside the lock
        # would serialise the very thing being measured. 0 = localhost speed,
        # which understates the conflict window by ~100x against real GitHub.
        self.latency_ms = 0
        self.requests = 0             # every HTTP call the worker made
        self.ref_gets = 0             # GET /git/ref  (one per read attempt)
        self.blob_posts = 0           # POST /git/blobs
        self.ref_patches = 0          # PATCH /git/refs
        self.commit_count = 0         # commits that actually moved the branch
        self.commit_messages = []
        # [{message, paths, written}] per landed commit. `paths` is what the
        # commit CHANGED; `written` is what it explicitly wrote. The two differ
        # when a write re-states a file whose content is unchanged, which is
        # routine for data/identity.json — a merge that re-sends the same
        # fingerprint rewrites the same bytes. "Four files in one commit" is a
        # claim about `written`, so the mock has to record it separately or the
        # assertion silently weakens into "four files, unless one didn't move".
        self.commit_log = []
        self.tree_written = {}        # tree sha -> [paths the POST named]
        self.errors = []              # protocol violations noticed by the mock


MOCK = MockState()
REPO_PREFIX = "/repos/%s" % MOCK_REPO


def _build_trees(files):
    """Materialise {path: text} into blobs + nested trees. Returns the root sha."""
    def build(prefix):
        entries = {}
        subdirs = set()
        for path in files:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if "/" in rest:
                subdirs.add(rest.split("/", 1)[0])
            else:
                sha = _sha(files[path])
                MOCK.blobs[sha] = files[path]
                entries[rest] = ("blob", sha)
        for d in sorted(subdirs):
            entries[d] = ("tree", build(prefix + d + "/"))
        key = json.dumps(sorted((n, t, s) for n, (t, s) in entries.items()))
        sha = _sha("tree\x00" + key)
        MOCK.trees[sha] = entries
        return sha
    root = build("")
    MOCK.tree_files[root] = dict(files)
    return root


def _land(files, message, paths):
    """Commit `files` onto the branch tip. Used by reset and by the mock's own
    simulated third-party commit; the worker's own writes go through the API."""
    root = _build_trees(files)
    sha = _sha("commit\x00%s\x00%s\x00%s" % (root, MOCK.head, message))
    MOCK.commits[sha] = {"tree": root, "parents": [MOCK.head] if MOCK.head else [],
                         "message": message}
    MOCK.head = sha
    MOCK.files = dict(files)
    MOCK.commit_count += 1
    MOCK.commit_messages.append(message)
    MOCK.commit_log.append({"message": message, "paths": sorted(paths),
                            "written": sorted(paths)})
    return sha


def _reset_repo():
    """A repository with the dataset present and empty — what the site looks
    like before its first submission, plus a file the data path never touches
    so `base_tree` carry-over is actually observable.

    🔴 data/identity.json is deliberately NOT seeded. That is the true state of
    the repository at the M14.1 commit — nothing has been fingerprinted yet, so
    the file does not exist — and it makes "no identity file present" the path
    the FIRST submission of every stage takes, rather than a special case some
    test has to remember to construct. Every submission after the first then
    exercises the read-back path."""
    MOCK.files = {}
    MOCK.blobs = {}
    MOCK.trees = {}
    MOCK.tree_files = {}
    MOCK.tree_written = {}
    MOCK.commits = {}
    MOCK.head = None
    MOCK.commit_count = 0
    MOCK.commit_messages = []
    MOCK.commit_log = []
    files = {
        "README.md": "# mock repo\n",
        "index.html": "<!doctype html>\n",
        INDEX_PATH: json.dumps(EMPTY_INDEX, indent=2) + "\n",
        FLEET_PATH: json.dumps(EMPTY_FLEET, indent=2) + "\n",
    }
    _land(files, "seed", sorted(files))
    # the seed is scaffolding, not a submission the assertions should see
    MOCK.commit_count = 0
    MOCK.commit_messages = []
    MOCK.commit_log = []


FOREIGN_ID = "sub-20260101000000-beef"


def _land_foreign_commit():
    """Somebody else's submission landing between our read and our ref update.

    A real row, in a real commit, on the branch. The retry has to re-read and
    re-resolve against it: a retry that reused its first read would write an
    index without this row, and the cross-file validator would then see a fleet
    series counting a submission the index does not list."""
    index = json.loads(MOCK.files[INDEX_PATH])
    fleet = json.loads(MOCK.files[FLEET_PATH])
    daily = [{"date": "2026-02-01", "requests": 40, "losses": 2, "wasted_tokens": 900}]
    totals = {"requests": 40, "in_ttl_losses": 2, "iron_losses": 1,
              "wasted_tokens": 900}
    index["submissions"].append({
        "id": FOREIGN_ID, "submitted_at": "2026-01-01", "nickname": "f***",
        "plan": "pro", "client": "cli", "concurrent_sessions": "single",
        "period_start": "2026-02-01", "period_end": "2026-02-01",
        "totals": totals, "daily_days": 1,
        "detail": "data/subs/%s.json" % FOREIGN_ID, "script_version": "web-1.0",
    })
    by_date = {d["date"]: d for d in fleet["days"]}
    for row in daily:
        slot = by_date.setdefault(row["date"], {"date": row["date"], "requests": 0,
                                                "losses": 0, "wasted_tokens": 0,
                                                "machines": 0})
        slot["requests"] += row["requests"]
        slot["losses"] += row["losses"]
        slot["wasted_tokens"] += row["wasted_tokens"]
        slot["machines"] += 1
    fleet["days"] = [by_date[k] for k in sorted(by_date)]
    detail = {"schema_version": 1, "id": FOREIGN_ID, "period_start": "2026-02-01",
              "period_end": "2026-02-01", "totals": totals, "daily": daily}
    files = dict(MOCK.files)
    files[INDEX_PATH] = json.dumps(index, indent=2) + "\n"
    files[FLEET_PATH] = json.dumps(fleet, indent=2) + "\n"
    files["data/subs/%s.json" % FOREIGN_ID] = json.dumps(detail, indent=2) + "\n"
    _land(files, "data: submission %s — f***, someone else's commit" % FOREIGN_ID,
          [INDEX_PATH, FLEET_PATH, "data/subs/%s.json" % FOREIGN_ID])


def _tree_role(sha):
    """Which of the three tree listings the read path walks this sha is, so a
    test can fail or truncate one of them by NAME instead of by guessing a hash.
    Called with MOCK.lock held."""
    if MOCK.head is None:
        return None
    root = MOCK.commits[MOCK.head]["tree"]
    if sha == root:
        return "root"
    data = (MOCK.trees.get(root) or {}).get("data")
    if not data or data[0] != "tree":
        return None
    if sha == data[1]:
        return "data"
    subs = (MOCK.trees.get(data[1]) or {}).get("subs")
    if subs and subs[0] == "tree" and sha == subs[1]:
        return "subs"
    return None


EMPTY_DAILY = object()


def corrupt_detail_row(sub_id, shape):
    """Replace the FIRST daily row of a landed detail file with `shape` (or
    empty the array, for EMPTY_DAILY) and re-land the branch tip, so the tree
    the worker reads really carries it.

    Only a repository admin can do this. The question the tests around it ask is
    not whether the edit is possible but what the API does with it: a row whose
    date is unreadable cannot be taken back OUT of the fleet series, so a write
    that proceeds publishes a dataset that no longer adds up."""
    path = "data/subs/%s.json" % sub_id
    with MOCK.lock:
        detail = json.loads(MOCK.files[path])
        if shape is EMPTY_DAILY:
            detail["daily"] = []
        else:
            detail["daily"][0] = shape
        files = dict(MOCK.files)
        files[path] = json.dumps(detail, indent=2) + "\n"
        _land(files, "admin: hand-edit a detail row", [path])


def land_fleet_days(days, why):
    """Hand-edit data/daily.json and re-land the tip. Same premise: a repo admin
    can, and the delta the write path applies has to survive it."""
    with MOCK.lock:
        fleet = json.loads(MOCK.files[FLEET_PATH])
        fleet["days"] = days
        files = dict(MOCK.files)
        files[FLEET_PATH] = json.dumps(fleet, indent=2) + "\n"
        _land(files, "admin: " + why, [FLEET_PATH])


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence
        pass

    def _delay(self):
        """Emulated network latency, taken BEFORE the state lock so simultaneous
        submissions overlap instead of queueing behind each other."""
        ms = MOCK.latency_ms
        if ms:
            time.sleep(ms / 1000.0)

    def _send(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_common(self):
        MOCK.requests += 1
        auth = self.headers.get("Authorization", "")
        if auth != "Bearer " + MOCK_TOKEN:
            MOCK.errors.append("bad Authorization header: %r" % auth)
        if not self.headers.get("User-Agent"):
            MOCK.errors.append("missing User-Agent")

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None

    def _rel(self):
        if not self.path.startswith(REPO_PREFIX + "/"):
            return None
        return self.path[len(REPO_PREFIX):]

    def do_GET(self):
        self._delay()
        with MOCK.lock:
            self._check_common()
            rel = self._rel()
            if rel is None:
                self._send(404, {"message": "Not Found"})
                return
            if rel == "/git/ref/heads/" + MOCK_BRANCH:
                MOCK.ref_gets += 1
                if MOCK.head is None:
                    self._send(404, {"message": "Not Found"})
                    return
                self._send(200, {"ref": "refs/heads/" + MOCK_BRANCH,
                                 "object": {"type": "commit", "sha": MOCK.head}})
                return
            if rel.startswith("/git/commits/"):
                sha = rel[len("/git/commits/"):]
                c = MOCK.commits.get(sha)
                if not c:
                    self._send(404, {"message": "Not Found"})
                    return
                self._send(200, {"sha": sha, "message": c["message"],
                                 "tree": {"sha": c["tree"]}})
                return
            if rel.startswith("/git/trees/"):
                sha = rel[len("/git/trees/"):].split("?")[0]
                t = MOCK.trees.get(sha)
                if t is None:
                    self._send(404, {"message": "Not Found"})
                    return
                role = _tree_role(sha)
                if MOCK.fail_tree is not None and role == MOCK.fail_tree:
                    self._send(500, {"message": "boom"})
                    return
                # 🔴 GitHub truncates a tree listing at 100,000 entries and says
                # so in this flag. The entries themselves are still returned —
                # just NOT all of them — which is why a client that ignores the
                # flag does not see an error, it sees a file that is not there.
                # The mock reproduces exactly that: a complete-looking listing
                # with the flag set.
                truncated = (MOCK.truncate_tree is not None and
                             role == MOCK.truncate_tree)
                self._send(200, {"sha": sha, "truncated": truncated, "tree": [
                    {"path": name, "type": typ, "sha": s,
                     "mode": "040000" if typ == "tree" else "100644"}
                    for name, (typ, s) in sorted(t.items())]})
                return
            if rel.startswith("/git/blobs/"):
                sha = rel[len("/git/blobs/"):]
                text = MOCK.blobs.get(sha)
                if text is None:
                    self._send(404, {"message": "Not Found"})
                    return
                raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
                self._send(200, {
                    "sha": sha, "encoding": "base64", "size": len(text),
                    # GitHub wraps blob content at 60 columns; a client that
                    # forgot to strip the newlines would decode to garbage.
                    "content": "\n".join(raw[i:i + 60]
                                         for i in range(0, len(raw), 60)) + "\n",
                })
                return
            self._send(404, {"message": "Not Found"})

    def do_POST(self):
        self._delay()
        with MOCK.lock:
            self._check_common()
            rel = self._rel()
            body = self._body()
            if rel is None or body is None:
                self._send(400, {"message": "bad request"})
                return
            if rel == "/git/blobs":
                MOCK.blob_posts += 1
                if MOCK.mode == "server_error":
                    self._send(500, {"message": "boom"})
                    return
                if body.get("encoding") != "base64":
                    MOCK.errors.append("blob POST encoding %r" % body.get("encoding"))
                try:
                    text = base64.b64decode(body["content"]).decode("utf-8")
                except Exception as exc:
                    MOCK.errors.append("blob content undecodable: %r" % exc)
                    self._send(400, {"message": "bad content"})
                    return
                sha = _sha(text)
                MOCK.blobs[sha] = text
                self._send(201, {"sha": sha})
                return
            if rel == "/git/trees":
                base = body.get("base_tree")
                if base not in MOCK.tree_files:
                    MOCK.errors.append("tree POST with unknown base_tree %r" % base)
                    self._send(422, {"message": "bad base_tree"})
                    return
                files = dict(MOCK.tree_files[base])
                for e in body.get("tree", []):
                    if e.get("mode") != "100644" or e.get("type") != "blob":
                        MOCK.errors.append("tree entry %r" % e)
                    text = MOCK.blobs.get(e.get("sha"))
                    if text is None:
                        self._send(422, {"message": "unknown blob"})
                        return
                    files[e["path"]] = text
                sha = _build_trees(files)
                MOCK.tree_written[sha] = sorted(e["path"] for e in body.get("tree", [])
                                                if isinstance(e.get("path"), str))
                self._send(201, {"sha": sha})
                return
            if rel == "/git/commits":
                if MOCK.fail_commit_post:
                    self._send(500, {"message": "boom"})
                    return
                tree = body.get("tree")
                if tree not in MOCK.tree_files:
                    self._send(422, {"message": "unknown tree"})
                    return
                if not isinstance(body.get("message"), str) or not body["message"]:
                    MOCK.errors.append("commit without a message")
                parents = body.get("parents") or []
                sha = _sha("commit\x00%s\x00%s\x00%s"
                           % (tree, parents, body.get("message")))
                MOCK.commits[sha] = {"tree": tree, "parents": parents,
                                     "message": body["message"]}
                self._send(201, {
                    "sha": sha,
                    "html_url": "https://example.invalid/commit/" + sha[:12],
                })
                return
            self._send(404, {"message": "Not Found"})

    def do_PATCH(self):
        self._delay()
        with MOCK.lock:
            self._check_common()
            rel = self._rel()
            body = self._body()
            if rel != "/git/refs/heads/" + MOCK_BRANCH or body is None:
                self._send(404, {"message": "Not Found"})
                return
            MOCK.ref_patches += 1
            if MOCK.mode == "conflict_once" and not MOCK.conflict_fired:
                MOCK.conflict_fired = True
                _land_foreign_commit()   # the branch really moves under the caller
            if body.get("force") is True:
                MOCK.errors.append("ref update asked for force")
            # A branch that is permanently contended: every update is refused as
            # a non-fast-forward, which is what the losers of a large burst see
            # until their budget runs out. Nothing lands, so the files stay
            # exactly as they were.
            if MOCK.mode == "conflict_always":
                self._send(MOCK.conflict_status,
                           {"message": MOCK.conflict_message})
                return
            sha = body.get("sha")
            commit = MOCK.commits.get(sha)
            if commit is None:
                self._send(422, {"message": "unknown commit"})
                return
            # fast-forward only: the parent must still be the branch tip
            if (commit["parents"] or [None])[0] != MOCK.head:
                self._send(MOCK.conflict_status,
                           {"message": "Update is not a fast forward"})
                return
            paths = [p for p, text in MOCK.tree_files[commit["tree"]].items()
                     if MOCK.files.get(p) != text]
            MOCK.head = sha
            MOCK.files = dict(MOCK.tree_files[commit["tree"]])
            MOCK.commit_count += 1
            MOCK.commit_messages.append(commit["message"])
            MOCK.commit_log.append({"message": commit["message"],
                                    "paths": sorted(paths),
                                    "written": MOCK.tree_written.get(commit["tree"], [])})
            self._send(200, {"ref": "refs/heads/" + MOCK_BRANCH,
                             "object": {"sha": sha}})


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # keep-alive resets from workerd are routine — never worth a traceback
        pass


def start_mock():
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# ---------------------------------------------------------------------------
# wrangler pages dev
# ---------------------------------------------------------------------------

def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_wrangler(npx, mock_port, scratch):
    """mock_port=None boots without the mock bindings (real-API mode:
    GITHUB_TOKEN/GITHUB_REPO must then come from site/.dev.vars)."""
    persist = os.path.join(scratch, "wrangler-state")
    log_path = os.path.join(scratch, "wrangler.log")
    args = [
        npx, "-y", WRANGLER_SPEC, "pages", "dev", ".",
        "--port", str(PORT),
        "--kv", "RATE_LIMIT",
        "--persist-to", persist,
        "--compatibility-date=2026-08-01",
    ]
    if mock_port is not None:
        args += [
            "--binding", "GITHUB_TOKEN=" + MOCK_TOKEN,
            "--binding", "GITHUB_REPO=" + MOCK_REPO,
            "--binding", "GITHUB_BRANCH=" + MOCK_BRANCH,
            "--binding", "GITHUB_API_BASE=http://127.0.0.1:%d" % mock_port,
        ]
    env = dict(os.environ)
    env["CI"] = "true"
    env["WRANGLER_SEND_METRICS"] = "false"
    env["NO_COLOR"] = "1"
    log = open(log_path, "wb")
    proc = subprocess.Popen(args, cwd=SITE_DIR, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env)
    return proc, log, log_path


def stop_wrangler(proc, log):
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=60)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        log.close()


def http_json(method, url, payload, headers=None, timeout=30):
    data = None
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    if payload is not None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read()
            status = res.status
    except urllib.error.HTTPError as err:
        body = err.read()
        status = err.code
    try:
        parsed = json.loads(body.decode("utf-8"))
    except ValueError:
        parsed = None
    return status, parsed


def wait_ready(proc, log_path):
    deadline = time.monotonic() + BOOT_DEADLINE_SECONDS
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail_log(log_path)
            raise SetupFail("wrangler exited early (code %s)" % proc.returncode)
        try:
            status, _ = http_json("POST", BASE + "/api/submit", b"not json", timeout=5)
            last = status
            if status == 400:
                return
            if status in (200, 429, 502):
                raise ContractFail("readiness probe (invalid JSON) got %d, want 400" % status)
            if status >= 500:
                raise ContractFail("function crashed on readiness probe (HTTP %d)" % status)
        except (urllib.error.URLError, OSError, ConnectionError):
            pass
        time.sleep(1.5)
    tail_log(log_path)
    raise SetupFail("wrangler not ready in %ds (last status: %r)" % (BOOT_DEADLINE_SECONDS, last))


def tail_log(log_path):
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for line in lines[-40:]:
            print("  wrangler| " + line.rstrip())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# contract cases
# ---------------------------------------------------------------------------

def valid_payload(**over):
    p = {
        "nickname": "contract-test",
        "plan": "pro",
        "client": "cli",
        "concurrent_sessions": "single",
        "period_start": "2026-08-01",
        "period_end": "2026-08-15",
        "totals": {"requests": 1000, "in_ttl_losses": 10,
                   "iron_losses": 5, "wasted_tokens": 12345},
        "daily": [
            {"date": "2026-08-01", "requests": 500, "losses": 4, "wasted_tokens": 6000},
            {"date": "2026-08-15", "requests": 500, "losses": 6, "wasted_tokens": 6345},
        ],
        "script_version": "web-1.0",
    }
    p.update(over)
    return p


def check(cond, msg):
    if not cond:
        raise ContractFail(msg)


def expect_schema_400(name, status, data):
    check(status == 400, "%s: status %s, want 400" % (name, status))
    check(isinstance(data, dict) and data.get("ok") is False and
          data.get("error") == "schema",
          "%s: body %r, want {ok:false, error:'schema', ...}" % (name, data))
    check("detail" in data, "%s: 400 body carries no detail" % name)


# ---------------------------------------------------------------------------
# reading the mock repository (M14: three kinds of file, one dataset)
# ---------------------------------------------------------------------------

def repo_files():
    with MOCK.lock:
        return dict(MOCK.files)


def _parse(files, path):
    """A public data file the pipeline wrote. Unparsable is a contract failure
    with a name, not a traceback: whatever is at that path is served to readers
    as JSON, so "it is not JSON" is a finding, not an accident of the test."""
    text = files.get(path)
    if text is None:
        raise ContractFail("%s is missing from the repository" % path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ContractFail("%s is not valid JSON (%s): %r" % (path, exc, text[:120]))


def index_doc(files=None):
    return _parse(files or repo_files(), INDEX_PATH)


def fleet_doc(files=None):
    return _parse(files or repo_files(), FLEET_PATH)


def identity_doc(files=None):
    """None when the file is not there, which is a valid dataset and the state
    every stage starts in — not a failure to be papered over with an empty
    document."""
    files = files or repo_files()
    return None if IDENTITY_PATH not in files else _parse(files, IDENTITY_PATH)


def identity_entries(files=None):
    doc = identity_doc(files)
    return {} if doc is None else doc.get("identities", {})


def identity_of(sub_id, files=None):
    return identity_entries(files).get(sub_id)


def detail_docs(files=None):
    files = files or repo_files()
    out = {}
    for path in files:
        if path.startswith("data/subs/") and path.endswith(".json"):
            out[path[len("data/subs/"):-len(".json")]] = _parse(files, path)
    return out


def detail_doc(sub_id, files=None):
    return detail_docs(files).get(sub_id)


def leak_blob():
    """Every byte this pipeline made public: all three kinds of data file plus
    every commit message. A raw value that leaked into any of them is a leak."""
    files = repo_files()
    with MOCK.lock:
        messages = list(MOCK.commit_messages)
    return "\n".join(sorted(files.values())) + "\n" + "\n".join(messages)


def check_file_arithmetic(tag):
    """🔴 After every path: the public dataset still adds up, checked by the
    SAME validator that runs against the committed files in data/. Nothing here
    reimplements the rule; a rule the live write path is not checked against is
    a rule that drifts."""
    files = repo_files()
    try:
        errors = dataset_validate.validate(index_doc(files), fleet_doc(files),
                                           detail_docs(files),
                                           identity_doc(files))
    except ContractFail:
        raise
    except Exception as exc:                      # a shape the validator cannot walk
        raise ContractFail("%s: the dataset could not even be validated: %r"
                           % (tag, exc))
    check(not errors, "%s: the dataset no longer adds up:\n  %s"
          % (tag, "\n  ".join(errors)))
    return index_doc(files)


def run_cases(xff_ip):
    submit_url = BASE + "/api/submit"
    headers = {"X-Forwarded-For": xff_ip}

    def post(payload):
        return http_json("POST", submit_url, payload, headers=headers)

    # -- case2: undefined field => 400 (reject, not drop) --------------------
    status, data = post(valid_payload(extra_field=1))
    expect_schema_400("case2", status, data)
    check(any("extra_field" in str(d) for d in data["detail"]),
          "case2: detail does not name the undefined field: %r" % (data["detail"],))
    print("PASS case2 undefined field -> 400 schema")

    # -- case3: losses > requests => 400 -------------------------------------
    bad = valid_payload()
    bad["totals"] = dict(bad["totals"], in_ttl_losses=bad["totals"]["requests"] + 1)
    status, data = post(bad)
    expect_schema_400("case3", status, data)
    print("PASS case3 losses>requests -> 400 schema")

    # -- case4: period spans 93 days => 400 ----------------------------------
    bad = valid_payload(
        period_start="2026-01-01", period_end="2026-04-03",  # 93 days inclusive
        daily=[{"date": "2026-01-01", "requests": 500, "losses": 4, "wasted_tokens": 6000},
               {"date": "2026-01-02", "requests": 500, "losses": 6, "wasted_tokens": 6345}])
    status, data = post(bad)
    expect_schema_400("case4", status, data)
    print("PASS case4 period 93 days -> 400 schema")

    # -- case5: nickname 21 chars => 400 -------------------------------------
    status, data = post(valid_payload(nickname="a" * 21))
    expect_schema_400("case5", status, data)
    print("PASS case5 nickname 21 chars -> 400 schema")

    # -- case9: daily [] => 400 ----------------------------------------------
    # 9a: inflated totals with no daily backing them.
    status, data = post(valid_payload(daily=[]))
    expect_schema_400("case9a", status, data)
    check(any("daily" in str(d) for d in data["detail"]),
          "case9a: detail does not mention daily: %r" % (data["detail"],))
    # 9b: all-zero totals — sums match (0=0), only the min-entries rule fires.
    status, data = post(valid_payload(
        daily=[],
        totals={"requests": 0, "in_ttl_losses": 0,
                "iron_losses": 0, "wasted_tokens": 0}))
    expect_schema_400("case9b", status, data)
    check(any("at least 1" in str(d) for d in data["detail"]),
          "case9b: detail misses the min-entries rule: %r" % (data["detail"],))
    print("PASS case9 daily [] -> 400 schema (inflated + all-zero totals)")

    # -- case10: daily sums != totals => 400 (each equality separately) ------
    for field, total_key in (("requests", "totals.requests"),
                             ("losses", "totals.in_ttl_losses"),
                             ("wasted_tokens", "totals.wasted_tokens")):
        bad = valid_payload()
        bad["daily"] = [dict(bad["daily"][0]), dict(bad["daily"][1])]
        bad["daily"][0][field] += 1
        status, data = post(bad)
        expect_schema_400("case10 " + field, status, data)
        check(any(total_key in str(d) for d in data["detail"]),
              "case10 %s: detail does not name %s: %r"
              % (field, total_key, data["detail"]))
    print("PASS case10 daily sums != totals -> 400 schema (3 fields)")

    # (none of the 400 cases may have reached storage or the rate limiter)
    with MOCK.lock:
        check(MOCK.commit_count == 0, "400 cases must not reach storage "
                                      "(%d commits landed)" % MOCK.commit_count)

    # -- case1 + case20: valid submission => 200, ONE commit, FOUR files ------
    # 🔴 case23 rides along here: the repository at this moment has NO
    # data/identity.json (see _reset_repo), so this is the first-submission path
    # over an absent identity file — the state the live repo is in at the M14.1
    # commit. It has to create the file, not fail and not skip it.
    check(IDENTITY_PATH not in repo_files(),
          "case23 setup: the repository already has %s, so the absent-file path "
          "is not the one being tested" % IDENTITY_PATH)
    payload = valid_payload()
    status, data = post(payload)
    check(status == 200, "case1: status %s, want 200 (body %r)" % (status, data))
    check(isinstance(data, dict) and data.get("ok") is True,
          "case1: body %r, want ok:true" % (data,))
    sub_id = data.get("id", "")
    check(re.fullmatch(r"sub-\d{14}-[0-9a-f]{4}", sub_id),
          "case1: id %r does not match sub-{timestamp14}-{hex4}" % sub_id)
    check(str(data.get("commit_url", "")).startswith("https://example.invalid/commit/"),
          "case1: commit_url %r not the mock's html_url" % data.get("commit_url"))
    files = repo_files()
    with MOCK.lock:
        check(MOCK.commit_count == 1,
              "case1: %d commits, want 1" % MOCK.commit_count)
        landed = MOCK.commit_log[-1]
    detail_rel = "data/subs/%s.json" % sub_id
    want_files = sorted([INDEX_PATH, IDENTITY_PATH, FLEET_PATH, detail_rel])
    check(landed["written"] == want_files,
          "🔴 case20: one submission must land the index, the identity map, the "
          "fleet series and its own detail file in ONE commit — this commit "
          "wrote %r, want %r" % (landed["written"], want_files))
    check(landed["paths"] == want_files,
          "case20: the commit changed %r, want all four (%r)"
          % (landed["paths"], want_files))
    check("README.md" in files and files["README.md"] == "# mock repo\n",
          "case20: the commit disturbed a file outside data/")
    check(IDENTITY_PATH in files,
          "🔴 case23: the first submission over a repository with no %s did not "
          "create it — the identity was resolved against nothing and written "
          "nowhere" % IDENTITY_PATH)
    doc = index_doc(files)
    subs = doc.get("submissions", [])
    check(doc.get("schema_version") == 2, "case1: index schema_version %r, want 2"
          % doc.get("schema_version"))
    check(len(subs) == 1, "case1: %d submissions stored, want 1" % len(subs))
    stored = subs[0]
    check(stored.get("id") == sub_id, "case1: stored id mismatch")
    check(re.fullmatch(r"\d{4}-\d{2}-\d{2}", stored.get("submitted_at", "")),
          "case1: submitted_at %r not truncated to a day" % stored.get("submitted_at"))
    for key in ("plan", "client", "concurrent_sessions",
                "period_start", "period_end", "script_version"):
        check(stored.get(key) == payload[key],
              "case1: stored %s=%r != %r" % (key, stored.get(key), payload[key]))
    # The nickname is stored masked (M11): first code point + a fixed
    # three-asterisk mask, so neither the string nor its length survives.
    check(stored.get("nickname") == "c***",
          "case1: nickname %r, want the masked 'c***'" % stored.get("nickname"))
    check(stored.get("totals") == payload["totals"], "case1: totals mismatch")
    # M14: the daily rows are in the detail file, and the index row says how
    # many of them there are and where they live.
    check("daily" not in stored,
          "case1: the index row still carries the daily array")
    check(stored.get("daily_days") == len(payload["daily"]),
          "case1: daily_days %r, want %d" % (stored.get("daily_days"),
                                             len(payload["daily"])))
    check(stored.get("detail") == detail_rel,
          "case1: detail %r, want %r" % (stored.get("detail"), detail_rel))
    detail = detail_doc(sub_id, files)
    check(detail is not None, "case1: no detail file at %s" % detail_rel)
    check(detail.get("daily") == payload["daily"], "case1: detail daily mismatch")
    check(detail.get("totals") == payload["totals"], "case1: detail totals mismatch")
    # M14.1: `identity` is no longer one of them — it lives in its own file.
    # The index row is now exactly the fields a reader uses, and `updated_at`
    # appears only once a row has actually been merged into.
    extra = set(stored) - {"id", "submitted_at", "nickname", "plan", "client",
                           "concurrent_sessions", "period_start", "period_end",
                           "totals", "daily_days", "detail", "script_version"}
    check(not extra, "case1: undefined fields stored: %r" % extra)
    check("identity" not in stored,
          "🔴 case1: the index row still carries the identity block — those "
          "digests are ~70%% of a fingerprinted row and every visitor downloads "
          "the index")
    check("updated_at" not in stored,
          "case1: a first submission is marked as updated")
    # This payload carries no anchors, so the API falls back to layer 2 and
    # issues a token. Only its hash may be stored, and now only in data/identity.json.
    check(re.fullmatch(r"[0-9a-f]{32}", data.get("token", "")),
          "case1: no link token issued to a submission with no anchors (%r)"
          % data.get("token"))
    check(data.get("merged") is False, "case1: a first submission reported a merge")
    ident_doc = identity_doc(files)
    check(ident_doc is not None and ident_doc.get("schema_version") == 1,
          "case1: %s schema_version %r, want 1"
          % (IDENTITY_PATH, (ident_doc or {}).get("schema_version")))
    entry = identity_of(sub_id, files)
    check(entry is not None,
          "case1: no identity entry for %s in %s" % (sub_id, IDENTITY_PATH))
    check(entry.get("token_hash") ==
          hashlib.sha256(("cco.token.v1|" + data["token"]).encode("utf-8")).hexdigest(),
          "case1: identity token_hash is not the hash of the issued token")
    check("anchor_hashes" not in entry,
          "case1: an anchorless submission invented anchors: %r" % entry)
    check(list(identity_entries(files)) == [sub_id],
          "case1: %s holds entries for %r, want only the one row that exists"
          % (IDENTITY_PATH, list(identity_entries(files))))
    check(data["token"] not in leak_blob(),
          "case1: the token itself was written into the public dataset")
    want_msg = ("data: submission %s — c***, 2026-08-01~2026-08-15, "
                "10 losses / 1000 req" % sub_id)
    with MOCK.lock:
        check(MOCK.commit_messages[-1] == want_msg,
              "case1: commit message %r != %r" % (MOCK.commit_messages[-1], want_msg))
        check("contract-test" not in MOCK.commit_messages[-1],
              "case1: raw nickname leaked into the commit message")
    check_file_arithmetic("case1")
    print("PASS case1 valid submit -> 200, index + detail + commit message verified")
    print("PASS case20 one submission = one commit carrying %s"
          % ", ".join(landed["written"]))
    print("PASS case23 first submission with no %s present -> file created"
          % IDENTITY_PATH)

    # -- case7: the ref moves under us => re-read + single retry => 200 ------
    # The mock lands a REAL third-party commit at the moment of the first ref
    # update. A retry that reused its first read would write an index without
    # that row, and check_file_arithmetic would then find a fleet series
    # counting a submission the index does not list.
    with MOCK.lock:
        MOCK.mode = "conflict_once"
        MOCK.conflict_fired = False
        commits_before = MOCK.commit_count
        patches_before = MOCK.ref_patches
        refgets_before = MOCK.ref_gets
    status, data = post(valid_payload(nickname="<b>x</b>"))
    check(status == 200, "case7: status %s, want 200 after retry (body %r)" % (status, data))
    with MOCK.lock:
        MOCK.mode = "normal"
        patch_delta = MOCK.ref_patches - patches_before
        refget_delta = MOCK.ref_gets - refgets_before
        commit_delta = MOCK.commit_count - commits_before
    check(patch_delta == 2,
          "case7: %d ref updates, want 2 (422 then success)" % patch_delta)
    check(refget_delta == 2,
          "case7: %d ref reads, want 2 (the retry must re-read HEAD)" % refget_delta)
    check(commit_delta == 2,
          "case7: %d commits landed, want 2 (the third party's and ours)"
          % commit_delta)
    doc = check_file_arithmetic("case7")
    subs = doc.get("submissions", [])
    ids = [s["id"] for s in subs]
    check(len(subs) == 3, "case7: %d submissions stored, want 3" % len(subs))
    check(FOREIGN_ID in ids,
          "🔴 case7: the retry overwrote the commit that landed under it — the "
          "third party's row is gone, so the merge used a stale read (%r)" % ids)
    # Masking runs before escaping, so the escape has to survive the slice:
    # "<b>x</b>" keeps "<" as its one visible code point and stores it as
    # the entity. A raw "<" here would be an injection into the public JSON.
    check(subs[-1].get("nickname") == "&lt;***",
          "case7: nickname not masked+escaped: %r" % subs[-1].get("nickname"))
    # 🔴 case24: the third party's row carries no identity at all, which is the
    # legitimate "explicitly none" state — the same one the row committed in
    # data/ is in. The retry rebuilt data/identity.json against the fresh read,
    # so it must have kept the entries for the rows that have one and invented
    # nothing for the row that does not. check_file_arithmetic above would have
    # rejected an entry keyed by a row the index does not list.
    check(identity_of(FOREIGN_ID) is None,
          "🔴 case24: an identity entry was invented for a row that has no "
          "fingerprint (%r)" % identity_of(FOREIGN_ID))
    check(sorted(identity_entries()) == sorted(i for i in ids if i != FOREIGN_ID),
          "case24: %s keys %r, want an entry for every row except the "
          "fingerprintless one" % (IDENTITY_PATH, sorted(identity_entries())))
    print("PASS case7 ref moved -> one re-read + retry -> 200, the third party's "
          "row survives (rows now %d)" % len(subs))
    print("PASS case24 an index row with no identity entry is valid, and the "
          "ref-moved retry re-resolved the identity map against the fresh read")

    # -- case8: GitHub 5xx => 502 storage, no retry --------------------------
    with MOCK.lock:
        MOCK.mode = "server_error"
        commits_before = MOCK.commit_count
        blobs_before = MOCK.blob_posts
        files_before = dict(MOCK.files)
    status, data = post(valid_payload())
    with MOCK.lock:
        MOCK.mode = "normal"
        commit_delta = MOCK.commit_count - commits_before
        blob_delta = MOCK.blob_posts - blobs_before
        files_after = dict(MOCK.files)
    check(status == 502, "case8: status %s, want 502 (body %r)" % (status, data))
    check(isinstance(data, dict) and data.get("ok") is False and
          data.get("error") == "storage",
          "case8: body %r, want {ok:false, error:'storage'}" % (data,))
    check(commit_delta == 0, "case8: %d commits landed on a 5xx" % commit_delta)
    check(files_after == files_before, "case8: a 5xx still changed the files")
    # M14.2 posts the four blobs as ONE batch instead of four sequential calls,
    # so the count that proves "a 5xx earns no retry" is now 4 rather than 1: a
    # retry would send a second batch and make it 8. The property is unchanged
    # and the number is stronger, because it also pins the batching.
    check(blob_delta == 4,
          "case8: %d blob writes attempted, want 4 (one batch, and a 5xx earns "
          "no retry — a retry would make it 8)" % blob_delta)
    print("PASS case8 storage 5xx -> 502, nothing committed, no retry")

    # -- case6: 4th rate-limited submission from the same IP => 429 ----------
    # Ledger so far for this IP hash+hour: case1 + case7 + case8 = 3 counted.
    with MOCK.lock:
        commits_before = MOCK.commit_count
    status, data = post(valid_payload())
    check(status == 429, "case6: status %s, want 429 on the 4th submit (body %r)"
          % (status, data))
    check(isinstance(data, dict) and data.get("ok") is False and
          data.get("error") == "rate_limited",
          "case6: body %r, want {ok:false, error:'rate_limited', ...}" % (data,))
    check(isinstance(data.get("retry_after"), int) and 0 < data["retry_after"] <= 3600,
          "case6: retry_after %r not a sane second count" % data.get("retry_after"))
    with MOCK.lock:
        check(MOCK.commit_count == commits_before,
              "case6: rate-limited request still reached storage")
    print("PASS case6 4th same-IP submit -> 429 retry_after=%ds" % data["retry_after"])

    with MOCK.lock:
        check(not MOCK.errors, "mock observed protocol violations: %r" % MOCK.errors)


# ---------------------------------------------------------------------------
# case11: nickname masking (M11)
# ---------------------------------------------------------------------------

NO_NICKNAME = object()

# (what is submitted, what must be stored, why this input is on the list)
MASK_CASES = [
    ("a", "***", "one code point keeps nothing: keeping it would keep the whole value"),
    ("ab", "a***", "two code points keep only the first"),
    ("가나다라", "가***", "hangul is masked by code point"),
    ("\U0001F41B\U0001F41Ex", "\U0001F41B***", "an astral code point survives whole"),
    ("  padded  ", "p***", "trimmed before masking"),
    ("g***", "g***", "masking an already masked value is a no-op"),
    ("<b>x</b>", "&lt;***", "the masked value is HTML-escaped after masking"),
    (NO_NICKNAME, "anonymous", "an absent nickname stays anonymous"),
    ("   ", "anonymous", "a whitespace-only nickname is anonymous"),
]

# Raw values distinctive enough that finding them anywhere is proof of a leak.
MASK_LEAK_PROBES = ["contract-test", "가나다라",
                    "\U0001F41B\U0001F41Ex", "padded"]


def run_mask_cases():
    """Every case is a real accepted submission, so they are spread across
    several IP hashes: 3 per hour per IP is the contract's own limit and the
    test must not trip it on itself."""
    submit_url = BASE + "/api/submit"
    for i, (raw, want, why) in enumerate(MASK_CASES):
        payload = valid_payload()
        if raw is NO_NICKNAME:
            payload.pop("nickname")
        else:
            payload["nickname"] = raw
        status, data = http_json(
            "POST", submit_url, payload,
            headers={"X-Forwarded-For": "198.51.100.%d" % (10 + i // 3)})
        check(status == 200,
              "case11 (%s): status %s, want 200 (body %r)" % (why, status, data))
        stored = index_doc()["submissions"][-1].get("nickname")
        with MOCK.lock:
            msg = MOCK.commit_messages[-1]
        check(stored == want,
              "case11 (%s): stored %r, want %r" % (why, stored, want))
        check(("— %s," % want) in msg,
              "case11 (%s): commit message %r does not carry the masked value"
              % (why, msg))

    # A per-case check only looks at the last record. Scan EVERY public file the
    # pipeline wrote — index, fleet series and every detail file — plus every
    # commit message the mock ever received: a raw value that leaked into an
    # earlier field, or into a file the assertions above never open, would
    # otherwise pass unnoticed.
    blob = leak_blob()
    for probe in MASK_LEAK_PROBES:
        check(probe not in blob,
              "case11: raw nickname %r survives in the stored data or a commit "
              "message" % probe)
    with MOCK.lock:
        check(not MOCK.errors, "mock observed protocol violations: %r" % MOCK.errors)
    print("PASS case11 nickname masking (%d boundary inputs, no raw value stored)"
          % len(MASK_CASES))


# ---------------------------------------------------------------------------
# M13: one submitter, one row (cases 12-19)
# ---------------------------------------------------------------------------

ANCHOR_STORE_PREFIX = "cco.anchor2.v1|"
TOKEN_STORE_PREFIX = "cco.token.v1|"


def anchor_set(tag, count=16, start=0):
    """A client-side anchor list. Any two lists sharing one element are the
    same machine as far as the API is concerned, which is exactly what the
    overlapping ranges below exercise."""
    return [hashlib.sha256(("%s#%d" % (tag, i)).encode("utf-8")).hexdigest()
            for i in range(start, start + count)]


def stored_anchor(anchor):
    """What the record must hold for that anchor: a SECOND hash. This is the
    security property — the public file never carries a replayable value."""
    return hashlib.sha256((ANCHOR_STORE_PREFIX + anchor).encode("utf-8")).hexdigest()


def stored_token(token):
    return hashlib.sha256((TOKEN_STORE_PREFIX + token).encode("utf-8")).hexdigest()


def day(iso):
    return datetime.date(int(iso[0:4]), int(iso[5:7]), int(iso[8:10]))


def shift(iso, n):
    return (day(iso) + datetime.timedelta(days=n)).isoformat()


def scan_payload(start, days, requests, losses, wasted, iron, **over):
    """A submission covering `days` consecutive calendar days from `start`,
    with sum(daily) == totals by construction."""
    rows = [{"date": shift(start, i), "requests": requests,
             "losses": losses, "wasted_tokens": wasted} for i in range(days)]
    payload = valid_payload(
        period_start=rows[0]["date"], period_end=rows[-1]["date"], daily=rows,
        totals={"requests": requests * days, "in_ttl_losses": losses * days,
                "iron_losses": iron, "wasted_tokens": wasted * days})
    payload.update(over)
    return payload


def reset_doc():
    """Start a stage from an empty dataset. Each M13 case is about the
    relationship between rows, so it needs to own the whole repository — and
    since M14 that means the detail files and the fleet series too, which is
    why this rebuilds the repo rather than blanking one document."""
    with MOCK.lock:
        _reset_repo()


def doc_snapshot():
    return index_doc()


def submit(payload, ip):
    return http_json("POST", BASE + "/api/submit", payload,
                     headers={"X-Forwarded-For": ip})


def expect_ok(name, status, data):
    check(status == 200, "%s: status %s, want 200 (body %r)" % (name, status, data))
    check(isinstance(data, dict) and data.get("ok") is True,
          "%s: body %r, want ok:true" % (name, data))
    return data


def daily_of(row, files=None):
    """The daily rows of an index row. Since M14 they live one file away, and
    every M13 assertion below that used to read row["daily"] reads them through
    here — which is itself a check that the detail file exists and belongs to
    the row that points at it."""
    detail = detail_doc(row["id"], files)
    check(detail is not None, "no detail file for %s" % row["id"])
    check(detail.get("id") == row["id"],
          "detail file for %s names %r" % (row["id"], detail.get("id")))
    return detail["daily"]


def run_identity_cases():
    # -- case12: two folder submissions from the same machine => ONE row -----
    reset_doc()
    ip = "198.51.100.30"
    a1 = anchor_set("machine-one", 16, 0)
    a2 = anchor_set("machine-one", 16, 15)   # shares exactly one anchor with a1
    check(len(set(a1) & set(a2)) == 1, "case12 setup: the two scans must share 1 anchor")

    first = scan_payload("2026-06-01", 10, 100, 2, 1000, 7, anchors=a1)
    status, data = submit(first, ip)
    data = expect_ok("case12 first", status, data)
    check(data.get("merged") is False, "case12: the first submission reported a merge")
    check("token" not in data,
          "case12: a fingerprinted submission was handed a token to store — the "
          "folder path must not be asked to store anything (%r)" % data)
    row_id = data["id"]
    before = check_file_arithmetic("case12 first")
    check(len(before["submissions"]) == 1,
          "case12: %d rows after the first submission" % len(before["submissions"]))
    stored_first = before["submissions"][0]
    check("updated_at" not in stored_first,
          "case12: a brand new row already claims to have been updated")
    check("identity" not in stored_first,
          "case12: the index row carries an identity block again")
    check(identity_of(row_id)["anchor_hashes"] == [stored_anchor(x) for x in a1],
          "case12: %s does not hold the second hash of what was sent"
          % IDENTITY_PATH)
    # The double-hash property, re-proven where the values now live. Scanning
    # every public file rather than just the index: the point of M14.1 is that
    # these moved, and a check that only looked at the index would pass for the
    # wrong reason after the move.
    for sent in a1:
        check(sent not in leak_blob(),
              "case12: an anchor the client sent is stored verbatim — anyone "
              "reading the public files could replay it")

    # A fresher scan of the same machine: 3 days overlap, 5 days are new.
    second = scan_payload("2026-06-08", 8, 200, 5, 3000, 11, anchors=a2)
    status, data = submit(second, ip)
    data = expect_ok("case12 second", status, data)
    check(data.get("merged") is True,
          "case12: the same machine's second submission was not merged (%r)" % data)
    check(data.get("id") == row_id, "case12: the row changed id on update")
    after = check_file_arithmetic("case12 second")
    check(len(after["submissions"]) == 1,
          "case12: %d rows after the second submission from the SAME machine — "
          "this is the double count M13 exists to remove"
          % len(after["submissions"]))
    row = after["submissions"][0]
    check(row["period_start"] == "2026-06-01" and row["period_end"] == "2026-06-15",
          "case12: merged period %s..%s, want 2026-06-01..2026-06-15"
          % (row["period_start"], row["period_end"]))
    check(row["submitted_at"] == stored_first["submitted_at"],
          "case12: submitted_at moved; it records the FIRST submission")
    check(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.get("updated_at", "")),
          "case12: no updated_at on a merged row (%r)" % row.get("updated_at"))
    row_daily = daily_of(row)
    by_date = {d["date"]: d for d in row_daily}
    check(len(row_daily) == 15,
          "case12: %d daily rows, want 15 (10 + 8 with 3 overlapping)"
          % len(row_daily))
    check(row.get("daily_days") == 15,
          "case12: the index says %r daily rows but the detail file holds 15"
          % row.get("daily_days"))
    check(by_date["2026-06-01"]["requests"] == 100,
          "case12: a day only the first submission covered was overwritten")
    check(by_date["2026-06-08"]["requests"] == 200,
          "case12: an overlapping day kept the STALER measurement")
    check(by_date["2026-06-15"]["requests"] == 200,
          "case12: a day only the second submission covered is missing")
    want_requests = 7 * 100 + 8 * 200
    check(row["totals"]["requests"] == want_requests,
          "case12: totals.requests %d, want %d (recomputed from the merged rows, "
          "not summed across submissions)" % (row["totals"]["requests"], want_requests))
    # iron: the 3 superseded days carried 6 in-TTL losses, which is less than the
    # 7 iron the first row claimed, so 1 survives alongside the fresh 11.
    check(row["totals"]["iron_losses"] == 12,
          "case12: iron_losses %d, want 12 (7 - 6 superseded losses, + 11)"
          % row["totals"]["iron_losses"])
    check(identity_of(row_id)["anchor_hashes"] == [stored_anchor(x) for x in a2],
          "case12: the stored anchors were not refreshed to the newest sample")
    check(list(identity_entries()) == [row_id],
          "case12: %s holds %r, want one entry for the one row"
          % (IDENTITY_PATH, list(identity_entries())))
    with MOCK.lock:
        msg = MOCK.commit_messages[-1]
        merge_written = MOCK.commit_log[-1]["written"]
    check(msg.startswith("data: update " + row_id + " —"),
          "case12: the commit message does not say this was an update: %r" % msg)
    check(merge_written == sorted([INDEX_PATH, IDENTITY_PATH, FLEET_PATH,
                                   "data/subs/%s.json" % row_id]),
          "case12: a MERGE must also rewrite all four files in one commit, not "
          "%r" % (merge_written,))
    print("PASS case12 two folder submissions, same machine -> 1 row "
          "(%s..%s, %d requests, %d daily rows)"
          % (row["period_start"], row["period_end"],
             row["totals"]["requests"], len(row_daily)))
    print("       rows %d -> %d · id %s kept" % (1, len(after["submissions"]), row_id))
    print("       before %s..%s %s"
          % (stored_first["period_start"], stored_first["period_end"],
             json.dumps(stored_first["totals"], sort_keys=True)))
    print("       after  %s..%s %s"
          % (row["period_start"], row["period_end"],
             json.dumps(row["totals"], sort_keys=True)))
    print("       pre-M13 would have summed to requests=%d in_ttl_losses=%d"
          % (first["totals"]["requests"] + second["totals"]["requests"],
             first["totals"]["in_ttl_losses"] + second["totals"]["in_ttl_losses"]))

    # -- case13: a value copied out of a PUBLIC file changes nothing ----------
    # M14 widened what "the public file" means and M14.1 moved the piece this
    # case is about. data/identity.json is in the same public repository as
    # everything else — it is not hidden, not restricted, and anyone may open it
    # — so the replay is mounted from exactly there, and all three files an
    # attacker could aim at must come back byte-identical: the row, its detail
    # file, and the identity entry the hash was lifted out of.
    published = identity_of(row_id)["anchor_hashes"][0]
    detail_before = detail_doc(row_id)
    identity_before = identity_doc()
    replay = scan_payload("2026-07-01", 3, 9999, 0, 0, 0,
                          anchors=[published], nickname="attacker")
    status, data = submit(replay, "198.51.100.31")
    data = expect_ok("case13", status, data)
    check(data.get("merged") is False,
          "🔴 case13: a hash lifted out of the public dataset MERGED into the row "
          "it came from — the public file is an overwrite key")
    check(data.get("id") != row_id, "case13: the replay was given the victim's id")
    doc = check_file_arithmetic("case13")
    check(len(doc["submissions"]) == 2,
          "case13: %d rows, want 2 (the replay appends as a stranger)"
          % len(doc["submissions"]))
    check(doc["submissions"][0] == row,
          "🔴 case13: the target row CHANGED after a replay of its own published "
          "hash\n  before %r\n  after  %r" % (row, doc["submissions"][0]))
    check(detail_doc(row_id) == detail_before,
          "🔴 case13: the target's DETAIL FILE changed after a replay of a "
          "published hash — the daily rows are public too, and a value copied "
          "out of any public file must still modify nothing")
    check(identity_of(row_id) == identity_before["identities"][row_id],
          "🔴 case13: the target's IDENTITY ENTRY changed after a replay of the "
          "hash lifted out of it — moving the digests to their own public file "
          "must not have made them an overwrite key")
    check(published not in json.dumps(identity_of(data["id"]) or {}),
          "case13: the replayed hash was stored again under the attacker's own "
          "id, which would make it match the victim on the next submission")
    print("PASS case13 replay of a published hash -> new row; target row, detail "
          "file AND identity entry all byte-identical")
    print("       sent anchors=[%s…] (copied from %s), got id %s, rows %d"
          % (published[:16], IDENTITY_PATH, data["id"], len(doc["submissions"])))

    # -- case14: server-only and malformed identity fields => 400 ------------
    bad_ip = "198.51.100.32"
    server_only = [
        ("identity", {"anchor_hashes": [stored_anchor(a1[0])]}),
        ("anchor_hashes", [stored_anchor(a1[0])]),
        ("token_hash", stored_token("0" * 32)),
        ("updated_at", "2026-08-24"),
        ("id", "sub-20260824115135-75fb"),
        ("submitted_at", "2026-08-24"),
    ]
    for field, value in server_only:
        status, data = submit(scan_payload("2026-06-01", 2, 10, 0, 0, 0,
                                           **{field: value}), bad_ip)
        expect_schema_400("case14 " + field, status, data)
        check(any(("undefined field: " + field) in str(d) for d in data["detail"]),
              "case14 %s: a server-generated field was not rejected as undefined: %r"
              % (field, data["detail"]))
    malformed = [
        ("anchors not an array", {"anchors": "deadbeef"}),
        ("anchors over the cap", {"anchors": anchor_set("x", 17, 0)}),
        ("anchor too short", {"anchors": ["a" * 63]}),
        ("anchor uppercase", {"anchors": ["A" * 64]}),
        ("anchor not a string", {"anchors": [12345]}),
        ("token too short", {"token": "0" * 31}),
        ("token uppercase", {"token": "A" * 32}),
        ("token is a stored hash", {"token": stored_token("0" * 32)}),
        ("token not a string", {"token": 12345}),
    ]
    for name, over in malformed:
        status, data = submit(scan_payload("2026-06-01", 2, 10, 0, 0, 0, **over),
                              bad_ip)
        expect_schema_400("case14 " + name, status, data)
    doc = doc_snapshot()
    check(len(doc["submissions"]) == 2,
          "case14: a rejected payload still reached storage (%d rows)"
          % len(doc["submissions"]))
    print("PASS case14 %d server-only + %d malformed identity fields -> 400"
          % (len(server_only), len(malformed)))

    # -- case15: a disjoint increment is ADDED, never a replacement ----------
    reset_doc()
    ip = "198.51.100.33"
    a = anchor_set("machine-inc", 16, 0)
    base = scan_payload("2026-07-01", 3, 100, 4, 500, 3, anchors=a)
    status, data = submit(base, ip)
    expect_ok("case15 base", status, data)
    inc = scan_payload("2026-07-10", 3, 50, 1, 200, 1, anchors=a)
    status, data = submit(inc, ip)
    data = expect_ok("case15 increment", status, data)
    check(data.get("merged") is True, "case15: the increment opened a second row")
    doc = check_file_arithmetic("case15")
    row = doc["submissions"][0]
    check(len(doc["submissions"]) == 1, "case15: %d rows" % len(doc["submissions"]))
    row_daily = daily_of(row)
    check(len(row_daily) == 6,
          "🔴 case15: %d daily rows, want 6 — a 3-day increment must not wipe the "
          "days it does not mention" % len(row_daily))
    check(row["period_start"] == "2026-07-01" and row["period_end"] == "2026-07-12",
          "case15: period %s..%s, want 2026-07-01..2026-07-12"
          % (row["period_start"], row["period_end"]))
    check(row["totals"]["requests"] == 3 * 100 + 3 * 50,
          "case15: totals.requests %d, want 450" % row["totals"]["requests"])
    check(row["totals"]["iron_losses"] == 4,
          "case15: iron_losses %d, want 4 (nothing superseded, so 3 + 1)"
          % row["totals"]["iron_losses"])
    print("PASS case15 disjoint increment merges (6 daily rows, %s..%s)"
          % (row["period_start"], row["period_end"]))

    # -- case16: a full re-scan of an overlapping period, fresher wins -------
    reset_doc()
    ip = "198.51.100.34"
    a = anchor_set("machine-rescan", 16, 0)
    status, data = submit(scan_payload("2026-07-01", 5, 100, 4, 500, 9, anchors=a), ip)
    expect_ok("case16 first", status, data)
    status, data = submit(scan_payload("2026-07-01", 8, 111, 3, 777, 5, anchors=a), ip)
    data = expect_ok("case16 rescan", status, data)
    check(data.get("merged") is True, "case16: the re-scan opened a second row")
    doc = check_file_arithmetic("case16")
    row = doc["submissions"][0]
    check(len(doc["submissions"]) == 1, "case16: %d rows" % len(doc["submissions"]))
    row_daily = daily_of(row)
    check(len(row_daily) == 8, "case16: %d daily rows, want 8" % len(row_daily))
    check(all(d["requests"] == 111 for d in row_daily),
          "case16: a day the re-scan covered kept the older measurement")
    check(row["totals"]["requests"] == 8 * 111,
          "case16: totals.requests %d, want %d" % (row["totals"]["requests"], 8 * 111))
    check(row["totals"]["iron_losses"] == 5,
          "case16: iron_losses %d, want 5 — every day of the older row was "
          "superseded, so only the fresh count stands"
          % row["totals"]["iron_losses"])
    print("PASS case16 overlapping re-scan merges, fresher rows win "
          "(8 daily rows, %d requests)" % row["totals"]["requests"])

    # -- case17: the paste path falls back to a token ------------------------
    reset_doc()
    ip = "198.51.100.35"
    paste = scan_payload("2026-05-01", 4, 70, 2, 300, 2)   # no anchors at all
    check("anchors" not in paste, "case17 setup: the paste payload carries anchors")
    status, data = submit(paste, ip)
    data = expect_ok("case17 first", status, data)
    check(data.get("merged") is False, "case17: the first paste reported a merge")
    token = data.get("token", "")
    check(re.fullmatch(r"[0-9a-f]{32}", token),
          "case17: no link token was issued to a submission that cannot be "
          "fingerprinted (%r)" % token)
    doc = check_file_arithmetic("case17 first")
    ident = identity_of(doc["submissions"][0]["id"])
    check(ident is not None and ident.get("token_hash") == stored_token(token),
          "case17: %s does not hold the hash of the issued token" % IDENTITY_PATH)
    check("anchor_hashes" not in ident,
          "case17: a paste row invented anchors: %r" % ident)
    check(token not in leak_blob(),
          "🔴 case17: the token itself was written into the public dataset")

    status, data = submit(scan_payload("2026-05-05", 4, 80, 1, 400, 1, token=token), ip)
    data = expect_ok("case17 return", status, data)
    check(data.get("merged") is True,
          "case17: presenting the issued token did not update the same row (%r)" % data)
    check("token" not in data, "case17: a second token was issued to the same row")
    doc = check_file_arithmetic("case17 return")
    check(len(doc["submissions"]) == 1,
          "case17: %d rows after a token-linked return" % len(doc["submissions"]))
    check(len(daily_of(doc["submissions"][0])) == 8,
          "case17: the token-linked merge lost days (%d)"
          % len(daily_of(doc["submissions"][0])))

    status, data = submit(scan_payload("2026-05-20", 2, 10, 0, 0, 0), ip)
    data = expect_ok("case17 no token", status, data)
    check(data.get("merged") is False,
          "case17: a paste with no token was merged into somebody's row")
    check(re.fullmatch(r"[0-9a-f]{32}", data.get("token", "")),
          "case17: the appended paste row was not given its own token")
    doc = check_file_arithmetic("case17 no token")
    check(len(doc["submissions"]) == 2,
          "case17: %d rows, want 2 (an unlinkable paste appends)"
          % len(doc["submissions"]))
    check(sorted(identity_entries()) == sorted(r["id"] for r in doc["submissions"]),
          "case17: %s keys %r do not match the index's rows %r"
          % (IDENTITY_PATH, sorted(identity_entries()),
             sorted(r["id"] for r in doc["submissions"])))
    print("PASS case17 paste path -> token issued, presented token merges, "
          "no token appends")

    # -- case18: a folder scan adopts the row its paste created --------------
    reset_doc()
    ip = "198.51.100.36"
    status, data = submit(scan_payload("2026-04-01", 3, 60, 2, 100, 1), ip)
    data = expect_ok("case18 paste", status, data)
    token = data["token"]
    row_id = data["id"]
    a = anchor_set("machine-adopt", 16, 0)
    status, data = submit(scan_payload("2026-04-05", 3, 90, 1, 150, 1,
                                       anchors=a, token=token), ip)
    data = expect_ok("case18 folder", status, data)
    check(data.get("merged") is True and data.get("id") == row_id,
          "case18: a folder scan presenting the stored token did not adopt the "
          "paste row (%r)" % data)
    doc = check_file_arithmetic("case18 folder")
    ident = identity_of(row_id)
    check(ident is not None and ident.get("anchor_hashes") ==
          [stored_anchor(x) for x in a],
          "case18: the adopted row did not gain the machine's fingerprint")
    check(ident.get("token_hash") == stored_token(token),
          "case18: adopting the row dropped its token")
    status, data = submit(scan_payload("2026-04-10", 2, 30, 0, 0, 0, anchors=a), ip)
    data = expect_ok("case18 fingerprint only", status, data)
    check(data.get("merged") is True and data.get("id") == row_id,
          "case18: the row no longer matches by fingerprint alone (%r)" % data)
    doc = check_file_arithmetic("case18 fingerprint only")
    check(len(doc["submissions"]) == 1, "case18: %d rows" % len(doc["submissions"]))
    print("PASS case18 paste row adopted by a fingerprinted submission, then "
          "matched by fingerprint alone")

    # -- case19: masking still holds on the update path ----------------------
    reset_doc()
    ip = "198.51.100.37"
    a = anchor_set("machine-mask", 16, 0)
    raw_first = "first-raw-nick"
    raw_update = "update-raw-nick"
    status, data = submit(scan_payload("2026-03-01", 2, 10, 0, 0, 0,
                                       anchors=a, nickname=raw_first), ip)
    expect_ok("case19 first", status, data)
    status, data = submit(scan_payload("2026-03-05", 2, 10, 0, 0, 0,
                                       anchors=a, nickname=raw_update), ip)
    data = expect_ok("case19 update", status, data)
    check(data.get("merged") is True, "case19: the second submission did not merge")
    doc = check_file_arithmetic("case19")
    check(doc["submissions"][0]["nickname"] == "u***",
          "case19: the updated nickname is not masked: %r"
          % doc["submissions"][0]["nickname"])
    blob = leak_blob()
    for raw in (raw_first, raw_update, "attacker", "contract-test"):
        check(raw not in blob,
              "🔴 case19: the raw nickname %r survives in the stored data or a "
              "commit message" % raw)
    with MOCK.lock:
        check(not MOCK.errors, "mock observed protocol violations: %r" % MOCK.errors)
        n_messages = len(MOCK.commit_messages)
    print("PASS case19 masking holds across an update (raw values absent from "
          "%d commit messages and every public file)" % n_messages)

    run_fleet_cases()


# ---------------------------------------------------------------------------
# M14: the fleet series, and what happens when a detail file is not there
# ---------------------------------------------------------------------------

def fleet_by_date():
    return {d["date"]: d for d in fleet_doc()["days"]}


def run_fleet_cases():
    # -- case21: data/daily.json is the sum ACROSS submissions ---------------
    # Two different machines, overlapping days, then one of them merges. The
    # fleet series has to track all three moves without ever double counting —
    # it is maintained as a delta, so a merge that added its new totals without
    # taking the old ones out would inflate every day the two submissions share.
    reset_doc()
    a = anchor_set("machine-fleet-a", 16, 0)
    b = anchor_set("machine-fleet-b", 16, 0)

    status, data = submit(scan_payload("2026-09-01", 3, 100, 5, 700, 2, anchors=a),
                          "198.51.100.40")
    expect_ok("case21 A", status, data)
    check_file_arithmetic("case21 A")
    days = fleet_by_date()
    check(sorted(days) == ["2026-09-01", "2026-09-02", "2026-09-03"],
          "case21: fleet dates %r after one 3-day submission" % sorted(days))
    check(days["2026-09-01"] == {"date": "2026-09-01", "requests": 100,
                                 "losses": 5, "wasted_tokens": 700, "machines": 1},
          "case21: %r" % days["2026-09-01"])

    status, data = submit(scan_payload("2026-09-02", 3, 40, 1, 300, 1, anchors=b),
                          "198.51.100.41")
    expect_ok("case21 B", status, data)
    check_file_arithmetic("case21 B")
    days = fleet_by_date()
    check(days["2026-09-02"]["machines"] == 2,
          "case21: a day two machines both report says machines=%r"
          % days["2026-09-02"]["machines"])
    check(days["2026-09-02"]["requests"] == 140 and days["2026-09-02"]["losses"] == 6,
          "case21: a shared day is not the sum of both machines: %r"
          % days["2026-09-02"])
    check(days["2026-09-01"]["machines"] == 1,
          "case21: a day only one machine reports says machines=%r"
          % days["2026-09-01"]["machines"])

    # A re-scan from machine A: same 3 days, different numbers. The old rows
    # must come OUT of the series as the new ones go in.
    status, data = submit(scan_payload("2026-09-01", 3, 250, 9, 1200, 4, anchors=a),
                          "198.51.100.40")
    data = expect_ok("case21 A rescan", status, data)
    check(data.get("merged") is True, "case21: the re-scan opened a second row")
    check_file_arithmetic("case21 A rescan")
    days = fleet_by_date()
    check(days["2026-09-01"]["requests"] == 250,
          "🔴 case21: a re-scanned day reads %d requests, want 250 — the merge "
          "added the fresh numbers without taking the superseded ones out"
          % days["2026-09-01"]["requests"])
    check(days["2026-09-02"]["requests"] == 250 + 40,
          "🔴 case21: a shared, re-scanned day reads %d requests, want 290"
          % days["2026-09-02"]["requests"])
    check(days["2026-09-02"]["machines"] == 2,
          "case21: a merge changed the machine count of a shared day to %r"
          % days["2026-09-02"]["machines"])
    total_req = sum(d["requests"] for d in fleet_doc()["days"])
    want_req = 3 * 250 + 3 * 40
    check(total_req == want_req,
          "case21: the fleet series totals %d requests, want %d"
          % (total_req, want_req))
    print("PASS case21 fleet series = sum across machines, delta-maintained "
          "(%d days, %d requests, machines 1..2)"
          % (len(days), total_req))

    # -- case22: a row whose detail file is unreadable is NOT merged into -----
    # Deleting a detail file is something only a repo admin can do, and the
    # honest response is to refuse the write. Merging against daily rows that
    # failed to load would recompute the row's totals from the incoming
    # submission alone and silently delete that machine's history.
    reset_doc()
    c = anchor_set("machine-orphan", 16, 0)
    status, data = submit(scan_payload("2026-10-01", 4, 60, 3, 250, 2, anchors=c),
                          "198.51.100.42")
    data = expect_ok("case22 base", status, data)
    row_id = data["id"]
    check_file_arithmetic("case22 base")
    with MOCK.lock:
        removed = MOCK.files.pop("data/subs/%s.json" % row_id)
        # rebuild the tip so the tree the worker reads really lacks the file
        _land(dict(MOCK.files), "admin: delete a detail file",
              ["data/subs/%s.json" % row_id])
        files_before = dict(MOCK.files)
        commits_before = MOCK.commit_count
    status, data = submit(scan_payload("2026-10-10", 2, 70, 1, 100, 1, anchors=c),
                          "198.51.100.42")
    check(status == 502, "case22: status %s, want 502 when the row's detail file "
          "cannot be read (body %r)" % (status, data))
    with MOCK.lock:
        check(MOCK.commit_count == commits_before,
              "🔴 case22: a submission committed after failing to read the row's "
              "history — the merge would have wiped days it never saw")
        check(dict(MOCK.files) == files_before,
              "case22: the refused submission still changed the public files")
    # put it back so the dataset is whole again for the final leak scan
    with MOCK.lock:
        MOCK.files["data/subs/%s.json" % row_id] = removed
        _land(MOCK.files, "admin: restore the detail file",
              ["data/subs/%s.json" % row_id])
    check_file_arithmetic("case22 restored")
    print("PASS case22 unreadable detail file -> 502, nothing committed, public "
          "files untouched")

    with MOCK.lock:
        check(not MOCK.errors, "mock observed protocol violations: %r" % MOCK.errors)


# ---------------------------------------------------------------------------
# M14.2: the retry budget, and the failures the mock could not previously make
# ---------------------------------------------------------------------------
#
# Every case below exists because something in the write path had no test that
# could reach it. Three of them were measured as surviving mutants: the two tree
# status guards (the mock could only fail blob POSTs, so a mutant that treated a
# failed root-tree GET as "this repository is empty" — and would have written an
# index containing one row, wiping every other submitter's — passed every
# suite), and the commit-POST failure branch (a mutant returning {url:""} says
# "your data was saved" when no ref was ever updated). The rest are the D1/D2
# defects: a burst that lost 80% of its submissions and called it a storage
# outage, and one hand-edited row that made the API publish a dataset that did
# not add up.

# Mirrors COMMIT_MAX_ATTEMPTS in functions/api/submit.js. Stated here as a
# number rather than read out of the source on purpose: the budget is part of
# what the endpoint promises, so changing it should have to be written down
# twice. A budget nothing pins is a budget that can quietly go back to one.
EXPECTED_COMMIT_ATTEMPTS = 6

MALFORMED_ROWS = [
    ("a literal null", None),
    ("no date key", {"requests": 10, "losses": 0, "wasted_tokens": 0}),
    ("a number instead of a row", 5),
    ("a date that is not a date",
     {"date": "not-a-date", "requests": 10, "losses": 0, "wasted_tokens": 0}),
    ("an emptied daily array", EMPTY_DAILY),
]


def repo_snapshot():
    """The public files and the commit count. Every refusal below has to leave
    both exactly as it found them."""
    with MOCK.lock:
        return dict(MOCK.files), MOCK.commit_count


def expect_refused(name, status, data, snapshot,
                   want_status=502, want_error="storage"):
    files_before, commits_before = snapshot
    check(status == want_status,
          "%s: status %s, want %d (body %r)" % (name, status, want_status, data))
    check(isinstance(data, dict) and data.get("ok") is False and
          data.get("error") == want_error,
          "%s: body %r, want {ok:false, error:%r}" % (name, data, want_error))
    with MOCK.lock:
        landed = MOCK.commit_count - commits_before
        changed = dict(MOCK.files) != files_before
    check(landed == 0,
          "🔴 %s: %d commit(s) landed on a refused submission" % (name, landed))
    check(not changed,
          "🔴 %s: a refused submission still changed the public files" % name)


def run_m142_cases():
    # -- case25: one hand-edited daily row is not a mergeable history ---------
    # Measured before the fix, over HTTP, against eight malformed shapes: seven
    # returned 200 and published a fleet series carrying a day no submission
    # covered, and the eighth (a literal null) threw out of applyFleetDelta and
    # escaped as HTTP 500. The date of a malformed row cannot be recovered, so
    # it cannot be subtracted; refusing is the only answer that leaves the
    # dataset no worse than the admin left it.
    for i, (why, shape) in enumerate(MALFORMED_ROWS):
        reset_doc()
        ip = "198.51.100.%d" % (70 + i)
        a = anchor_set("machine-malformed-%d" % i, 16, 0)
        status, data = submit(
            scan_payload("2026-05-01", 2, 10, 0, 0, 0, anchors=a), ip)
        data = expect_ok("case25 base (%s)" % why, status, data)
        check_file_arithmetic("case25 base (%s)" % why)
        corrupt_detail_row(data["id"], shape)
        snapshot = repo_snapshot()
        status, data = submit(
            scan_payload("2026-05-20", 2, 10, 0, 0, 0, anchors=a), ip)
        expect_refused("case25 (%s)" % why, status, data, snapshot)
    print("PASS case25 %d malformed detail rows -> 502, nothing committed "
          "(a literal null no longer escapes as a 500)" % len(MALFORMED_ROWS))

    # -- case26: losing the branch every time is a conflict, not an outage ----
    # Three shapes of the same refusal, because the write path must recognise it
    # by status OR by message. GitHub's answer is 422 + "Update is not a fast
    # forward" and that has never been exercised against the live API, so the
    # second leg proves a host that uses 409 still earns retries, and the third
    # proves a host that changes the status while keeping the sentence does too.
    # Getting this wrong in that direction is what turns every conflict into a
    # hard failure.
    reset_doc()
    conflict_shapes = [
        (422, "Update is not a fast forward"),      # GitHub, as documented
        (409, "Reference cannot be updated"),       # recognised by status alone
        (400, "Update is not a fast forward"),      # recognised by message alone
    ]
    for n, (conflict_status, conflict_message) in enumerate(conflict_shapes):
        with MOCK.lock:
            MOCK.mode = "conflict_always"
            MOCK.conflict_status = conflict_status
            MOCK.conflict_message = conflict_message
            patches_before = MOCK.ref_patches
            refgets_before = MOCK.ref_gets
        snapshot = repo_snapshot()
        status, data = submit(
            scan_payload("2026-08-01", 2, 10, 0, 0, 0,
                         anchors=anchor_set("machine-busy-%d" % n, 16, 0)),
            "198.51.100.%d" % (80 + n))
        with MOCK.lock:
            MOCK.mode = "normal"
            MOCK.conflict_status = 422
            MOCK.conflict_message = "Update is not a fast forward"
            patch_delta = MOCK.ref_patches - patches_before
            refget_delta = MOCK.ref_gets - refgets_before
        name = "case26 (%d %r forever)" % (conflict_status, conflict_message)
        expect_refused(name, status, data, snapshot,
                       want_status=409, want_error="conflict")
        check(isinstance(data.get("retry_after"), int) and
              0 < data["retry_after"] <= 60,
              "%s: retry_after %r is not a small second count"
              % (name, data.get("retry_after")))
        check(patch_delta == EXPECTED_COMMIT_ATTEMPTS,
              "🔴 %s: %d ref updates attempted, want %d — one retry is not a "
              "budget, and at ten simultaneous submissions it accepted two"
              % (name, patch_delta, EXPECTED_COMMIT_ATTEMPTS))
        check(refget_delta == EXPECTED_COMMIT_ATTEMPTS,
              "%s: %d ref reads, want %d (every attempt must re-read HEAD)"
              % (name, refget_delta, EXPECTED_COMMIT_ATTEMPTS))
    # …and 409 is tolerated as the conflict itself, not only as the exhaustion.
    # The suite used to pin the retry to the ONE status the mock returns, which
    # proves the mock agrees with itself and nothing about GitHub.
    with MOCK.lock:
        MOCK.mode = "conflict_once"
        MOCK.conflict_fired = False
        MOCK.conflict_status = 409
        commits_before = MOCK.commit_count
    status, data = submit(
        scan_payload("2026-08-01", 2, 10, 0, 0, 0,
                     anchors=anchor_set("machine-409", 16, 0)),
        "198.51.100.82")
    with MOCK.lock:
        MOCK.mode = "normal"
        MOCK.conflict_status = 422
        commit_delta = MOCK.commit_count - commits_before
    expect_ok("case26 409 conflict", status, data)
    check(commit_delta == 2,
          "case26: %d commits after a 409 conflict, want 2 (the third party's "
          "and ours) — a host that answers 409 instead of 422 must earn a "
          "retry, not a hard failure" % commit_delta)
    check_file_arithmetic("case26 409 retry")
    print("PASS case26 conflict exhaustion -> 409 {error:conflict, retry_after} "
          "after %d attempts, nothing committed; 409 and 422 both retry"
          % EXPECTED_COMMIT_ATTEMPTS)

    # -- case27: a tree listing that fails is not an empty repository ---------
    # 🔴 The mutant this exists for: drop `if (root.status !== 200) return null`
    # and a failed root-tree GET reads as "there is no data/ here", so the write
    # produces an index containing ONLY the new row — every other submitter's
    # row gone, data/daily.json and data/identity.json replaced, every detail
    # file orphaned. It survived every suite because the mock could only fail
    # blob POSTs.
    reset_doc()
    a = anchor_set("machine-tree", 16, 0)
    status, data = submit(
        scan_payload("2026-04-01", 2, 10, 0, 0, 0, anchors=a), "198.51.100.83")
    expect_ok("case27 base", status, data)
    check_file_arithmetic("case27 base")
    for n, role in enumerate(("root", "data", "subs")):
        with MOCK.lock:
            MOCK.fail_tree = role
        snapshot = repo_snapshot()
        status, data = submit(
            scan_payload("2026-04-10", 2, 10, 0, 0, 0, anchors=a),
            "198.51.100.%d" % (84 + n))
        with MOCK.lock:
            MOCK.fail_tree = None
        expect_refused("case27 (%s tree GET fails)" % role, status, data, snapshot)
    print("PASS case27 a failing tree listing (root / data / subs) -> 502, "
          "nothing committed, no row wiped")

    # -- case28: a commit POST that fails never becomes a success ------------
    # The mutant: return {url:""} instead of null, which tells the submitter
    # their data was saved when no ref was ever updated.
    with MOCK.lock:
        MOCK.fail_commit_post = True
        blobs_before = MOCK.blob_posts
        patches_before = MOCK.ref_patches
    snapshot = repo_snapshot()
    status, data = submit(
        scan_payload("2026-04-20", 2, 10, 0, 0, 0, anchors=a), "198.51.100.87")
    with MOCK.lock:
        MOCK.fail_commit_post = False
        blob_delta = MOCK.blob_posts - blobs_before
        patch_delta = MOCK.ref_patches - patches_before
    expect_refused("case28 (commit POST fails)", status, data, snapshot)
    check(blob_delta == 4,
          "case28: %d blobs posted, want 4 (the batch goes out, the commit is "
          "what fails)" % blob_delta)
    check(patch_delta == 0,
          "🔴 case28: the ref was updated %d time(s) after the commit POST "
          "failed" % patch_delta)
    print("PASS case28 a failing commit POST -> 502, ref untouched, nothing "
          "published")

    # -- case29: a new submitter whose logs are OLDER than the series --------
    # Machine A reports June, machine B then reports May. Nothing in the
    # contract test had ever submitted a window earlier than one already in the
    # fleet series, which is an ordinary thing for a new submitter with older
    # logs — and without the sort in applyFleetDelta the public file reads
    # ["2026-06-01", "2026-05-01"].
    reset_doc()
    status, data = submit(
        scan_payload("2026-06-01", 3, 100, 2, 500, 1,
                     anchors=anchor_set("machine-june", 16, 0)), "198.51.100.88")
    expect_ok("case29 June", status, data)
    status, data = submit(
        scan_payload("2026-05-01", 3, 50, 1, 200, 0,
                     anchors=anchor_set("machine-may", 16, 0)), "198.51.100.89")
    expect_ok("case29 May", status, data)
    check_file_arithmetic("case29")
    dates = [d["date"] for d in fleet_doc()["days"]]
    check(dates == sorted(dates),
          "🔴 case29: the fleet series is out of date order after an earlier "
          "window was submitted second: %r" % dates)
    check(dates[0] == "2026-05-01" and dates[-1] == "2026-06-03",
          "case29: fleet series spans %r..%r, want 2026-05-01..2026-06-03"
          % (dates[0], dates[-1]))
    print("PASS case29 June then May -> the fleet series is sorted (%s..%s, "
          "%d days)" % (dates[0], dates[-1], len(dates)))

    # -- case30: a tree listing GitHub truncated is a failed read ------------
    # GitHub truncates at 100,000 entries and data/subs/ holds one file per
    # submitter. The reply still looks like a complete listing, so ignoring the
    # flag means treeEntry() returns null and the write path decides a returning
    # submitter has no detail file — the read that is wrong in the one direction
    # that then writes.
    reset_doc()
    a = anchor_set("machine-trunc", 16, 0)
    status, data = submit(
        scan_payload("2026-03-01", 2, 10, 0, 0, 0, anchors=a), "198.51.100.90")
    expect_ok("case30 base", status, data)
    for n, role in enumerate(("root", "data", "subs")):
        with MOCK.lock:
            MOCK.truncate_tree = role
        snapshot = repo_snapshot()
        status, data = submit(
            scan_payload("2026-03-10", 2, 10, 0, 0, 0, anchors=a),
            "198.51.100.%d" % (91 + n))
        with MOCK.lock:
            MOCK.truncate_tree = None
        expect_refused("case30 (%s listing truncated)" % role, status, data,
                       snapshot)
    print("PASS case30 a truncated tree listing -> 502 (the 100,000-entry "
          "ceiling fails loudly instead of losing a file)")

    # -- case31: the two guards a hand-edited data/daily.json reaches --------
    # Both were recorded as equivalent mutants "for the submission-only
    # universe", which is true and is not the whole universe: data/daily.json is
    # a file in a public repository and an admin can edit it. These two cases
    # reach them, so they are covered rather than argued about.
    reset_doc()
    ip = "198.51.100.94"
    a = anchor_set("machine-phantom", 16, 0)
    status, data = submit(
        scan_payload("2026-09-01", 3, 100, 5, 700, 2, anchors=a), ip)
    expect_ok("case31 phantom base", status, data)
    land_fleet_days(fleet_doc()["days"] +
                    [{"date": "2026-01-15", "requests": 0, "losses": 0,
                      "wasted_tokens": 0, "machines": 0}],
                    "a fleet row no submission covers")
    status, data = submit(
        scan_payload("2026-09-04", 2, 10, 1, 20, 0, anchors=a), ip)
    expect_ok("case31 phantom", status, data)
    check_file_arithmetic("case31 phantom")
    check("2026-01-15" not in [d["date"] for d in fleet_doc()["days"]],
          "🔴 case31: a day with machines:0 survived into the published series "
          "— a row reading '0 requests, 0 machines' is a claim about a day "
          "nobody observed")

    # …and a series hand-edited BELOW the detail files behind it, which drives
    # the delta negative. The dataset is invalid either way here — that is the
    # admin's edit, not something the write path can repair — so what is
    # asserted is the narrow thing the clamps promise: a public file never
    # carries a negative count.
    reset_doc()
    ip = "198.51.100.95"
    a = anchor_set("machine-clamp", 16, 0)
    status, data = submit(
        scan_payload("2026-09-01", 3, 100, 5, 700, 2, anchors=a), ip)
    expect_ok("case31 clamp base", status, data)
    days = [dict(d) for d in fleet_doc()["days"]]
    for d in days:
        if d["date"] == "2026-09-01":
            d["requests"], d["losses"], d["wasted_tokens"] = 5, 0, 1
    land_fleet_days(days, "a fleet row smaller than the detail file behind it")
    status, data = submit(
        scan_payload("2026-09-01", 3, 3, 1, 2, 0, anchors=a), ip)
    expect_ok("case31 clamp", status, data)
    for d in fleet_doc()["days"]:
        for col in ("requests", "losses", "wasted_tokens", "machines"):
            check(isinstance(d[col], int) and d[col] >= 0,
                  "🔴 case31: %s on %s is %r — a hand-edited series drove the "
                  "delta negative and a NEGATIVE count reached a public file"
                  % (col, d["date"], d[col]))
    reset_doc()   # that dataset is deliberately inconsistent; do not leave it
    print("PASS case31 a hand-edited fleet series: a machines:0 day is dropped "
          "and a negative delta is clamped, never published")

    with MOCK.lock:
        check(not MOCK.errors, "mock observed protocol violations: %r" % MOCK.errors)


# ---------------------------------------------------------------------------
# M14.1: the two sides of index <-> identity, one of which no live path can reach
# ---------------------------------------------------------------------------

def _mini_dataset(identities):
    """The smallest dataset that validates, plus whatever identity map is being
    tested against it. One row, one day, one detail file."""
    sub_id = "sub-20260824115135-75fb"
    totals = {"requests": 10, "in_ttl_losses": 2, "iron_losses": 1,
              "wasted_tokens": 40}
    daily = [{"date": "2026-08-24", "requests": 10, "losses": 2,
              "wasted_tokens": 40}]
    index = {"schema_version": 2, "submissions": [{
        "id": sub_id, "submitted_at": "2026-08-24", "nickname": "anonymous",
        "plan": "unknown", "client": "unknown", "concurrent_sessions": "unknown",
        "period_start": "2026-08-24", "period_end": "2026-08-24",
        "totals": totals, "daily_days": 1,
        "detail": "data/subs/%s.json" % sub_id, "script_version": "web-1.0"}]}
    fleet = {"schema_version": 1, "days": [dict(daily[0], machines=1)]}
    details = {sub_id: {"schema_version": 1, "id": sub_id,
                        "period_start": "2026-08-24", "period_end": "2026-08-24",
                        "totals": totals, "daily": daily}}
    doc = None if identities is None else {"schema_version": 1,
                                           "identities": identities}
    return sub_id, index, fleet, details, doc


def run_validator_cases():
    """The index<->identity relation is deliberately ASYMMETRIC, and only one
    half of it is reachable from the write path. A row with no entry happens for
    real (case24, and the row committed in data/). An entry with no row cannot
    be produced by any sequence of submissions — the map is derived from the
    index that was just written — so the only way to prove the validator would
    catch it is to hand it one. Needs no server, so it runs before wrangler
    boots and fails in seconds rather than minutes."""
    anchors = [stored_anchor("x%d" % i) for i in range(16)]

    def errors_for(identities):
        _sid, index, fleet, details, doc = _mini_dataset(identities)
        return dataset_validate.validate(index, fleet, details, doc)

    sub_id, _i, _f, _d, _doc = _mini_dataset({})

    ok_cases = [
        ("no identity file at all (the pre-M14.1 repository)", None),
        ("a file with no entries, and a row that has none", {}),
        ("a row with a full 16-anchor fingerprint",
         {sub_id: {"anchor_hashes": anchors}}),
        ("a row with a token hash only (the paste path)",
         {sub_id: {"token_hash": stored_token("0" * 32)}}),
        ("a row with both",
         {sub_id: {"anchor_hashes": anchors, "token_hash": stored_token("1" * 32)}}),
    ]
    for why, identities in ok_cases:
        errs = errors_for(identities)
        check(not errs, "validator: rejected a legitimate dataset (%s): %r"
              % (why, errs))

    orphan = "sub-20260101000000-beef"
    bad_cases = [
        ("an entry keyed by an id the index has no row for",
         {orphan: {"anchor_hashes": anchors}}, "identity/%s" % orphan),
        ("an entry that is empty", {sub_id: {}}, "neither anchor_hashes"),
        ("an anchor that is not a sha-256 digest",
         {sub_id: {"anchor_hashes": ["nope"]}}, "lowercase sha-256"),
        ("an uppercase anchor digest",
         {sub_id: {"anchor_hashes": ["A" * 64]}}, "lowercase sha-256"),
        ("more than 16 anchors",
         {sub_id: {"anchor_hashes": anchors + [stored_anchor("y")]}}, "max 16"),
        ("a token hash that is not a digest",
         {sub_id: {"token_hash": "0" * 31}}, "token_hash"),
        ("an undefined field inside an entry",
         {sub_id: {"anchor_hashes": anchors, "anchors": ["raw"]}}, "undefined field"),
        ("an entry that is not an object", {sub_id: "deadbeef"}, "not an object"),
    ]
    for why, identities, needle in bad_cases:
        errs = errors_for(identities)
        check(errs, "🔴 validator: accepted a broken dataset (%s)" % why)
        check(any(needle in e for e in errs),
              "validator (%s): no violation mentions %r: %r" % (why, needle, errs))

    # The index row itself must not carry the block back.
    _sid, index, fleet, details, doc = _mini_dataset({})
    index["submissions"][0]["identity"] = {"anchor_hashes": anchors}
    errs = dataset_validate.validate(index, fleet, details, doc)
    check(any("still carries an identity block" in e for e in errs),
          "🔴 validator: an index row carrying the identity block again was "
          "accepted: %r" % errs)

    # A malformed document, as opposed to a malformed entry.
    _sid, index, fleet, details, _doc = _mini_dataset({})
    for why, doc, needle in (
            ("wrong schema_version", {"schema_version": 2, "identities": {}},
             "schema_version"),
            ("identities is not an object",
             {"schema_version": 1, "identities": []}, "not an object"),
            ("not a JSON object", ["nope"], "not a JSON object")):
        errs = dataset_validate.validate(index, fleet, details, doc)
        check(any(needle in e for e in errs),
              "validator (%s): no violation mentions %r: %r" % (why, needle, errs))

    # 🔴 M14.2. The validator's regexes must mean what the worker's mean.
    # Python's `$` matches before a trailing newline and JavaScript's does not,
    # so `sub-20260824115135-75fb\n` used to pass SUB_ID_RE here while
    # functions/api/submit.js refused the identical string. No traversal follows
    # from it — the worker is the one that builds paths — but the validator
    # would bless a row the write path will refuse forever with a 502, which is
    # the one direction a validator must never be wrong in.
    newline_cases = []

    _sid, index, fleet, details, doc = _mini_dataset({})
    index["submissions"][0]["id"] = sub_id + "\n"
    newline_cases.append(("an id with a trailing newline",
                          dataset_validate.validate(index, fleet, details, doc),
                          "is not sub-"))

    _sid, index, fleet, details, doc = _mini_dataset({})
    details[sub_id]["daily"][0]["date"] = "2026-08-24\n"
    newline_cases.append(("a daily date with a trailing newline",
                          dataset_validate.validate(index, fleet, details, doc),
                          "no valid date"))

    _sid, index, fleet, details, _doc = _mini_dataset({})
    doc = {"schema_version": 1,
           "identities": {sub_id: {"anchor_hashes": [anchors[0] + "\n"]}}}
    newline_cases.append(("an anchor digest with a trailing newline",
                          dataset_validate.validate(index, fleet, details, doc),
                          "lowercase sha-256"))

    for why, errs, needle in newline_cases:
        check(errs, "🔴 validator: accepted %s — Python's $ matches before a "
                    "trailing newline and JavaScript's does not, so this "
                    "validator is more lenient than the write path it mirrors"
                    % why)
        check(any(needle in e for e in errs),
              "validator (%s): no violation mentions %r: %r" % (why, needle, errs))

    print("PASS validator %d valid + %d invalid identity datasets, including "
          "'index row with no identity' (valid), 'identity entry with no "
          "index row' (error) and %d trailing-newline shapes the worker refuses"
          % (len(ok_cases), len(bad_cases) + 4, len(newline_cases)))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if not os.path.isfile(SUBMIT_JS):
        print("FATAL: %s missing" % SUBMIT_JS)
        return 2
    if port_in_use(PORT):
        print("FATAL: port %d already in use — kill the stale wrangler first "
              "(a stale server would silently test old code)" % PORT)
        return 2

    # The rate-limit ledger (case1..case6) must live inside one UTC hour.
    now = time.time()
    to_next_hour = 3600 - (int(now) % 3600)
    if to_next_hour < 150:
        print("hour boundary in %ds — waiting it out so the rate-limit ledger "
              "stays in one bucket" % to_next_hour)
        time.sleep(to_next_hour + 2)

    npx = find_npx()
    if npx is None:
        print("FATAL: npx not found (node.js install required)")
        return 2

    # Pure-python, no server: fail in seconds if the validator itself is wrong,
    # rather than after a four-minute wrangler boot.
    try:
        run_validator_cases()
    except ContractFail as exc:
        print("CONTRACT_FAIL: %s" % exc)
        return 1

    _reset_repo()   # a repository that exists, with an empty dataset in it
    mock_server, mock_port = start_mock()
    scratch = make_scratch()
    xff_ip = "203.0.113.%d" % random.randint(1, 254)
    print("mock GitHub API on 127.0.0.1:%d · scratch %s" % (mock_port, scratch))

    proc = log = log_path = None
    code = 1
    try:
        proc, log, log_path = start_wrangler(npx, mock_port, scratch)
        print("wrangler pages dev starting on %s (deadline %ds)…"
              % (BASE, BOOT_DEADLINE_SECONDS))
        wait_ready(proc, log_path)
        print("wrangler ready — running contract cases")
        run_cases(xff_ip)
        run_mask_cases()
        run_identity_cases()
        run_m142_cases()
        print("CONTRACT_OK")
        code = 0
    except ContractFail as exc:
        print("CONTRACT_FAIL: %s" % exc)
        code = 1
    except SetupFail as exc:
        print("FATAL(setup): %s" % exc)
        code = 2
    finally:
        if proc is not None:
            stop_wrangler(proc, log)
        mock_server.shutdown()
        mock_server.server_close()
        cleanup_error = None
        for attempt in range(5):
            try:
                guarded_delete_scratch(scratch)
                cleanup_error = None
                break
            except OSError as exc:  # handles still settling after taskkill
                cleanup_error = exc
                time.sleep(1)
        if cleanup_error is not None:
            print("WARN: scratch cleanup failed (left in place): %s -> %s"
                  % (scratch, cleanup_error))
    return code


if __name__ == "__main__":
    sys.exit(main())
