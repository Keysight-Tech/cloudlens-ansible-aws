"""Loopback-only HTTP server: serves the UI, starts jobs, streams SSE.

Routes (nothing else is exposed):
  GET  /health           -> {ok, version} - liveness probe, no pairing required
  GET  /                 -> the premium UI (web/index.html)
  GET  /flows            -> the four flows as JSON (the UI renders from this)
  POST /run              -> {flow, inputs, replay?}  starts a job, returns {job_id}
  GET  /events/<job_id>  -> Server-Sent Events for that job (supports Last-Event-ID)
  POST /stop/<job_id>    -> cancels a running job

Bind defaults to 127.0.0.1. It is NOT loopback-only by construction: --host
takes any address, and --allow-remote is the deliberate gate in front of a
non-loopback bind. Off-machine callers reach a console that runs real deploys
under the operator's AWS identity, so that gate is load-bearing, not cosmetic.
"""
from __future__ import annotations
import os
import json
import uuid
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events as E
from . import flows as F
from . import orchestrator as O

WEB = os.path.join(os.path.dirname(__file__), "web")
JOBS = {}
FIXTURES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))

# Bumped only when the wire contract the public page depends on changes.
# Stays a plain N.N: a build id or commit SHA here would fingerprint the
# visitor's machine for any unauthenticated prober.
VERSION = "1.0"

# Reachable without a pairing code, by design. Adding to this set means
# accepting that any origin on the machine can read the response.
PUBLIC_PATHS = frozenset({"/health"})

PAGES_ORIGIN = "https://keysight-tech.github.io"

# CORS is defence in depth here, NOT the security boundary. This origin is
# shared by every GitHub Pages site under the Keysight-Tech account, and the
# same-origin policy has no path component, so any page on it can reach this
# console. The real boundary is the pairing code: 40 bits a human types, with
# a guess cap. Never let allowlisted-origin be read as trusted.
#
# Mutable, not frozen: serve() adds the loopback origins for whatever port we
# actually bound. Seeded below for the default port so in-process tests and
# any embedder that never calls serve() still behave.
ALLOWED_ORIGINS = {PAGES_ORIGIN}

# The console's own UI. These exist for one narrow reason: a same-origin POST
# still carries an Origin header (per Fetch, Origin is omitted only on
# same-origin GET/HEAD), so /run and /stop/ from our own page arrive with an
# Origin that has to be recognised. Same-origin GETs never consult this list
# at all - the browser does not apply CORS to them.
SELF_ORIGINS = set()


def _set_self_origins(port):
    """Loopback origins for the console's own UI. Derive from 127.0.0.1 and
    localhost only, never from --host: with --host 0.0.0.0 there is no
    meaningful self-origin a browser would send."""
    SELF_ORIGINS.clear()
    SELF_ORIGINS.update({"http://127.0.0.1:%d" % port, "http://localhost:%d" % port})
    ALLOWED_ORIGINS.update(SELF_ORIGINS)


_set_self_origins(8760)


def _origin_if_allowed(origin):
    """Return the origin if it is on the allowlist, else None. A filter, not a
    predicate. Never a wildcard: this console holds the visitor's AWS
    identity, so `*` would let any site on the internet drive it."""
    return origin if origin in ALLOWED_ORIGINS else None


# Unambiguous alphabet: no O/0, no I/1. The visitor types this by hand.
# ASCII only: compared with hmac.compare_digest.
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_pair_code(n=8):
    """Return a fresh pairing code.

    This is the human-consent gate for cross-origin callers: a public page may
    only drive this console once the visitor has typed the code we printed at
    startup. The code is compared with hmac.compare_digest, so neither the
    alphabet nor the length is free to change - PAIR_ALPHABET must stay ASCII,
    and 8 characters over 32 symbols is the 40 bits of entropy that keeps
    offline grinding from being cheap.
    """
    return "".join(secrets.choice(PAIR_ALPHABET) for _ in range(n))


