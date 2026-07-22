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
    items = list((headers or {}).items())
    if raw_body and content_length and not any(k.lower() == "content-length" for k, _ in items):
        items.append(("Content-Length", str(len(raw_body))))

    class CapturingHandler(server.Handler):
        def __init__(self):
            self.path = path
            self.command = method
            self.requestline = "{} {} HTTP/1.1".format(method, path)
            self.request_version = "HTTP/1.1"
            self.close_connection = True
            self.headers = _make_headers(items)
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
    assert {k.lower() for k, _ in r.headers.items()} <= {
        "server", "date", "content-type", "content-length", "cache-control",
        "access-control-allow-origin", "vary"}
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


def test_replay_needs_no_boto3(monkeypatch=None):
    # _rebuild + replay path use only stdlib; importing orchestrator must not require boto3
    assert hasattr(O, "run_job") and hasattr(O, "_rebuild")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print("\n%d tests passed" % len(fns))
