"""Unit tests for the live console - pure logic and the HTTP handler, no AWS.
The handler tests drive Handler.do_* in-process against a BytesIO; no socket is
opened and no listener is bound.
Run:  cd console && python3 -m pytest tests -q     (or: python3 tests/test_console.py)
"""
import io
import os
import re
import sys
import json
import contextlib
from collections import namedtuple
from http.client import HTTPMessage, parse_headers

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cloudlens_console import events as E, flows as F, orchestrator as O, server  # noqa


Resp = namedtuple("Resp", "status headers payload raw")

PAGES_ORIGIN = "https://keysight-tech.github.io"


def _make_headers(pairs):
    """Build the exact object BaseHTTPRequestHandler parses from the wire.

    http.client.HTTPMessage is an email.message.Message: .get() is
    case-insensitive and repeated names survive via .get_all(). A plain dict
    is neither, so tests written against a dict can neither detect a casing
    bug nor a duplicate/overwritten-header bug.
    """
    if isinstance(pairs, HTTPMessage):
        return pairs
    items = pairs.items() if isinstance(pairs, dict) else list(pairs or [])
    blob = "".join("{}: {}\r\n".format(k, v) for k, v in items) + "\r\n"
    return parse_headers(io.BufferedReader(io.BytesIO(blob.encode("latin-1"))))


def _handler_response(path, method="GET", headers=None, body=None, content_length=True):
    """Drive Handler.do_* without a socket, capturing what it writes.

    Returns Resp(status, headers, payload, raw). `headers` is the response
    headers as an HTTPMessage, including the Server/Date the base class adds -
    those are on the wire, so tests must be able to see them.
    Pass content_length=False to send a deliberately mismatched body.
    """
    if path.startswith("/events/"):
        raise AssertionError("/events/<id> tails a live queue and never returns; "
                             "test the SSE layer directly")

    raw_body = body if isinstance(body, bytes) else (body or "").encode("utf-8")
    # A list of pairs, not just a dict: a dict cannot express a repeated header,
    # and a repeated Host is exactly the ambiguity the handler has to refuse.
    headers = headers or {}
    items = list(headers.items() if isinstance(headers, dict) else headers)
    if raw_body and content_length and not any(k.lower() == "content-length" for k, _ in items):
        items.append(("Content-Length", str(len(raw_body))))
    # Every HTTP/1.1 request on the wire carries a Host, and the handler rejects
    # one that is not ours (DNS rebinding). Tests that omit it are describing a
    # request no browser ever sends, so default it rather than let the guard be
    # invisible to the whole suite. Pass Host explicitly to exercise the guard.
    if not any(k.lower() == "host" for k, _ in items):
        items.append(("Host", "127.0.0.1:8760"))

    class CapturingHandler(server.Handler):
        def __init__(self):
            self.path = path
            self.command = method
            self.requestline = "{} {} HTTP/1.1".format(method, path)
            self.request_version = "HTTP/1.1"
            self.close_connection = True
            self.headers = _make_headers(items)
            self._headers_buffer = []     # the base end_headers() writes here
            self.rfile = io.BytesIO(raw_body)
            self.wfile = io.BytesIO()
            self.status = None
            self.sent_list = []

        def send_response(self, code, message=None):
            self.status = code
            super().send_response(code, message)

        def send_response_only(self, code, message=None):
            pass

        def send_header(self, k, v):
            self.sent_list.append((k, v))

        def end_headers(self):
            # Do NOT stub this out: Handler.end_headers() is where the
            # anti-framing headers are emitted, precisely so no response path
            # can skip them. Run the real one and swallow only the socket write.
            super().end_headers()

        def flush_headers(self):
            pass

        def log_message(self, *a):
            pass

    h = CapturingHandler()
    getattr(h, "do_" + method)()
    raw = h.wfile.getvalue()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        payload = None
    return Resp(h.status, _make_headers(h.sent_list), payload, raw)


@contextlib.contextmanager
def _running_server(host="127.0.0.1", allow_remote=False, quiet=True):
    """A real listener on a scratch port, with every global serve() touches
    restored on the way out.

    Port 0 so a scratch server never collides with a real console. quiet=False
    leaves handle_error alone, for the one test that is about what it prints.
    """
    from threading import Thread
    saved = (set(server.ALLOWED_ORIGINS), set(server.SELF_ORIGINS),
             server.LOOPBACK_ONLY, server.OUR_HOSTS, dict(server.JOBS))
    httpd = server.serve(host, 0, allow_remote=allow_remote)
    if quiet:
        # Tests that walk away from a never-ending stream RST the socket. That
        # is the test being rude, not the server misbehaving.
        httpd.handle_error = lambda request, addr: None
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd, httpd.server_address[1]
    finally:
        httpd.shutdown(); httpd.server_close()
        allowed, selfo, loopback, hosts, jobs = saved
        server.ALLOWED_ORIGINS.clear(); server.ALLOWED_ORIGINS.update(allowed)
        server.SELF_ORIGINS.clear(); server.SELF_ORIGINS.update(selfo)
        server.LOOPBACK_ONLY = loopback
        server.OUR_HOSTS = hosts
        server.JOBS.clear(); server.JOBS.update(jobs)


