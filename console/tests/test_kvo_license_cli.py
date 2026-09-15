"""scripts/kvo_license.py release modes, run as a real subprocess against a
fake KVO: --list, --release-all, --release CODE[,QTY].

Why a subprocess and not an import: the teardown script reads exit codes
and stdout from this program, so the exit code IS the contract, and the
thing to prove is what the operator's shell sees.

The fake KVO is an http.server thread that implements exactly the four
things the release path touches: the Keycloak token endpoint, GET
licenses, POST operations/deactivate (async: it answers {"url": ...}), and
the op status url that is polled until the state leaves IN_PROGRESS. It is
plain http, which the script honours when --kvo carries a scheme; the TLS
path is the activation flow's, unchanged and proven live.

Run:  cd console && python3 -m pytest tests/test_kvo_license_cli.py -q
      KVO_LICENSE_PY=/path/to/old/kvo_license.py ... points the suite at
      another copy of the script; the suite has to FAIL against the
      version before the release modes existed.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.environ.get("KVO_LICENSE_PY") or os.path.join(REPO, "scripts", "kvo_license.py")

PASSWORD = "s3cret-teardown!"
ROWS = [
    {"activationCode": "AAAA-1111-BBBB-1111", "product": "KVO-DEVICE", "quantity": 5,
     "partNumber": "KVO-DEV-01", "expirationDate": "2027-01-31"},
    {"activationCode": "CCCC-3333-DDDD-2222", "product": "CL-CREDIT", "quantity": 20},
]
FULL_CODES = [r["activationCode"] for r in ROWS]


class FakeKVO:
    """One fake KVO per test. `states` decides what an op reports per code;
    `stall` makes that op stay IN_PROGRESS forever; `break_list_after`
    makes GET licenses answer 500 once a deactivate has been received;
    `keep_zero_rows` keeps a fully released row on the list with quantity
    0 (the live KVO drops it, so both shapes have to be right);
    `cross_ref` makes every op's detail name the OTHER codes on the host,
    in its text and as dict keys; `token_garbage` answers the login with
    a line that is not an HTTP status line at all."""

    def __init__(self, rows, states=None, stall=(), break_list_after=False, list_body=None,
                 keep_zero_rows=False, cross_ref=False, token_garbage=False):
        self.rows = [dict(r) for r in rows]
        self.states = states or {}
        self.stall = set(stall)
        self.break_list_after = break_list_after
        self.list_body = list_body
        self.keep_zero_rows = keep_zero_rows
        self.cross_ref = cross_ref
        self.token_garbage = token_garbage
        self.deactivates = []          # bodies, in order
        self.token_posts = []          # parsed form bodies
        self.ops = {}                  # op id -> (code, qty)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n).decode() if n else ""

            def do_POST(self):
                p = urllib.parse.urlparse(self.path).path
                if p == "/auth/realms/keysight/protocol/openid-connect/token":
                    form = dict(urllib.parse.parse_qsl(self._body()))
                    fake.token_posts.append(form)
                    if fake.token_garbage:
                        self.close_connection = True
                        self.wfile.write(b"NOPE 200 OK\r\nContent-Length: 0\r\n\r\n")
                        return None
                    if form.get("password") != PASSWORD:
                        return self._send(401, {"error": "invalid_grant"})
                    return self._send(200, {"access_token": "tok-" + str(len(fake.token_posts))})
                if p == "/api/v2/licensing/operations/deactivate":
                    if self.headers.get("Authorization", "").startswith("Bearer tok-") is False:
                        return self._send(401, {"error": "no token"})
                    body = json.loads(self._body())
                    fake.deactivates.append(body)
                    code, qty = body[0]["activationCode"], body[0]["quantity"]
                    op = "op%d" % len(fake.deactivates)
                    fake.ops[op] = (code, qty)
                    # relative on purpose: the script has to resolve it against the base
                    return self._send(200, {"url": "/api/v2/licensing/operations/" + op})
                return self._send(404, {"error": "no route " + p})

            def do_GET(self):
                p = urllib.parse.urlparse(self.path).path
                if p == "/api/v2/licensing/licenses":
                    if fake.break_list_after and fake.deactivates:
                        return self._send(500, {"message": "internal error"})
                    if fake.list_body is not None:
                        return self._send(*fake.list_body)
                    return self._send(200, [r for r in fake.rows
                                            if fake.keep_zero_rows or r.get("quantity", 1) != 0])
                if p.startswith("/api/v2/licensing/operations/"):
                    rest = p[len("/api/v2/licensing/operations/"):]
                    op, _, tail = rest.partition("/")
                    if op not in fake.ops:
                        return self._send(404, {"error": "no op"})
                    code, qty = fake.ops[op]
                    if tail == "result":
                        detail = {"activationCode": code, "quantity": qty, "message": "detail for " + code}
                        if fake.cross_ref:
                            others = [r["activationCode"] for r in fake.rows if r["activationCode"] != code]
                            detail["message"] += "; other codes on this host: " + ", ".join(others)
                            for o in others:
                                detail[o] = "held"
                        return self._send(200, detail)
                    if code in fake.stall:
                        return self._send(200, {"state": "IN_PROGRESS"})
                    state = fake.states.get(code, "SUCCESS")
                    if state == "SUCCESS":
                        for r in fake.rows:
                            if r["activationCode"] == code and "quantity" in r:
                                r["quantity"] = max(0, r["quantity"] - qty)
                    return self._send(200, {"state": state})
                return self._send(404, {"error": "no route " + p})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake(request):
    """fake(**FakeKVO options) -> a running FakeKVO, closed after the test."""
    made = []

    def make(rows=ROWS, **kw):
        k = FakeKVO(rows, **kw)
        made.append(k)
        return k
    yield make
    for k in made:
        k.close()


@pytest.fixture
def kvo(fake):
    """The default fake: two licences, every op succeeds."""
    return fake()


def run(kvo_base, *args, password=PASSWORD, timeout=60):
    """Run the real script. The password travels through --password-env
    so it never sits on a command line. Every run is checked for a leak of
    a full activation code or the password."""
    env = dict(os.environ, KVO_TEST_PASS=password)
    p = subprocess.run([sys.executable, SCRIPT, "--kvo", kvo_base, "--password-env", "KVO_TEST_PASS",
                        "--http-timeout", "5", "--timeout", "20"] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=env)
    both = p.stdout + p.stderr
    for code in FULL_CODES:
        assert code not in both, "a full activation code leaked into the output: %s" % both
    assert PASSWORD not in both, "the password leaked into the output"
    return p


# ---------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------
def test_list_json_masks_codes(kvo):
    p = run(kvo.base, "--list", "--json")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["count"] == 2 and out["clear"] is False
    got = {r["code_last4"]: r for r in out["licences"]}
    assert set(got) == {"****-1111", "****-2222"}
    assert got["****-1111"]["part"] == "KVO-DEV-01" and got["****-1111"]["expiry"] == "2027-01-31"
    assert got["****-2222"]["part"] == "-" and got["****-2222"]["expiry"] == "-"
    assert got["****-2222"]["product"] == "CL-CREDIT" and got["****-2222"]["quantity"] == 20


def test_list_human_table(kvo):
    p = run(kvo.base, "--list")
    assert p.returncode == 0, p.stderr
    assert "[license] 2 licence(s) installed on KVO " in p.stdout
    assert "KVO-DEVICE" in p.stdout and "****-1111" in p.stdout and "****-2222" in p.stdout


def test_list_unreadable_is_unknown_not_empty(fake):
    kvo = fake(list_body=(200, "<html>eula</html>", "text/html"))
    """A list that is not the JSON array is exit 3 with a reason, never
    'no licences installed' with exit 0: that is what --json's clear=null says."""
    p = run(kvo.base, "--list", "--json")
    assert p.returncode == 3
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["clear"] is None and out["count"] == 0 and "unreadable" in out
    assert "could not be read" in p.stderr