PAIR_CODE = new_pair_code()   # one per process; re-set by serve()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CloudLensConsole"
    sys_version = ""          # do NOT emit "Python/3.x.y" to an unauthenticated prober

    def log_message(self, *a):  # keep the console quiet
        pass

    # ---- helpers ----
    def _cors(self):
        """Every response path must call this. There are three (_send,
        do_OPTIONS, _sse) and the SSE one was missed once already.

        Vary: Origin is unconditional - a reject-path response that omits it
        can be cached and replayed to an allowed origin (and 204 is
        heuristically cacheable, so the no-store on _send is not a backstop
        that covers do_OPTIONS).

        Deliberately no Access-Control-Allow-Credentials: app.js builds
        EventSource without withCredentials, so a bare ACAO is enough, and
        ACAC would hand a hostile origin ambient authority.
        """
        origin = _origin_if_allowed(self.headers.get("Origin"))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in PUBLIC_PATHS:
            # Readable by ANY origin without a pairing code, because the public
            # page must probe before the visitor has typed anything. So it must
            # leak nothing - no account, region, flow list, hostname or path.
            # Liveness and a wire-contract version, and that is all.
            return self._send(200, {"ok": True, "version": VERSION})
        if path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/flows":
            data = {"order": F.ORDER, "flows": {
                fid: {k: F.FLOWS[fid][k] for k in ("id", "name", "script", "subtitle", "inputs", "nodes", "wires")}
                for fid in F.ORDER}}
            return self._send(200, data)
        if path.startswith("/events/"):
            return self._sse(path.rsplit("/", 1)[-1])
        if path.startswith("/web/"):
            return self._file(path[len("/web/"):], _ctype(path))
        return self._send(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/run":
            b = self._body()
            fid = b.get("flow")
            if fid not in F.FLOWS:
                return self._send(400, {"error": "unknown flow"})
            job_id = uuid.uuid4().hex[:12]
            job = O.Job(job_id, fid, b.get("inputs", {}))
            JOBS[job_id] = job
            replay = None
            if b.get("replay"):
                fx = os.path.join(FIXTURES, "{}.json".format(fid))
                replay = fx if os.path.exists(fx) else None
            threading.Thread(target=O.run_job, args=(job,), kwargs={"replay": replay}, daemon=True).start()
            return self._send(200, {"job_id": job_id, "replay": bool(replay)})
        if path.startswith("/stop/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if job:
                job.stop()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    # ---- CORS preflight ----
    def do_OPTIONS(self):
        origin = _origin_if_allowed(self.headers.get("Origin"))
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self._cors()
        if origin:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # Last-Event-ID is set by the browser itself on EventSource
            # reconnect, not by app.js, so it must be pre-allowed here.
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-CloudLens-Pair, Last-Event-ID")
            # Caches the permission SHAPE (which methods/headers are legal),
            # never authorization. The pairing code is checked per request, so
            # revoking or rotating it takes effect immediately regardless.
            self.send_header("Access-Control-Max-Age", "600")
            # Chrome Private Network Access: a public page reaching 127.0.0.1
            # is blocked without this, and the failure is silent.
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        # No Content-Length on a 204: RFC 9110 section 8.6 forbids it.
        self.end_headers()

    # ---- static file ----
    def _file(self, rel, ctype):
        fp = os.path.normpath(os.path.join(WEB, rel))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ---- SSE ----
    def _sse(self, job_id):
        job = JOBS.get(job_id)
        if not job:
            return self._send(404, {"error": "no such job"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        last = self.headers.get("Last-Event-ID")
        try:
            # replay buffered events after Last-Event-ID (reconnect with no gaps)
            if last is not None:
                try:
                    last = int(last)
                except ValueError:
                    last = 0
                for ev in list(job.buffer):
                    if ev["id"] > last:
                        self.wfile.write(E.to_sse(ev).encode("utf-8"))
                self.wfile.flush()
            else:
                for ev in list(job.buffer):
                    self.wfile.write(E.to_sse(ev).encode("utf-8"))
                self.wfile.flush()
            sent = job.buffer[-1]["id"] if job.buffer else 0
            # live tail
            while True:
                try:
                    ev = job.q.get(timeout=12)
                    if ev["id"] > sent:
                        self.wfile.write(E.to_sse(ev).encode("utf-8"))
                        self.wfile.flush()
                        sent = ev["id"]
                    if ev["type"] in (E.DONE, E.ERROR) and job.done:
                        # allow late per-node errors to flush, then close
                        pass
                except queue.Empty:
                    if job.done:
                        break
                    self.wfile.write(b": keepalive\n\n")  # SSE comment
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def _ctype(path):
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".js"):
        return "application/javascript"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def serve(host="127.0.0.1", port=8760):
    httpd = ThreadingHTTPServer((host, port), Handler)
    # The UI's own POSTs carry an Origin naming the port we actually bound,
    # so the allowlist has to follow --port rather than assume 8760.
    _set_self_origins(httpd.server_address[1])
    return httpd
