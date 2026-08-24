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

All pass -> prints CONTRACT_OK as the last line, exit 0.
Contract violation -> exit 1. Setup/infra failure -> exit 2 (the mutation
harness treats exit 1 as KILLED and exit 2 as fatal, so infra problems can
never masquerade as a defended mutation).

No real GitHub, no real tokens, no network beyond 127.0.0.1.
"""
import base64
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
    temp-root whitelist + prefix + marker check + no junction following."""
    if not path:
        raise RuntimeError("refusing delete: empty path")
    real = os.path.realpath(path)
    tmp_root = os.path.realpath(tempfile.gettempdir())
    if not real.startswith(tmp_root + os.sep):
        raise RuntimeError("refusing delete outside temp root: %r" % real)
    if not os.path.basename(real).startswith(SCRATCH_PREFIX):
        raise RuntimeError("refusing delete: prefix mismatch: %r" % real)
    if not os.path.isfile(os.path.join(real, SCRATCH_MARKER)):
        raise RuntimeError("refusing delete: scratch marker missing: %r" % real)
    _rm_no_follow(real)


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
        check(stored.get("nickname") == "contract-test", "case1: nickname mismatch")
        check(stored.get("totals") == payload["totals"], "case1: totals mismatch")
        check(stored.get("daily") == payload["daily"], "case1: daily mismatch")
        extra = set(stored) - {"id", "submitted_at", "nickname", "plan", "client",
                               "concurrent_sessions", "period_start", "period_end",
                               "totals", "daily", "script_version"}
        check(not extra, "case1: undefined fields stored: %r" % extra)
        want_msg = ("data: submission %s — contract-test, 2026-08-01~2026-08-15, "
                    "10 losses / 1000 req" % sub_id)
        check(MOCK.put_messages[-1] == want_msg,
              "case1: commit message %r != %r" % (MOCK.put_messages[-1], want_msg))
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
        check(subs[-1].get("nickname") == "&lt;b&gt;x&lt;/b&gt;",
              "case7: nickname not HTML-escaped: %r" % subs[-1].get("nickname"))
    print("PASS case7 sha conflict -> one retry -> 200, nickname escaped")

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
