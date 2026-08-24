#!/usr/bin/env python3
"""Contract test for functions/api/submit.js (06_FUNCTIONAL_SPEC.md section 2).

Self-contained: boots `npx wrangler pages dev` against the site directory
(KV binding RATE_LIMIT, GitHub API base pointed at a local mock), runs the
mock GitHub Contents API (GET/PUT of data/submissions.json) in-process, and
drives the contract cases:

  case1  valid submission        -> 200, PUT reaches the mock, append verified
  case2  undefined field         -> 400 (reject, not drop)
  case3  losses > requests       -> 400
  case4  period > 92 days        -> 400
  case5  nickname 21 chars       -> 400
  case6  4th submit, same IP     -> 429 with retry_after
  case7  sha conflict (409) once -> re-GET + one retry -> 200
  case8  GitHub 5xx              -> 502 storage (single PUT, no retry)
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
import secrets
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
# mock GitHub Contents API
# ---------------------------------------------------------------------------

class MockState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.doc = {"schema_version": 1, "submissions": []}
        self.sha = secrets.token_hex(20)
        self.mode = "normal"          # normal | conflict_once | server_error
        self.conflict_fired = False
        self.get_count = 0
        self.put_count = 0
        self.put_messages = []
        self.commit_serial = 0
        self.errors = []              # protocol violations noticed by the mock


MOCK = MockState()
CONTENTS_PATH = "/repos/%s/contents/data/submissions.json" % MOCK_REPO


def _b64_github(text):
    raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\n".join(raw[i:i + 60] for i in range(0, len(raw), 60)) + "\n"


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_common(self):
        auth = self.headers.get("Authorization", "")
        if auth != "Bearer " + MOCK_TOKEN:
            MOCK.errors.append("bad Authorization header: %r" % auth)
        if not self.headers.get("User-Agent"):
            MOCK.errors.append("missing User-Agent")

    def do_GET(self):
        with MOCK.lock:
            self._check_common()
            if self.path != CONTENTS_PATH:
                self._send(404, {"message": "Not Found"})
                return
            MOCK.get_count += 1
            text = json.dumps(MOCK.doc, ensure_ascii=False, indent=2) + "\n"
            self._send(200, {
                "path": "data/submissions.json",
                "sha": MOCK.sha,
                "encoding": "base64",
                "content": _b64_github(text),
            })

    def do_PUT(self):
        with MOCK.lock:
            self._check_common()
            if self.path != CONTENTS_PATH:
                self._send(404, {"message": "Not Found"})
                return
            MOCK.put_count += 1
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                MOCK.errors.append("PUT body is not JSON")
                self._send(400, {"message": "bad body"})
                return
            if MOCK.mode == "server_error":
                self._send(500, {"message": "boom"})
                return
            if MOCK.mode == "conflict_once" and not MOCK.conflict_fired:
                # simulate a commit landing in between: rotate the sha
                MOCK.conflict_fired = True
                MOCK.sha = secrets.token_hex(20)
                self._send(409, {"message": "data/submissions.json does not match"})
                return
            if body.get("sha") != MOCK.sha:
                self._send(409, {"message": "sha mismatch"})
                return
            if not isinstance(body.get("message"), str) or not body["message"]:
                MOCK.errors.append("PUT without a commit message")
            try:
                text = base64.b64decode(body["content"]).decode("utf-8")
                MOCK.doc = json.loads(text)
            except Exception as exc:
                MOCK.errors.append("PUT content undecodable: %r" % exc)
                self._send(400, {"message": "bad content"})
                return
            MOCK.put_messages.append(body["message"])
            MOCK.sha = secrets.token_hex(20)
            MOCK.commit_serial += 1
            self._send(201, {
                "content": {"sha": MOCK.sha},
                "commit": {
                    "sha": "c" * 40,
                    "html_url": "https://example.invalid/commit/%d" % MOCK.commit_serial,
                },
            })


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
        check(MOCK.put_count == 0, "400 cases must not reach storage "
                                   "(put_count=%d)" % MOCK.put_count)

    # -- case1: valid submission => 200 + append verified --------------------
    payload = valid_payload()
    status, data = post(payload)
    check(status == 200, "case1: status %s, want 200 (body %r)" % (status, data))
    check(isinstance(data, dict) and data.get("ok") is True,
          "case1: body %r, want ok:true" % (data,))
    sub_id = data.get("id", "")
    check(re.fullmatch(r"sub-\d{14}-[0-9a-f]{4}", sub_id),
          "case1: id %r does not match sub-{timestamp14}-{hex4}" % sub_id)
    check(data.get("commit_url") == "https://example.invalid/commit/1",
          "case1: commit_url %r not the mock's html_url" % data.get("commit_url"))
    with MOCK.lock:
        check(MOCK.put_count == 1, "case1: put_count=%d, want 1" % MOCK.put_count)
        subs = MOCK.doc.get("submissions", [])
        check(MOCK.doc.get("schema_version") == 1, "case1: schema_version lost")
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
        check(stored.get("daily") == payload["daily"], "case1: daily mismatch")
        # M13 adds exactly two server-generated keys, and `updated_at` only
        # once a row has actually been merged into.
        extra = set(stored) - {"id", "submitted_at", "nickname", "plan", "client",
                               "concurrent_sessions", "period_start", "period_end",
                               "totals", "daily", "script_version", "identity"}
        check(not extra, "case1: undefined fields stored: %r" % extra)
        check("updated_at" not in stored,
              "case1: a first submission is marked as updated")
        # This payload carries no anchors, so the API falls back to layer 2 and
        # issues a token. Only its hash may be stored.
        check(re.fullmatch(r"[0-9a-f]{32}", data.get("token", "")),
              "case1: no link token issued to a submission with no anchors (%r)"
              % data.get("token"))
        check(data.get("merged") is False, "case1: a first submission reported a merge")
        check(stored["identity"].get("token_hash") ==
              hashlib.sha256(("cco.token.v1|" + data["token"]).encode("utf-8")).hexdigest(),
              "case1: identity.token_hash is not the hash of the issued token")
        check(data["token"] not in json.dumps(MOCK.doc),
              "case1: the token itself was written into the public dataset")
        want_msg = ("data: submission %s — c***, 2026-08-01~2026-08-15, "
                    "10 losses / 1000 req" % sub_id)
        check(MOCK.put_messages[-1] == want_msg,
              "case1: commit message %r != %r" % (MOCK.put_messages[-1], want_msg))
        check("contract-test" not in MOCK.put_messages[-1],
              "case1: raw nickname leaked into the commit message")
    print("PASS case1 valid submit -> 200, append + commit message verified")

    # -- case7: sha conflict once => re-GET + single retry => 200 ------------
    with MOCK.lock:
        MOCK.mode = "conflict_once"
        MOCK.conflict_fired = False
        put_before = MOCK.put_count
        get_before = MOCK.get_count
    status, data = post(valid_payload(nickname="<b>x</b>"))
    check(status == 200, "case7: status %s, want 200 after retry (body %r)" % (status, data))
    with MOCK.lock:
        MOCK.mode = "normal"
        check(MOCK.put_count - put_before == 2,
              "case7: %d PUTs, want 2 (409 then success)" % (MOCK.put_count - put_before))
        check(MOCK.get_count - get_before == 2,
              "case7: %d GETs, want 2 (re-GET before the retry)" % (MOCK.get_count - get_before))
        subs = MOCK.doc.get("submissions", [])
        check(len(subs) == 2, "case7: %d submissions stored, want 2" % len(subs))
        # Masking runs before escaping, so the escape has to survive the slice:
        # "<b>x</b>" keeps "<" as its one visible code point and stores it as
        # the entity. A raw "<" here would be an injection into the public JSON.
        check(subs[-1].get("nickname") == "&lt;***",
              "case7: nickname not masked+escaped: %r" % subs[-1].get("nickname"))
    print("PASS case7 sha conflict -> one retry -> 200, nickname masked + escaped")

    # -- case8: GitHub 5xx => 502 storage, no retry --------------------------
    with MOCK.lock:
        MOCK.mode = "server_error"
        put_before = MOCK.put_count
    status, data = post(valid_payload())
    with MOCK.lock:
        MOCK.mode = "normal"
        put_delta = MOCK.put_count - put_before
    check(status == 502, "case8: status %s, want 502 (body %r)" % (status, data))
    check(isinstance(data, dict) and data.get("ok") is False and
          data.get("error") == "storage",
          "case8: body %r, want {ok:false, error:'storage'}" % (data,))
    check(put_delta == 1, "case8: %d PUTs, want 1 (5xx earns no retry)" % put_delta)
    print("PASS case8 storage 5xx -> 502, single PUT")

    # -- case6: 4th rate-limited submission from the same IP => 429 ----------
    # Ledger so far for this IP hash+hour: case1 + case7 + case8 = 3 counted.
    with MOCK.lock:
        put_before = MOCK.put_count
    status, data = post(valid_payload())
    check(status == 429, "case6: status %s, want 429 on the 4th submit (body %r)"
          % (status, data))
    check(isinstance(data, dict) and data.get("ok") is False and
          data.get("error") == "rate_limited",
          "case6: body %r, want {ok:false, error:'rate_limited', ...}" % (data,))
    check(isinstance(data.get("retry_after"), int) and 0 < data["retry_after"] <= 3600,
          "case6: retry_after %r not a sane second count" % data.get("retry_after"))
    with MOCK.lock:
        check(MOCK.put_count == put_before,
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
        with MOCK.lock:
            stored = MOCK.doc["submissions"][-1].get("nickname")
            msg = MOCK.put_messages[-1]
        check(stored == want,
              "case11 (%s): stored %r, want %r" % (why, stored, want))
        check(("— %s," % want) in msg,
              "case11 (%s): commit message %r does not carry the masked value"
              % (why, msg))

    # A per-case check only looks at the last record. Scan the whole stored
    # document and every commit message the mock ever received: a raw value
    # that leaked into an earlier field would otherwise pass unnoticed.
    with MOCK.lock:
        blob = json.dumps(MOCK.doc, ensure_ascii=False) + "\n".join(MOCK.put_messages)
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
    relationship between rows, so it needs to own the whole file."""
    with MOCK.lock:
        MOCK.doc = {"schema_version": 1, "submissions": []}
        MOCK.sha = secrets.token_hex(20)