# ---------------------------------------------------------------------
# --release-all
# ---------------------------------------------------------------------
def test_release_all_success_is_exit_0_and_list_empty(kvo):
    p = run(kvo.base, "--release-all")
    assert p.returncode == 0, p.stdout + p.stderr
    # every row, with its full quantity, in the proven body shape
    assert kvo.deactivates == [[{"activationCode": "AAAA-1111-BBBB-1111", "quantity": 5}],
                               [{"activationCode": "CCCC-3333-DDDD-2222", "quantity": 20}]]
    assert "released 2 licence(s); the KVO reports no licence left" in p.stdout
    assert "Nothing will be stranded" in p.stdout
    assert all(r["quantity"] == 0 for r in kvo.rows)
    # the password went through the environment, never argv
    assert kvo.token_posts and kvo.token_posts[0]["password"] == PASSWORD
    assert kvo.token_posts[0]["username"] == "admin"


def test_release_all_json_shape(kvo):
    p = run(kvo.base, "--release-all", "--json")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["released"] == 2 and out["failed"] == 0 and out["unknown"] == 0
    assert out["clear"] is True and out["remaining"] == []
    assert [r["code_last4"] for r in out["results"]] == ["****-1111", "****-2222"]


def test_release_all_stalled_op_is_unknown_not_success(fake):
    kvo = fake(stall=("CCCC-3333-DDDD-2222",))
    """An op still IN_PROGRESS when the budget runs out is UNKNOWN: exit 3,
    named as unknown, never counted as released, and the run ends within
    the bound instead of polling forever."""
    t0 = time.time()
    p = subprocess.run([sys.executable, SCRIPT, "--kvo", kvo.base, "--password-env", "KVO_TEST_PASS",
                        "--http-timeout", "5", "--timeout", "4", "--release-all"],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ, KVO_TEST_PASS=PASSWORD))
    took = time.time() - t0
    both = p.stdout + p.stderr
    for code in FULL_CODES:
        assert code not in both
    assert p.returncode == 3, both
    assert took < 30, "the release did not stop at its overall bound (%.1fs)" % took
    assert "****-1111 x5: released" in p.stdout
    assert "****-2222 x20: outcome UNKNOWN" in p.stdout and "not counted as released" in p.stdout
    # the reason is the time budget, and says so: the poll never saw a terminal state
    assert "no terminal state within the time budget; last state 'IN_PROGRESS'" in p.stdout
    assert "at the deadline" not in p.stdout and "_OP_DONE" not in p.stdout
    assert "1 operation(s) with an UNKNOWN outcome (not success): ****-2222" in p.stderr
    assert "NOT clear" in p.stderr
    # the stalled row never left the KVO, and the re-read says so
    assert "still holds 1 licence(s): ****-2222 x20" in p.stderr


