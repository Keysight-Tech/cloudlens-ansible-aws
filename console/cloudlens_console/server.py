"""Loopback-only HTTP server: serves the UI, starts jobs, streams SSE.

Routes (nothing else is exposed). "paired" means a cross-origin caller must
send X-CloudLens-Pair; the console's own UI is exempt:
  GET  /health           -> {ok, version} - liveness probe, open by design
  GET  /                 -> the premium UI (web/index.html)
  GET  /flows            -> paired. the four flows as JSON (the UI renders it)
  POST /run              -> paired. {flow, inputs, replay?} -> {job_id}
  GET  /events/<job_id>  -> Server-Sent Events (Last-Event-ID). NOT header-gated:
                            EventSource cannot send headers, so the gate is that
                            job_id is unguessable and only /run hands one out.
  POST /stop/<job_id>    -> paired. cancels a running job

Every request, preflight included, is refused unless Host names this machine: a
hostile page can resolve its own name to 127.0.0.1, and the browser then treats
it as same-origin and sends no Origin at all, which is exactly what the pairing
exemption keys on. Every response forbids framing for the same reason - a framed
console is same-origin with the framing page, so it inherits that exemption.

Bind defaults to 127.0.0.1. It is NOT loopback-only by construction: --host
takes any address, and --allow-remote is the deliberate gate in front of a
non-loopback bind. Off-machine callers reach a console that runs real deploys
under the operator's AWS identity, so that gate is load-bearing, not cosmetic.
"""
from __future__ import annotations
import os
import hmac
import json
import time
import uuid
import queue
import secrets
import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events as E
from . import flows as F
from . import orchestrator as O

# Absolute: _under_web() compares this with commonpath, which raises on a mix of
# relative and absolute paths. Under an embedder or a zipapp where __file__ is
# relative that raise is caught and every UI file 404s, with no diagnostic.
WEB = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))
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

# Loopback has no network cost, so an unattended page could otherwise grind 40
# bits at thousands of guesses a second. The DELAY is the real defence: half a
# second a guess puts 2^40 far out of reach while costing a human who mistypes
# nothing they would notice. The cap is only the final backstop, and it is
# cumulative for the life of the process - 200 wrong guesses EVER, not 200
# without a legitimate request interleaving.
#
# The threshold is high on purpose. A small cap is itself a denial of service:
# any page that can reach the port can spend the whole budget and force the
# visitor to restart the console in the middle of a demo. Note the browser is
# the attacker here, and it holds ~6 connections per origin, so the delay caps
# a hostile page at ~12 guesses a second: 2^40 takes some 2,900 years, and only
# ~6 handler threads are ever parked in the sleep.
#
# The brick is MITIGATED, not removed: at 6 parallel connections a hostile page
# still reaches 200 failures in about 17 seconds, and the outcome is a restart
# mid-demo. The delay alone already makes grinding hopeless, so if this ever
# bites in practice, an escalating delay capped at a few seconds does both
# remaining jobs without ever killing pairing permanently.
#
# The budget is deliberately GLOBAL. Counting per origin looks fairer - it would
# stop a hostile page from spending the honest visitor's budget - but Origin is
# chosen by the attacker, so a per-origin counter is a per-origin bypass: vary
# the header and the cap never fires. One global budget is the only version that
# actually bounds anything.
PAIR_FAILURES = 0
PAIR_MAX_FAILURES = 200
PAIR_FAILURE_DELAY = 0.5      # seconds; occupies a handler thread, which is the point
PAIR_WARNED = False

# True until serve() is asked for a bind that is reachable off-machine. The
# "no Origin means a local process, which already has the shell" exemption in
# _check_pairing() is sound ONLY under this. Enabling a remote bind without
# replacing that exemption turns this console into an unauthenticated remote
# deploy endpoint for anyone who can reach the port, and makes a forged
# "Origin: http://127.0.0.1:8760" from curl a complete bypass.
LOOPBACK_ONLY = True