def doc_snapshot():
    with MOCK.lock:
        return json.loads(json.dumps(MOCK.doc, ensure_ascii=False))


def submit(payload, ip):
    return http_json("POST", BASE + "/api/submit", payload,
                     headers={"X-Forwarded-For": ip})


def expect_ok(name, status, data):
    check(status == 200, "%s: status %s, want 200 (body %r)" % (name, status, data))
    check(isinstance(data, dict) and data.get("ok") is True,
          "%s: body %r, want ok:true" % (name, data))
    return data


def check_file_arithmetic(tag):
    """🔴 After every path: the public file's own numbers still add up, and its
    daily rows stay inside the period they claim. A reader adding up the JSON by
    hand is the site's whole trust model."""
    doc = doc_snapshot()
    check(doc.get("schema_version") == 1, "%s: schema_version lost" % tag)
    for row in doc["submissions"]:
        daily = row["daily"]
        dates = [d["date"] for d in daily]
        check(dates == sorted(dates), "%s/%s: daily rows are not in date order"
              % (tag, row["id"]))
        check(len(set(dates)) == len(dates), "%s/%s: a date appears twice"
              % (tag, row["id"]))
        check(row["period_start"] <= dates[0] and dates[-1] <= row["period_end"],
              "%s/%s: daily rows %s..%s fall outside the period %s..%s"
              % (tag, row["id"], dates[0], dates[-1],
                 row["period_start"], row["period_end"]))
        for field, total in (("requests", "requests"),
                             ("losses", "in_ttl_losses"),
                             ("wasted_tokens", "wasted_tokens")):
            got = sum(d[field] for d in daily)
            check(got == row["totals"][total],
                  "%s/%s: daily %s sums to %d but totals.%s says %d"
                  % (tag, row["id"], field, got, total, row["totals"][total]))
        check(row["totals"]["iron_losses"] <= row["totals"]["in_ttl_losses"],
              "%s/%s: iron_losses %d exceeds in_ttl_losses %d"
              % (tag, row["id"], row["totals"]["iron_losses"],
                 row["totals"]["in_ttl_losses"]))
    return doc


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
    check(stored_first["identity"]["anchor_hashes"] == [stored_anchor(x) for x in a1],
          "case12: the record does not hold the second hash of what was sent")
    for sent in a1:
        check(sent not in json.dumps(before),
              "case12: an anchor the client sent is stored verbatim — anyone "
              "reading the public file could replay it")

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
    by_date = {d["date"]: d for d in row["daily"]}
    check(len(row["daily"]) == 15,
          "case12: %d daily rows, want 15 (10 + 8 with 3 overlapping)"
          % len(row["daily"]))
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
    check(row["identity"]["anchor_hashes"] == [stored_anchor(x) for x in a2],
          "case12: the stored anchors were not refreshed to the newest sample")
    with MOCK.lock:
        msg = MOCK.put_messages[-1]
    check(msg.startswith("data: update " + row_id + " —"),
          "case12: the commit message does not say this was an update: %r" % msg)
    print("PASS case12 two folder submissions, same machine -> 1 row "
          "(%s..%s, %d requests, %d daily rows)"
          % (row["period_start"], row["period_end"],
             row["totals"]["requests"], len(row["daily"])))
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

    # -- case13: a value copied out of the PUBLIC file changes nothing --------
    published = row["identity"]["anchor_hashes"][0]
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
    print("PASS case13 replay of a published hash -> new row, target byte-identical")
    print("       sent anchors=[%s…] (copied from submissions.json), got id %s, "
          "rows %d" % (published[:16], data["id"], len(doc["submissions"])))

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
    check(len(row["daily"]) == 6,
          "🔴 case15: %d daily rows, want 6 — a 3-day increment must not wipe the "
          "days it does not mention" % len(row["daily"]))
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
    check(len(row["daily"]) == 8, "case16: %d daily rows, want 8" % len(row["daily"]))
    check(all(d["requests"] == 111 for d in row["daily"]),
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
    ident = doc["submissions"][0]["identity"]
    check(ident.get("token_hash") == stored_token(token),
          "case17: the record does not hold the hash of the issued token")
    check("anchor_hashes" not in ident,
          "case17: a paste row invented anchors: %r" % ident)
    check(token not in json.dumps(doc),
          "🔴 case17: the token itself was written into the public dataset")

    status, data = submit(scan_payload("2026-05-05", 4, 80, 1, 400, 1, token=token), ip)
    data = expect_ok("case17 return", status, data)
    check(data.get("merged") is True,
          "case17: presenting the issued token did not update the same row (%r)" % data)
    check("token" not in data, "case17: a second token was issued to the same row")
    doc = check_file_arithmetic("case17 return")
    check(len(doc["submissions"]) == 1,
          "case17: %d rows after a token-linked return" % len(doc["submissions"]))
    check(len(doc["submissions"][0]["daily"]) == 8,
          "case17: the token-linked merge lost days (%d)"
          % len(doc["submissions"][0]["daily"]))

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
    ident = doc["submissions"][0]["identity"]
    check(ident["anchor_hashes"] == [stored_anchor(x) for x in a],
          "case18: the adopted row did not gain the machine's fingerprint")
    check(ident["token_hash"] == stored_token(token),
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
    with MOCK.lock:
        blob = json.dumps(MOCK.doc, ensure_ascii=False) + "\n".join(MOCK.put_messages)
    for raw in (raw_first, raw_update, "attacker", "contract-test"):
        check(raw not in blob,
              "🔴 case19: the raw nickname %r survives in the stored data or a "
              "commit message" % raw)
    with MOCK.lock:
        check(not MOCK.errors, "mock observed protocol violations: %r" % MOCK.errors)
    print("PASS case19 masking holds across an update (raw values absent from "
          "%d commit messages and the whole file)" % len(MOCK.put_messages))


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