def test_release_all_unrecognised_terminal_state_is_unknown_and_names_the_allow_list(fake):
    kvo = fake(states={"AAAA-1111-BBBB-1111": "COMPLETED"})
    """A KVO that answers a terminal word this script does not know (here
    COMPLETED, answered on the first poll) is UNKNOWN and exit 3, as before;
    but the line must not claim the poll ran out of time. It says the word
    is not one the script counts as released, and where that word would go
    if the KVO UI proves it released: the allow-list stays literal SUCCESS."""
    t0 = time.time()
    p = run(kvo.base, "--release-all")
    assert p.returncode == 3, p.stdout + p.stderr
    assert time.time() - t0 < 15, "an instant terminal answer must not be polled to the deadline"
    assert "****-1111 x5: outcome UNKNOWN (state 'COMPLETED' is not one this script counts as released" in p.stdout
    assert "that word belongs in _OP_DONE" in p.stdout and "not counted as released" in p.stdout
    assert "at the deadline" not in p.stdout and "time budget" not in p.stdout
    assert "****-2222 x20: released" in p.stdout
    assert "1 operation(s) with an UNKNOWN outcome (not success): ****-1111" in p.stderr
    assert "still holds 1 licence(s): ****-1111 x5" in p.stderr


def test_release_all_list_unreadable_after_is_unknown_not_clear(fake):
    kvo = fake(break_list_after=True)
    p = run(kvo.base, "--release-all")
    assert p.returncode == 3, p.stdout + p.stderr
    assert len(kvo.deactivates) == 2
    assert "could not be re-read after the release" in p.stderr
    assert "UNKNOWN, not clear" in p.stderr
    assert "HTTP 500" in p.stderr
    assert "Nothing will be stranded" not in p.stdout