def _offmachine_addr():
    """The address a caller on the LAN would dial to reach this machine, or ''.

    A UDP connect() picks a route and sends nothing, so this touches no network
    and reaches no one; 192.0.2.1 is TEST-NET-1. Deliberately not read out of
    the server's own OUR_HOSTS: the point is to dial the machine the way a real
    client does and see whether the guard lets it in.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        addr = s.getsockname()[0]
        return "" if addr in ("", "0.0.0.0") else addr
    except OSError:
        return ""
    finally:
        s.close()


def test_health_leaks_nothing():
    r = _handler_response("/health")
    assert r.status == 200
    assert set(r.payload.keys()) == {"ok", "version"}
    assert r.payload["ok"] is True
    assert re.fullmatch(r"\d+\.\d+", r.payload["version"]), \
        "version is a wire-contract number, not a build id: a SHA would " \
        "fingerprint the visitor's machine"
    # Widened for CORS. access-control-allow-origin and vary are safe to emit
    # on this unauthenticated endpoint: neither carries machine-specific data.
    # ACAO only ever echoes a value the caller already sent AND that is on our
    # own static allowlist, so it tells a prober nothing it did not already
    # know; Vary: Origin is a cache directive, not content. Everything the
    # assertion is actually guarding - no Python version, no build id, no
    # hostname - is still guarded, because the set is still a closed allowlist.
    # Widened again for the anti-framing headers. Same reasoning as CORS: both
    # are constants describing OUR policy, not the visitor's machine, so they
    # tell a prober nothing. The set stays a closed allowlist, so no Python
    # version, build id or hostname can appear without failing here.
    assert {k.lower() for k, _ in r.headers.items()} <= {
        "server", "date", "content-type", "content-length", "cache-control",
        "access-control-allow-origin", "vary",
        "content-security-policy", "x-frame-options"}
    assert r.headers.get("server").strip() == "CloudLensConsole", \
        "the Server token is sent to unauthenticated probers: keep it a constant, " \
        "never a version or build id"
    assert r.headers.get("cache-control") == "no-store", \
        "the public page polls this to decide whether a console is running; a cached " \
        "ok=true keeps reporting a live console after the process has exited"


def test_health_answers_with_no_headers_at_all():
    # No Origin, no pairing header: the public page probes before the visitor
    # has typed anything. Any guard that breaks this breaks discovery.
    assert _handler_response("/health", headers={}).status == 200


def test_cors_allows_the_pages_origin():
    r = _handler_response("/health", headers={"Origin": PAGES_ORIGIN})
    assert r.headers.get("Access-Control-Allow-Origin") == PAGES_ORIGIN
    assert len(r.headers.get_all("Access-Control-Allow-Origin") or []) == 1, \
        "exactly one ACAO: a wildcard under a pinned origin is a browser-visible bug"
    assert "Origin" in (r.headers.get("Vary") or ""), \
        "Vary: Origin or a cache will serve one origin's response to another"


def test_cors_rejects_a_foreign_origin():
    r = _handler_response("/health", headers={"Origin": "https://evil.example"})
    assert r.headers.get("Access-Control-Allow-Origin") is None
    assert not r.headers.get_all("Access-Control-Allow-Origin")


def test_cors_never_emits_a_wildcard():
    for origin in (PAGES_ORIGIN, "https://evil.example", None):
        hdrs = {"Origin": origin} if origin else {}
        r = _handler_response("/health", headers=hdrs)
        assert "*" not in (r.headers.get_all("Access-Control-Allow-Origin") or []), \
            "a wildcard would let any site on the internet drive this console"


def test_preflight_allows_private_network():
    r = _handler_response(
        "/run", method="OPTIONS",
        headers={"Origin": PAGES_ORIGIN, "Access-Control-Request-Private-Network": "true"})
    assert r.status == 204
    assert r.headers.get("Access-Control-Allow-Private-Network") == "true", \
        "Chrome blocks public-page-to-127.0.0.1 without this; the failure is silent"
    assert "X-CloudLens-Pair" in (r.headers.get("Access-Control-Allow-Headers") or "")


def test_preflight_from_a_foreign_origin_grants_nothing():
    r = _handler_response(
        "/run", method="OPTIONS",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Private-Network": "true"})
    assert r.headers.get("Access-Control-Allow-Private-Network") is None
    assert r.headers.get("Access-Control-Allow-Origin") is None


def test_same_origin_post_is_not_rejected_for_its_origin():
    # Per Fetch, Origin is omitted only on a same-origin GET/HEAD. A
    # same-origin POST DOES send it, so app.js's /run and /stop/ arrive
    # carrying Origin: http://localhost:8760. Any pairing exemption that keys
    # on "no Origin header" therefore fails on the primary happy path, on the
    # default port, for the local UI - and hides until the first Run click,
    # because /flows is a GET.
    for origin in ("http://localhost:8760", "http://127.0.0.1:8760"):
        assert server._origin_if_allowed(origin) == origin, \
            "the console's own UI must be recognised by the origin it actually sends"
        r = _handler_response("/run", method="POST", headers={"Origin": origin},
                              body=json.dumps({"flow": "stack", "replay": True}))
        assert r.status == 200, "same-origin POST from our own page must not be rejected"
        assert r.headers.get("Access-Control-Allow-Origin") == origin


def test_self_origins_follow_the_bound_port():
    # --port is user-settable and the docstring advertises it. If the
    # allowlist stays pinned to 8760, the UI's own POST is blocked on any
    # other port.
    saved_allowed, saved_self = set(server.ALLOWED_ORIGINS), set(server.SELF_ORIGINS)
    try:
        server._set_self_origins(8890)
        assert server._origin_if_allowed("http://localhost:8890") == "http://localhost:8890"
        assert server._origin_if_allowed("http://127.0.0.1:8890") == "http://127.0.0.1:8890"
        assert server._origin_if_allowed(PAGES_ORIGIN) == PAGES_ORIGIN, \
            "rebinding the port must never drop the pages origin"
        assert server._origin_if_allowed("https://evil.example") is None
        # SELF_ORIGINS is cleared and refilled, but ALLOWED_ORIGINS is only
        # ever added to, so the previous port stayed allowlisted forever. The
        # allowlist is a standing grant to a browser origin: leaving 8760 on it
        # after moving to 8890 means whatever now answers on 8760 - another
        # user's process on a shared box - can read this console's responses.
        assert server._origin_if_allowed("http://localhost:8760") is None, \
            "re-serving on a new port must retire the old port's origins"
        assert server._origin_if_allowed("http://127.0.0.1:8760") is None
    finally:
        server.ALLOWED_ORIGINS.clear(); server.ALLOWED_ORIGINS.update(saved_allowed)
        server.SELF_ORIGINS.clear(); server.SELF_ORIGINS.update(saved_self)


def test_sse_emits_cors_over_a_real_socket():
    """The SSE path builds its own headers and was missed by the first pass.

    _handler_response deliberately refuses /events/ (it would block on the job
    queue), and stubbing it out is exactly the mistake that hid a wire-level
    bug once already - so this drives a real listener over a real socket and
    reads what actually goes out. http.client returns once the headers are in,
    so the never-ending body does not hang us.
    """
    import http.client
    from threading import Thread

    saved_allowed, saved_self = set(server.ALLOWED_ORIGINS), set(server.SELF_ORIGINS)
    httpd = server.serve("127.0.0.1", 0)          # port 0: never collide with a real console
    port = httpd.server_address[1]
    # This test walks away from a never-ending stream, which RSTs the socket
    # and makes socketserver dump a traceback to stderr. That is the test
    # being rude, not the server misbehaving, so swallow it HERE rather than
    # in the handler - a real reset still surfaces in production.
    httpd.handle_error = lambda request, addr: None
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"flow": "stack", "replay": True})
        c.request("POST", "/run", body=body, headers={
            "Content-Type": "application/json", "Origin": PAGES_ORIGIN,
            # /run is pairing-gated, and that gate is exactly what makes the
            # job id a capability: an unpaired cross-origin caller can never
            # learn one, which is why /events/ itself needs no header.
            "X-CloudLens-Pair": server.PAIR_CODE,
            "Connection": "close"})   # keep-alive would strand the socket at teardown
        job_id = json.loads(c.getresponse().read().decode())["job_id"]
        c.close()

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/events/" + job_id,
                  headers={"Origin": PAGES_ORIGIN, "Connection": "close"})
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type") == "text/event-stream"
        assert r.getheader("Access-Control-Allow-Origin") == PAGES_ORIGIN, \
            "EventSource from the public page is blocked without ACAO on the stream"
        assert "Origin" in (r.getheader("Vary") or "")
        assert r.getheader("Access-Control-Allow-Credentials") is None, \
            "ACAC would hand a hostile origin ambient authority; EventSource does " \
            "not need it without withCredentials"
        c.close()

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/events/" + job_id,
                  headers={"Origin": "https://evil.example", "Connection": "close"})
        r = c.getresponse()
        assert r.getheader("Access-Control-Allow-Origin") is None, \
            "a foreign origin must not be able to read the deploy stream"
        c.close()
    finally:
        httpd.shutdown(); httpd.server_close()
        server.ALLOWED_ORIGINS.clear(); server.ALLOWED_ORIGINS.update(saved_allowed)
        server.SELF_ORIGINS.clear(); server.SELF_ORIGINS.update(saved_self)


def test_non_loopback_host_needs_an_explicit_flag():
    from cloudlens_console import __main__ as M
    assert M.is_loopback("127.0.0.1") and M.is_loopback("localhost") and M.is_loopback("::1")
    for bad in ("0.0.0.0", "", "::", "192.168.1.10", "example.com"):
        assert not M.is_loopback(bad), "%r binds something reachable off-machine" % (bad,)
    # argparse .error() exits: binding the LAN must not be a silent default
    try:
        M.main(["--host", "0.0.0.0", "--no-open"])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("--host 0.0.0.0 was accepted without --allow-remote")


def test_event_contract_roundtrip():
    for ev in (E.hello("1", "arn", "us-east-1"), E.log("hi"), E.state("vpc", E.LIVE, "live"),
               E.narrate("why", "good"), E.stat(created=3, elapsed=9), E.done("ok"),
               E.error("boom", node="kvo", fix="do x")):
        frame = E.to_sse(ev)
        assert frame.startswith("id: ") and "event: " in frame and frame.endswith("\n\n")
        data = json.loads(frame.split("data: ", 1)[1].strip())
        assert data["type"] == ev["type"] and data["id"] == ev["id"]


def test_event_ids_monotonic():
    a, b = E.log("a"), E.log("b")
    assert b["id"] > a["id"]


def test_flow_pattern_matching():
    # a real vpb-adopt line should light the vpb node
    hit = F.match(F.KVO["source"]["patterns"], "[vpb-adopt]   vpb-prod availability: Online")
    assert hit and hit[0] == "vpb" and hit[1] == E.LIVE
    # a mirror session line should light the mirror node
    hit = F.match(F.MIRROR["source"]["patterns"], "[kvo-mirror] CreateTrafficMirrorSession x 3")
    assert hit and hit[0] == "mir"
    # noise matches nothing
    assert F.match(F.SENSORS["source"]["patterns"], "just some unrelated output") is None


def test_all_flows_wellformed():
    assert F.ORDER == ["stack", "sensors", "kvo", "mirror"]
    for fid, flow in F.FLOWS.items():
        assert flow["inputs"] and flow["nodes"] and flow["wires"]
        node_ids = set(flow["nodes"])
        for a, b in flow["wires"]:                       # wires reference real nodes
            assert a in node_ids and b in node_ids
        if flow["source"]["kind"] == "cfn":
            for frag, node in flow["source"]["resource_map"].items():
                assert node in node_ids                  # cfn resource maps to a real node
        else:
            for _, node, status, _, _ in flow["source"]["patterns"]:
                assert node in node_ids
                assert status in (E.BUSY, E.LIVE, E.FAIL, E.GHOST)


def test_fixtures_valid_and_rebuild():
    fx_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))
    for fid in F.ORDER:
        frames = json.load(open(os.path.join(fx_dir, fid + ".json")))
        assert frames, fid
        for fr in frames:
            ev = dict(fr["event"]); t = ev.pop("type"); ev.pop("id", None)
            rebuilt = O._rebuild(t, ev)                  # raises if malformed
            assert rebuilt["type"] == t
        # every fixture ends in a terminal event
        assert frames[-1]["event"]["type"] in (E.DONE, E.ERROR)


def test_pair_code_shape_and_alphabet():
    from cloudlens_console import server
    allowed = set(server.PAIR_ALPHABET)
    assert not (allowed & set("O0I1l")), "alphabet must stay unambiguous to type"
    assert len(server.PAIR_ALPHABET) == len(allowed) == 32, "duplicates would silently cut entropy"
    for _ in range(200):
        c = server.new_pair_code()
        assert len(c) == 8 and set(c) <= allowed
    assert len(server.new_pair_code(10)) == 10
    assert server.new_pair_code() != server.new_pair_code(), "must not be constant"


def _with_pair_code(code, fn):
    """Run fn with a known pairing state, then put the module back.

    The suite is one process and these tests mutate module globals, so a leaked
    PAIR_CODE (or a PAIR_CODE left at None by the cap test) would silently
    change the meaning of every test that runs after it.

    Also zeroes the per-failure delay: it is deliberately half a second in
    production, which would make the cap test take two minutes.
    """
    from cloudlens_console import server
    old = (server.PAIR_CODE, server.PAIR_FAILURES, server.PAIR_WARNED,
           server.PAIR_FAILURE_DELAY)
    server.PAIR_CODE, server.PAIR_FAILURES, server.PAIR_WARNED = code, 0, False
    server.PAIR_FAILURE_DELAY = 0
    try:
        return fn()
    finally:
        (server.PAIR_CODE, server.PAIR_FAILURES, server.PAIR_WARNED,
         server.PAIR_FAILURE_DELAY) = old


def test_run_without_pair_code_is_rejected():
    def body():
        r = _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN, "Content-Length": "0"})
        assert r.status == 401, "an unpaired public page must not start a real AWS deploy"
        assert r.payload["error"] == "pairing required"
    _with_pair_code("ABC23456", body)


def test_run_with_pair_code_is_accepted():
    def body():
        raw = json.dumps({"flow": "stack", "inputs": {}, "replay": True})
        r = _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN,
                                       "X-CloudLens-Pair": "ABC23456"},
                              body=raw)
        assert r.status == 200 and "job_id" in r.payload
    _with_pair_code("ABC23456", body)


def test_wrong_pair_code_is_rejected_on_every_acting_route():
    def body():
        for path, method in (("/flows", "GET"), ("/run", "POST"), ("/stop/deadbeef", "POST")):
            r = _handler_response(path, method=method,
                                  headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                           "X-CloudLens-Pair": "WRONGCOD"})
            assert r.status == 401, "%s must be behind the pairing code" % path
            assert r.payload["error"] == "pairing required"
    _with_pair_code("ABC23456", body)


def test_same_origin_needs_no_pair_code():
    # Two shapes of "this is our own UI": a GET the browser sends with no
    # Origin at all, and a POST that per Fetch DOES carry the loopback origin.
    def body():
        assert _handler_response("/flows").status == 200
        for origin in ("http://localhost:8760", "http://127.0.0.1:8760"):
            r = _handler_response("/run", method="POST", headers={"Origin": origin},
                                  body=json.dumps({"flow": "stack", "replay": True}))
            assert r.status == 200, \
                "%s is the console's own page; pairing must not prompt the local SE" % origin
    _with_pair_code("ABC23456", body)


def test_health_is_never_gated():
    def body():
        for hdrs in ({}, {"Origin": PAGES_ORIGIN},
                     {"Origin": PAGES_ORIGIN, "X-CloudLens-Pair": "WRONGCOD"}):
            assert _handler_response("/health", headers=hdrs).status == 200, \
                "the public page probes /health before the visitor has typed anything"
    _with_pair_code("ABC23456", body)


def test_preflight_is_ungated_and_costs_no_guesses():
    from cloudlens_console import server

    def body():
        r = _handler_response(
            "/run", method="OPTIONS",
            headers={"Origin": PAGES_ORIGIN,
                     "Access-Control-Request-Private-Network": "true"})
        assert r.status == 204, \
            "a preflight cannot carry X-CloudLens-Pair; gating it kills the bridge"
        assert r.headers.get("Access-Control-Allow-Private-Network") == "true"
        assert server.PAIR_FAILURES == 0, \
            "preflights must not burn the guess budget, or a hostile page can DoS " \
            "pairing without ever guessing"
    _with_pair_code("ABC23456", body)


def test_pairing_disables_itself_after_repeated_failures():
    from cloudlens_console import server

    def body():
        for _ in range(server.PAIR_MAX_FAILURES):
            _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                       "X-CloudLens-Pair": "WRONGCOD"})
        assert server.PAIR_CODE is None, "the cap must discard the code"
        r = _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                       "X-CloudLens-Pair": "ABC23456"})
        assert r.status == 403, \
            "even the right code must fail once pairing is disabled, and say so"
        assert r.payload["error"] == "pairing disabled, restart the console"
        # A legitimate local user is never caught by this: same-origin is exempt
        # and never consults the counter.
        assert _handler_response("/flows").status == 200
    _with_pair_code("ABC23456", body)


def test_pair_failures_are_cumulative_across_successes():
    # _paired() runs on every acting request, so resetting on success would let
    # a legitimately paired tab keep zeroing the counter while another grinds.
    from cloudlens_console import server

    def body():
        _handler_response("/run", method="POST",
                          headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                   "X-CloudLens-Pair": "WRONGCOD"})
        assert server.PAIR_FAILURES == 1
        _handler_response("/flows", headers={"Origin": PAGES_ORIGIN,
                                             "X-CloudLens-Pair": "ABC23456"})
        assert server.PAIR_FAILURES == 1, "a success must not clear the grind counter"
    _with_pair_code("ABC23456", body)


def test_non_ascii_pair_header_does_not_raise():
    # hmac.compare_digest raises TypeError on a non-ASCII str, and the header
    # bytes are attacker-controlled: an unhandled 500 here is a free oracle.
    def body():
        r = _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                       "X-CloudLens-Pair": "ABC2345é"})
        assert r.status == 401
    _with_pair_code("ABC23456", body)


def test_foreign_host_header_is_refused():
    # DNS rebinding: evil.com resolves to 127.0.0.1, the browser then calls the
    # console same-origin and sends NO Origin on a GET, which would otherwise
    # be read as "a local process, which already has the shell".
    def body():
        for path, method in (("/health", "GET"), ("/flows", "GET"), ("/run", "POST")):
            r = _handler_response(path, method=method,
                                  headers={"Host": "evil.com:8760", "Content-Length": "0"})
            assert r.status == 403, "%s answered a rebound Host" % path
        assert _handler_response("/health", headers={"Host": "evil.com"}).status == 403
    _with_pair_code("ABC23456", body)


def test_loopback_host_headers_are_accepted():
    for host in ("127.0.0.1", "127.0.0.1:8760", "localhost", "localhost:8890",
                 "[::1]:8760", "LOCALHOST:8760", "localhost."):
        assert _handler_response("/health", headers={"Host": host}).status == 200, \
            "%r is us; refusing it would break the local UI" % host


def test_host_name_parsing():
    # There is no other test of this parser, and every Host decision rests on it.
    hn = server._host_name
    assert hn("127.0.0.1:8760") == "127.0.0.1"
    assert hn("[::1]:8760") == "::1" and hn("[::1]") == "::1"
    assert hn("::1") == "::1", "a bare IPv6 literal has many colons, not a port"
    assert hn("LocalHost.") == "localhost", "host names are case-insensitive (RFC 9110 4.2.3)"
    assert hn(" localhost:8760 ") == "localhost"
    assert hn(None) == "" and hn("") == ""
    # Trailing garbage after the bracket is not a port, it is an attack
    assert hn("[::1]@evil.com") not in server.OUR_HOSTS
    assert hn("[::1].evil.com") not in server.OUR_HOSTS
    assert hn("evil.com:8760") == "evil.com"
    assert hn("127.0.0.1.evil.com") == "127.0.0.1.evil.com"


def test_duplicate_host_header_is_refused():
    # Two Host headers: .get() takes the first, a proxy or the next hop may
    # take the second. Ambiguity here is to be refused, not resolved.
    r = _handler_response("/health", headers=[("Host", "127.0.0.1:8760"),
                                              ("Host", "evil.com")])
    assert r.status == 403


def test_preflight_also_checks_the_host():
    # The preflight is exempt from PAIRING, which is correct, but it is not
    # exempt from being addressed to us.
    r = _handler_response("/run", method="OPTIONS",
                          headers={"Host": "evil.com", "Origin": PAGES_ORIGIN})
    assert r.status == 403
    assert r.headers.get("Access-Control-Allow-Origin") is None


def test_every_response_forbids_framing():
    # A framed console is SAME-ORIGIN with the framing document, so
    # _check_pairing() exempts its POST /run and the Host check passes: one
    # clickjacked click becomes a real AWS deploy, routing around pairing,
    # CORS and Host at once.
    def body():
        cases = [
            ("/health", "GET", {}),                                    # public JSON
            ("/", "GET", {}),                                          # static file path
            ("/flows", "GET", {"Origin": PAGES_ORIGIN}),               # 401 reject path
            ("/run", "POST", {"Origin": PAGES_ORIGIN, "Content-Length": "0"}),
            ("/nope", "GET", {}),                                      # 404 path
        ]
        for path, method, hdrs in cases:
            r = _handler_response(path, method=method, headers=hdrs)
            assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'none'", \
                "%s can be framed" % path
            assert r.headers.get("X-Frame-Options") == "DENY", \
                "%s can be framed by a browser too old for CSP" % path
        r = _handler_response("/run", method="OPTIONS", headers={"Origin": PAGES_ORIGIN})
        assert r.headers.get("X-Frame-Options") == "DENY"
    _with_pair_code("ABC23456", body)


def test_disabled_pairing_is_distinguishable_from_a_wrong_code():
    # Same 401 for both, and the page's copy for it says "that code did not
    # match", so a visitor would retype a correct code forever with no way to
    # learn that the console has stopped accepting any code at all.
    from cloudlens_console import server

    def body():
        r = _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                       "X-CloudLens-Pair": "WRONGCOD"})
        assert r.status == 401 and r.payload["error"] == "pairing required"
        server.PAIR_CODE = None
        r = _handler_response("/run", method="POST",
                              headers={"Origin": PAGES_ORIGIN, "Content-Length": "0",
                                       "X-CloudLens-Pair": "ABC23456"})
        assert r.status == 403, "a disabled console must not look like a typo"
        assert r.payload["error"] == "pairing disabled, restart the console"
    _with_pair_code("ABC23456", body)


def test_each_wrong_guess_costs_real_time():
    # The cap is the backstop; the delay is what makes 40 bits intractable
    # without bricking the console in the middle of a demo.
    assert server.PAIR_FAILURE_DELAY >= 0.5, \
        "without a delay, loopback grinding runs at thousands of guesses a second"
    assert server.PAIR_MAX_FAILURES >= 200, \
        "a small cap turns a handful of stray requests into a restart-the-console outage"


def test_remote_bind_revokes_the_origin_exemption():
    # "no Origin implies local shell implies already trusted" holds only while
    # the bind is loopback. Off-machine callers send no Origin either.
    saved = server.LOOPBACK_ONLY

    def body():
        try:
            server.LOOPBACK_ONLY = False
            assert _handler_response("/flows").status == 401, \
                "with a remote bind, an Origin-less caller is not necessarily local"
            r = _handler_response("/run", method="POST",
                                  headers={"Origin": "http://127.0.0.1:8760",
                                           "Content-Length": "0"})
            assert r.status == 401, "a forged loopback Origin is free over the network"
        finally:
            server.LOOPBACK_ONLY = saved
    _with_pair_code("ABC23456", body)
    assert server.LOOPBACK_ONLY is True, "the default bind is loopback"


def test_serve_sets_loopback_only_from_the_bind_host():
    # The one line in serve() that ties the pairing exemption to the bind. Every
    # other test sets the flag by hand, so without this the line could vanish in
    # a refactor and nothing would notice.
    from cloudlens_console import server
    old = server.LOOPBACK_ONLY
    saved_allowed, saved_self = set(server.ALLOWED_ORIGINS), set(server.SELF_ORIGINS)
    try:
        h = server.serve("0.0.0.0", 0)
        try:
            assert server.LOOPBACK_ONLY is False, \
                "a remote bind MUST revoke the origin exemption: without this line " \
                "the console is an unauthenticated remote deploy endpoint"
        finally:
            h.server_close()
        h = server.serve("127.0.0.1", 0)
        try:
            assert server.LOOPBACK_ONLY is True
        finally:
            h.server_close()
    finally:
        server.LOOPBACK_ONLY = old
        server.ALLOWED_ORIGINS.clear(); server.ALLOWED_ORIGINS.update(saved_allowed)
        server.SELF_ORIGINS.clear(); server.SELF_ORIGINS.update(saved_self)


def test_base_class_error_pages_also_forbid_framing():
    """send_error() never reaches _cors(), so the framing headers cannot live
    there and still be true of every response.

    An unknown method and a malformed request line are both answered by the
    base class, before any do_* method runs, so this needs a real socket.
    """
    import http.client
    from threading import Thread

    saved_allowed, saved_self = set(server.ALLOWED_ORIGINS), set(server.SELF_ORIGINS)
    saved_loopback = server.LOOPBACK_ONLY
    httpd = server.serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    httpd.handle_error = lambda request, addr: None
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("PUT", "/run", headers={"Connection": "close"})   # 501, from the base class
        r = c.getresponse()
        assert r.status == 501
        assert r.getheader("X-Frame-Options") == "DENY", \
            "the base class's own error page is a response too"
        assert r.getheader("Content-Security-Policy") == "frame-ancestors 'none'"
        r.read(); c.close()

        # A request whose REQUEST LINE will not parse is answered as HTTP/0.9:
        # a bare body, no status line and no headers, so there is nothing to
        # attach a framing header to. That is not a gap: no browser can be made
        # to emit a malformed request line - fetch, iframe, img and navigation
        # all send a well-formed HTTP/1.1 line - so the response can never
        # become a framed document, and it carries no controls and no data
        # either. Asserted rather than ignored so that if CPython ever starts
        # answering these with real headers, this test says so.
        import socket
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GARBAGE\r\n\r\n")
        chunks = []
        while True:                      # read to EOF
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
        head = b"".join(chunks).decode("latin-1")
        s.close()
        assert head.startswith("<!DOCTYPE HTML>"), \
            "a parseable request line means headers, and headers must carry XFO"
        assert "\r\n" not in head.split("\n")[0], "an HTTP/0.9 reply has no status line"
    finally:
        httpd.shutdown(); httpd.server_close()
        server.LOOPBACK_ONLY = saved_loopback
        server.ALLOWED_ORIGINS.clear(); server.ALLOWED_ORIGINS.update(saved_allowed)
        server.SELF_ORIGINS.clear(); server.SELF_ORIGINS.update(saved_self)


def test_static_paths_cannot_escape_the_web_dir():
    # startswith() lets a sibling directory named web-anything through.
    assert server._under_web(os.path.join(server.WEB, "app.js"))
    assert server._under_web(server.WEB)
    assert not server._under_web(server.WEB + "-evil/secret.js"), \
        "a web-* sibling is not inside web/"
    assert not server._under_web(os.path.dirname(server.WEB) + "/server.py")
    assert _handler_response("/web/../../server.py").status == 404, \
        "the package source must not be servable"


def test_finished_jobs_are_pruned_after_a_ttl():
    """A job id is a capability, and an un-pruned JOBS makes it a permanent one.

    /events/<id> replays the buffer from the start, and frame one is E.hello
    with the account id, the caller ARN and the region. /health strips exactly
    those to keep them behind pairing - so an id that outlives the pairing code
    (the guess cap sets PAIR_CODE = None for the life of the process) hands
    them back to anyone still holding it.
    """
    saved = dict(server.JOBS)
    try:
        server.JOBS.clear()
        live, fresh, stale = (O.Job("live0", "stack", {}), O.Job("fresh", "stack", {}),
                              O.Job("stale", "stack", {}))
        for j in (live, fresh, stale):
            server.JOBS[j.id] = j
        fresh.emit(E.done("ok"))
        stale.emit(E.done("ok"))
        assert stale.done_at is not None, "finishing a job must record when"
        assert live.done_at is None
        stale.done_at -= server.JOB_TTL + 1
        server._prune_jobs()
        assert "stale" not in server.JOBS, "a finished job past its TTL must not be reachable"
        assert "fresh" in server.JOBS, \
            "the TTL is not zero: a page reload has to be able to reconnect to a finished job"
        assert "live0" in server.JOBS, \
            "a running deploy must never be pruned, however long the stack takes"
    finally:
        server.JOBS.clear(); server.JOBS.update(saved)


def test_expired_job_events_404_over_a_real_socket():
    """The prune has to be visible on the wire, not just in the dict.

    _handler_response refuses /events/ (it tails a live queue), so this drives
    a real listener. The jobs are planted directly rather than run: what is
    under test is the lifetime of the id, not the deploy.
    """
    import http.client
    with _running_server() as (httpd, port):
        for jid in ("expired1", "recent01"):
            job = O.Job(jid, "stack", {})
            job.emit(E.done("ok"))
            server.JOBS[jid] = job
        server.JOBS["expired1"].done_at -= server.JOB_TTL + 1

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/events/expired1", headers={"Connection": "close"})
        r = c.getresponse()
        assert r.status == 404, "an expired job id must be as good as unknown"
        assert json.loads(r.read().decode())["error"] == "no such job"
        c.close()

        # ... and the sweep must not take the job the visitor is still watching
        # with it. http.client returns as soon as the headers are in, so the
        # never-ending stream does not hang the test.
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/events/recent01", headers={"Connection": "close"})
        assert c.getresponse().status == 200
        c.close()


def test_sse_stream_is_delimited_over_a_real_socket():
    """An SSE body has no Content-Length and no chunking, so the only thing
    that says where it ends is the connection closing. Promising keep-alive on
    an HTTP/1.1 connection and then streaming an undelimited body leaves the
    client unable to tell the end of the stream from the start of the next
    response."""
    import http.client
    with _running_server() as (httpd, port):
        job = O.Job("framing1", "stack", {})
        job.emit(E.done("ok"))
        server.JOBS["framing1"] = job
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/events/framing1")     # deliberately NOT Connection: close
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type") == "text/event-stream"
        assert (r.getheader("Connection") or "").lower() == "close", \
            "an undelimited body on a connection we promised to reuse"
        assert r.getheader("Content-Length") is None and r.getheader("Transfer-Encoding") is None
        assert r.will_close, "http.client must know the body ends at EOF"
        c.close()


def test_disconnect_noise_is_swallowed_but_real_errors_are_not():
    """Every closed SSE tab used to print a traceback from socketserver.

    That matters past tidiness: the pairing cap's only observability is a
    warning on this same stderr, and an operator trained to ignore stderr has
    no way to notice that pairing disabled itself.
    """
    import socket
    import struct
    import time as _time
    err = io.StringIO()
    with _running_server(quiet=False) as (httpd, port):
        real_stderr, sys.stderr = sys.stderr, err
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            s.recv(4096)
            # RST, not FIN: an orderly close is a quiet EOF, while a tab that
            # goes away mid-stream resets, and that is the one that printed.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            _time.sleep(0.5)
            assert err.getvalue() == "", \
                "a client disconnect is not a server fault: %r" % err.getvalue()
            # Narrowly, though. A blanket handle_error hides real handler bugs,
            # and this server holds the visitor's AWS identity.
            try:
                raise ValueError("a real handler bug")
            except ValueError:
                httpd.handle_error(None, ("127.0.0.1", 0))
            assert "ValueError" in err.getvalue(), \
                "only BrokenPipe/ConnectionReset may be swallowed"
        finally:
            sys.stderr = real_stderr


def test_banner_url_brackets_an_ipv6_literal():
    # webbrowser.open() gets this string verbatim: 'http://::1:8760/' is not a
    # URL, and the failure is a browser that opens nothing or searches for it.
    from cloudlens_console import __main__ as M
    assert M._banner_url("127.0.0.1", 8760) == "http://localhost:8760/"
    assert M._banner_url("::1", 8760) == "http://[::1]:8760/"
    assert M._banner_url("fe80::1", 9) == "http://[fe80::1]:9/"
    assert M._banner_url("[::1]", 8760) == "http://[::1]:8760/", "already bracketed"
    assert M._banner_url("192.168.1.10", 8801) == "http://192.168.1.10:8801/"


def test_allow_remote_widens_the_host_guard_only_when_asked():
    assert server._remote_host_names("192.168.1.10") == {"192.168.1.10"}
    assert server._remote_host_names("[fe80::1]") == {"fe80::1"}
    # A wildcard bind names nothing, so the guard falls back to the names this
    # machine actually answers to. Without that, --host 0.0.0.0 --allow-remote
    # 403s every LAN client, which is the whole bug.
    assert server._remote_host_names("0.0.0.0"), "a wildcard bind must still name this machine"
    assert server.DEFAULT_HOSTS.isdisjoint({"192.168.1.10"}), "the default guard is loopback only"

    saved_hosts, saved_loopback = server.OUR_HOSTS, server.LOOPBACK_ONLY
    # serve() on port 0 re-points the self-origins at a scratch port, and now
    # that the old port is properly retired that is no longer harmless to leave
    # behind for the next test.
    saved_allowed, saved_self = set(server.ALLOWED_ORIGINS), set(server.SELF_ORIGINS)
    try:
        server.serve("127.0.0.1", 0, allow_remote=True).server_close()
        assert server.OUR_HOSTS == server.DEFAULT_HOSTS, \
            "a loopback bind must never widen the rebinding guard, flag or no flag"
        server.serve("0.0.0.0", 0).server_close()
        assert server.OUR_HOSTS == server.DEFAULT_HOSTS, \
            "the widening is gated on the flag, and serve() is callable without it"
    finally:
        server.OUR_HOSTS, server.LOOPBACK_ONLY = saved_hosts, saved_loopback
        server.ALLOWED_ORIGINS.clear(); server.ALLOWED_ORIGINS.update(saved_allowed)
        server.SELF_ORIGINS.clear(); server.SELF_ORIGINS.update(saved_self)


def test_remote_bind_accepts_its_own_host_and_still_demands_pairing():
    """--allow-remote has to be functional AND still gated.

    Accepting the bind's own Host is the point of the flag. Keeping
    LOOPBACK_ONLY False for the same bind is what stops it becoming an
    unauthenticated remote deploy endpoint: an off-machine caller sends no
    Origin, exactly like a local process, so that exemption cannot survive.
    """
    import http.client
    # The address a caller off this machine would actually dial - discovered
    # here rather than read out of OUR_HOSTS, because asserting that the server
    # answers to a name it made up itself is how this passed while the real LAN
    # client still got 403.
    dial = _offmachine_addr()
    assert dial, "no non-loopback address on this machine to test a remote bind with"

    with _running_server("0.0.0.0", allow_remote=True) as (httpd, port):
        hosthdr = "[%s]:%d" % (dial, port) if ":" in dial else "%s:%d" % (dial, port)

        assert server.LOOPBACK_ONLY is False, \
            "widening the Host guard must not quietly restore the Origin exemption"

        def ask(pair=None, host=hosthdr):
            c = http.client.HTTPConnection(dial, port, timeout=5)
            c.putrequest("GET", "/flows", skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", host)
            if pair:
                c.putheader("X-CloudLens-Pair", pair)
            c.putheader("Connection", "close")
            c.endheaders()
            r = c.getresponse()
            out = (r.status, json.loads(r.read().decode() or "{}"))
            c.close()
            return out

        status, body = ask()
        assert status != 403, "the bind must answer to the address clients dial: %r" % (body,)
        assert (status, body.get("error")) == (401, "pairing required"), \
            "no Origin over the network is not proof of a local process"
        assert ask(server.PAIR_CODE)[0] == 200, \
            "a paired remote caller is exactly what the flag promises"
        assert ask(server.PAIR_CODE, "evil.example:%d" % port)[0] == 403, \
            "widening for our own address must not admit a rebound name"


def test_main_threads_allow_remote_into_serve():
    # Without this wiring the flag parses, prints its scary warning, and does
    # nothing: the Host guard still 403s every caller it was meant to admit.
    from cloudlens_console import __main__ as M
    seen = {}

    class FakeHTTPD:
        def serve_forever(self):
            raise KeyboardInterrupt
        def shutdown(self):
            pass

    def fake_serve(host, port, allow_remote=False):
        seen.update(host=host, port=port, allow_remote=allow_remote)
        return FakeHTTPD()

    old = server.serve
    server.serve = fake_serve
    try:
        M.main(["--host", "127.0.0.1", "--no-open"])
        assert seen["allow_remote"] is False
        M.main(["--host", "0.0.0.0", "--allow-remote", "--no-open"])
        assert seen == {"host": "0.0.0.0", "port": 8760, "allow_remote": True}
    finally:
        server.serve = old


def test_replay_needs_no_boto3(monkeypatch=None):
    # _rebuild + replay path use only stdlib; importing orchestrator must not require boto3
    assert hasattr(O, "run_job") and hasattr(O, "_rebuild")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print("\n%d tests passed" % len(fns))