# Host values that mean "this machine's own console". Anything else is a name
# that resolved to us, i.e. DNS rebinding.
OUR_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback(host):
    """True only for addresses that cannot be reached from another machine.

    An empty host means INADDR_ANY, and 0.0.0.0 / :: are wildcards: all three
    bind every interface, so none of them is loopback. Anything that is not a
    literal IP (a hostname or interface name) is treated as remote, because we
    cannot know what it resolves to at bind time.
    """
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_name(raw):
    """The host part of a Host header: lower-cased, port and IPv6 brackets
    removed, one trailing root dot dropped.

    Not a plain split(':')[0]: that yields '' for '::1' and '[' for
    '[::1]:8760', so an IPv6 console would refuse its own UI. Anything after
    the closing bracket other than a port is returned intact so it fails the
    OUR_HOSTS lookup - '[::1]@evil.com' is an attack, not an address.
    """
    raw = (raw or "").strip().lower()
    if raw.startswith("["):                       # [::1] or [::1]:8760
        inner, _, rest = raw[1:].partition("]")
        if rest and not rest.startswith(":"):
            return raw                            # garbage after the bracket
        raw = inner
    elif raw.count(":") == 1:                     # host:port
        raw = raw.split(":", 1)[0]
    if raw.endswith(".") and not raw.endswith(".."):
        raw = raw[:-1]                            # 'localhost.' is the same host
    return raw


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
        # A request addressed to a rebound name is granted nothing at all, not
        # even a read of the refusal. Checked here rather than at each reject
        # site: this is the one function every response path already goes
        # through, which is how the SSE path was missed once.
        origin = (_origin_if_allowed(self.headers.get("Origin"))
                  if self._host_is_ours() else None)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def end_headers(self):
        """The anti-framing headers live here, not in _cors().

        A framed console is same-origin with the framing document, so the
        pairing exemption applies to it and the Host check passes - the browser
        still sends Host: 127.0.0.1:8760. app.js runs a flow on one click with
        defaults pre-filled, so a clickjack overlay turns a stray click into a
        real AWS deploy, around pairing, origin pinning and the Host check at
        once. CSP for current browsers, XFO for the rest.

        In end_headers because EVERY response funnels through it, including the
        base class's send_error() pages (501 on an unknown method, 400 on a
        malformed request line), which never reach _cors() at all. Putting it
        here makes "no response can be framed" true by construction rather than
        by remembering.
        """
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _host_is_ours(self):
        """DNS rebinding: a hostile page can point its own name at 127.0.0.1, at
        which point the browser calls it same-origin, sends no Origin on a GET,
        and the absent-Origin exemption stops meaning 'local process'.

        Fails closed on a missing Host, and on a repeated one: with two Host
        headers we and any intermediary may disagree about which is real, and
        that ambiguity is to be refused, not resolved (RFC 9112 3.2).
        """
        seen = self.headers.get_all("Host") or []
        if len(seen) != 1:
            return False
        return _host_name(seen[0]) in OUR_HOSTS

    def _check_pairing(self):
        """None when the caller may act, else the (status, body) to send.

        Not a predicate despite the shape of the question: it spends time,
        counts failures, can disable pairing and prints. Named accordingly.

        Requests from the console's own UI are exempt - they arrive either with
        no Origin at all (same-origin GET) or with a loopback origin
        (same-origin POST; per Fetch, Origin is omitted only on same-origin
        GET/HEAD). Everything else must present the code this process printed.

        Absence of Origin is NOT proof of same-origin. curl and local scripts
        omit it, and so does any cross-origin no-cors subresource GET - a
        hostile page's <script src="http://127.0.0.1:8760/flows"> reaches this
        handler with no Origin at all. CORS stops it READING the response, and
        /flows is not secret, so today that is harmless. It will not stay
        harmless for the next route put behind this gate: anything with a side
        effect must be a POST, which always carries an Origin.
        """
        global PAIR_FAILURES, PAIR_CODE, PAIR_WARNED
        origin = self.headers.get("Origin")
        # The exemption rests on "local code already has the shell and the AWS
        # identity, so pairing adds nothing". Off-machine callers send no
        # Origin either, so it is false the moment the bind is not loopback.
        if (not origin or origin in SELF_ORIGINS) and LOOPBACK_ONLY:
            return None
        supplied = self.headers.get("X-CloudLens-Pair") or ""
        # compare_digest raises TypeError on non-ASCII str, and a hostile page
        # can put any bytes in a header. Normalise before comparing.
        if not supplied.isascii():
            supplied = ""
        if PAIR_CODE is None:
            # Distinct from a mismatch on purpose. Both used to answer 401
            # "pairing required", and the page's copy for that is "that code
            # did not match" - so a visitor whose console had already disabled
            # itself would retype a correct code forever, with the only signal
            # on a stdout they are not watching.
            return 403, {"error": "pairing disabled, restart the console"}
        if hmac.compare_digest(supplied, PAIR_CODE):
            return None
        # The cost of a guess. Loopback has no network latency to borrow, so
        # this is the only thing that makes 2^40 expensive. It occupies a
        # handler thread, which is affordable on ThreadingHTTPServer for a
        # loopback service and is precisely the point.
        if PAIR_FAILURE_DELAY:
            time.sleep(PAIR_FAILURE_DELAY)
        # Cumulative, never reset: this runs on every acting request, so
        # zeroing on success would let a paired tab keep clearing the counter
        # while a hostile tab grinds.
        PAIR_FAILURES += 1
        # Warn on the transition only, via a flag rather than == the threshold:
        # two threads can step over an exact value and the warning would never
        # print, and an unflagged condition stays true forever and buries the
        # live deploy output the cap exists to keep readable.
        if PAIR_FAILURES >= PAIR_MAX_FAILURES and not PAIR_WARNED:
            PAIR_CODE, PAIR_WARNED = None, True
            # flush: stdout is block-buffered whenever the console is piped or
            # redirected, and a warning that sits in a buffer until exit is not
            # a warning.
            print("\n  !! %d bad pairing attempts, pairing disabled."
                  " Restart the console to pair again.\n" % PAIR_FAILURES, flush=True)
        return 401, {"error": "pairing required"}

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
        if not self._host_is_ours():
            return self._send(403, {"error": "bad host"})
        if path in PUBLIC_PATHS:
            # Readable by ANY origin without a pairing code, because the public
            # page must probe before the visitor has typed anything. So it must
            # leak nothing - no account, region, flow list, hostname or path.
            # Liveness and a wire-contract version, and that is all.
            return self._send(200, {"ok": True, "version": VERSION})
        if path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/flows":
            deny = self._check_pairing()
            if deny is not None:
                return self._send(*deny)
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
        if not self._host_is_ours():
            return self._send(403, {"error": "bad host"})
        # Every POST route acts, so gate before the body is even read.
        # do_OPTIONS deliberately does NOT flow through here: a preflight
        # cannot carry X-CloudLens-Pair, and must not spend a guess either.
        deny = self._check_pairing()
        if deny is not None:
            return self._send(*deny)
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
        # Exempt from PAIRING (a preflight cannot carry the header, and must
        # not spend a guess), never exempt from being addressed to us.
        if not self._host_is_ours():
            return self._send(403, {"error": "bad host"})
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
        if not _under_web(fp) or not os.path.isfile(fp):
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


def _under_web(fp):
    """True when fp is WEB itself or inside it.

    startswith(WEB) is not containment: it also accepts a sibling directory
    whose name merely starts with 'web', so a 'web-backup' left next to the
    package would be servable.
    """
    try:
        return os.path.commonpath([os.path.abspath(fp), WEB]) == WEB
    except ValueError:      # different drives on Windows
        return False


def _ctype(path):
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".js"):
        return "application/javascript"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def serve(host="127.0.0.1", port=8760):
    global LOOPBACK_ONLY
    # Set BEFORE the constructor binds, so there is no window in which the
    # socket is listening while the flag still says loopback. Recorded rather
    # than merely warned about: _check_pairing() exempts callers that send no
    # Origin because local code already has the shell and the AWS identity, and
    # that is true only while nothing off-machine can reach us.
    LOOPBACK_ONLY = is_loopback(host)
    httpd = ThreadingHTTPServer((host, port), Handler)
    # The UI's own POSTs carry an Origin naming the port we actually bound,
    # so the allowlist has to follow --port rather than assume 8760.
    _set_self_origins(httpd.server_address[1])
    return httpd