def test_release_all_failed_op_is_exit_3_and_detail_is_scrubbed(fake):
    kvo = fake(states={"AAAA-1111-BBBB-1111": "FAILED"})
    """A refused deactivate is reported with the KVO's detail, but the
    detail's copy of the full code is masked too."""
    p = run(kvo.base, "--release-all")
    assert p.returncode == 3
    assert "****-1111 x5: FAILED (FAILED)" in p.stdout
    assert "detail for ****-1111" in p.stdout
    assert "1 operation(s) FAILED: ****-1111" in p.stderr
    assert "still holds 1 licence(s): ****-1111 x5" in p.stderr


def test_release_all_on_an_empty_kvo_is_clear(fake):
    kvo = fake(list_body=(200, []))
    p = run(kvo.base, "--release-all")
    assert p.returncode == 0, p.stderr
    assert kvo.deactivates == []
    assert "nothing to release" in p.stdout and "no licence left" in p.stdout


# ---------------------------------------------------------------------
# --release CODE[,QTY]
# ---------------------------------------------------------------------
def test_release_one_by_last4_leaves_the_rest(kvo):
    p = run(kvo.base, "--release", "2222")
    assert p.returncode == 3, p.stdout + p.stderr       # one released, one still held
    assert kvo.deactivates == [[{"activationCode": "CCCC-3333-DDDD-2222", "quantity": 20}]]
    assert "****-2222 x20: released" in p.stdout
    assert "still holds 1 licence(s): ****-1111 x5" in p.stderr


def test_release_one_full_code_partial_quantity(kvo):
    p = run(kvo.base, "--release", "AAAA-1111-BBBB-1111,2")
    assert p.returncode == 3
    assert kvo.deactivates == [[{"activationCode": "AAAA-1111-BBBB-1111", "quantity": 2}]]
    assert "****-1111 x2: released" in p.stdout
    assert "****-1111 x3" in p.stderr           # 3 remain, and the other row


def test_release_one_unknown_code(kvo):
    p = run(kvo.base, "--release", "ZZZZ-9999")
    assert p.returncode == 3
    assert kvo.deactivates == []
    assert "no installed licence matches ****-9999" in p.stderr


# ---------------------------------------------------------------------
# auth / reachability
# ---------------------------------------------------------------------
def test_wrong_password_is_exit_6(kvo):
    p = run(kvo.base, "--release-all", password="not-it")
    assert p.returncode == 6
    assert kvo.deactivates == []
    assert "refused the credentials" in p.stderr and "HTTP 401" in p.stderr
    assert "not-it" not in p.stdout + p.stderr


def test_garbage_status_line_is_exit_6_with_one_line(fake):
    kvo = fake(token_garbage=True)
    """A login answered with something that is not an HTTP status line
    raises http.client's own exception, which urllib does not wrap. That
    used to escape auth() as a traceback and exit 1 in the operator's
    terminal; it is one line and exit 6, like every other unreachable KVO."""
    p = run(kvo.base, "--list")
    assert p.returncode == 6, p.stdout + p.stderr
    assert "Traceback" not in p.stderr and "Traceback" not in p.stdout
    lines = [l for l in p.stderr.splitlines() if l.strip()]
    assert len(lines) == 1, p.stderr
    assert lines[0].startswith("[license] could not reach the KVO at ") and "BadStatusLine" in lines[0]


def test_unreachable_is_exit_6_within_the_bound():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    t0 = time.time()
    p = run("http://127.0.0.1:%d" % port, "--list")
    assert p.returncode == 6
    assert time.time() - t0 < 15
    assert "could not reach the KVO" in p.stderr


def test_password_env_unset_is_a_clean_stop(kvo):
    env = {k: v for k, v in os.environ.items() if k != "KVO_TEST_PASS"}
    p = subprocess.run([sys.executable, SCRIPT, "--kvo", kvo.base, "--password-env", "KVO_TEST_PASS", "--list"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 2 and "KVO_TEST_PASS: that variable is not set" in p.stderr
    assert kvo.token_posts == []
