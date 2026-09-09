"""Loopback-only HTTP server: serves the UI, starts jobs, streams SSE.

Routes (nothing else is exposed):
  GET  /                         -> the premium UI (web/index.html)
  GET  /flows                    -> the four flows as JSON (the UI renders from this)
  POST /run                      -> {flow, inputs, replay?}  starts a job, returns {job_id}
  GET  /events/<job_id>          -> Server-Sent Events for that job (honours Last-Event-ID)
  POST /stop/<job_id>            -> cancels a running job (also POST /api/stop/<job_id>)

  The operations API (api.py; every route a face on an existing command):
  GET  /api/doctor?region=R                           deploy-stack.sh --doctor
  GET  /api/discover/vpcs?region=R                    aws ec2 describe-vpcs
  GET  /api/discover/subnets?region=R&vpc=V           aws ec2 describe-subnets (+ route tables)
  GET  /api/discover/workloads?region=R&tag=K=V&vpcs= aws ec2 describe-instances
  GET  /api/discover/eks?region=R                     aws eks list-clusters
  POST /api/plan       {plan}                         profile.py render + validation
  POST /api/run        {plan, secrets?, kvo_codes?}   deploy-stack.sh --profile, via run_engine
  POST /api/answer/<job_id>  {prompt_id, text}        job.answer
  POST /api/teardown   {stack, region, confirm_name, orphans_only?, licences_released?}
  POST /api/licences   {action, kvo, user, password, ...}  (also /api/licences/<action>)

Bind is 127.0.0.1 only - the console is never reachable off the machine.
POST routes also check the Host header (a DNS-rebinding page reaches the
loopback address under its own name), the Origin header when a browser
sends one, and require application/json (a cross-site form or text/plain
fetch can post without a preflight; JSON cannot).
"""
from __future__ import annotations
import os
import json
import uuid
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from . import api as A
from . import events as E
from . import flows as F
from . import orchestrator as O

WEB = os.path.join(os.path.dirname(__file__), "web")
JOBS = {}
FIXTURES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))
MAX_BODY = 256 * 1024     # bytes of one POST body
KEEPALIVE_SECS = 12       # SSE comment cadence while a job is quiet
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def host_ok(host):
    """True when a Host (or an Origin's host) names this machine's loopback:
    127.0.0.1 or localhost, with or without a port. Anything else is a page
    that reached the console through a name it controls."""
    if not isinstance(host, str) or not host.strip():
        return False
    h = host.strip().lower()
    if h.startswith("["):
        return False            # the server never binds an IPv6 address
    if ":" in h:
        h, port = h.rsplit(":", 1)
        if not port.isdigit():
            return False
    return h in LOOPBACK_HOSTS


def origin_ok(origin):
    """A browser names the page's origin on a cross-site POST; absent is the
    console's own page (or a non-browser client on this machine)."""
    if origin is None:
        return True
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and host_ok(parts.netloc)


def last_event_id(value):
    """The Last-Event-ID header as an int; junk, negative or absent is 0 (a
    fresh start), never an error: the browser sends what it last saw."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def events_after(job, last_id):
    """The job's buffered events with id > last_id, in order: what a
    reconnecting browser missed. Ids are the buffer order (Job.emit stamps
    them under the job's lock), so this is exact."""
    return job.events_since(last_id)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the console quiet
        pass

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _api(self, result):
        """Send an api.py result: 200, or the error's own code (400 unless
        the dict names one under "http")."""
        code = 200
        if isinstance(result, dict) and ("error" in result or "errors" in result):
            code = result.pop("http", 400)
        return self._send(code, result)

    def _read_json(self):
        """(body, None) for a JSON object under MAX_BODY, else (None, sent):
        the error response has already gone out."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send(415, {"error": "send application/json"})
            return None, True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "bad Content-Length"})
            return None, True
        if n > MAX_BODY:
            # Drain a moderately oversized body so the client reads the 413
            # rather than a reset (closing with unread bytes makes the
            # kernel send RST); an absurd one is not read at all. Either
            # way the connection closes, so no leftover byte is ever parsed
            # as a next request.
            self.close_connection = True
            if n <= 8 * MAX_BODY:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            self._send(413, {"error": "body over %d bytes" % MAX_BODY})
            return None, True
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "body is not JSON"})
            return None, True
        if not isinstance(body, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return None, True
        return body, None

    # ---- GET ----
    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/flows":
            data = {"order": F.ORDER, "flows": {
                fid: {k: F.FLOWS[fid][k] for k in ("id", "name", "script", "subtitle", "inputs", "nodes", "wires")}
                for fid in F.ORDER}}
            return self._send(200, data)
        if path.startswith("/events/"):
            return self._sse(path[len("/events/"):])
        if path.startswith("/web/"):
            return self._file(path[len("/web/"):], _ctype(path))
        if path.startswith("/api/"):
            return self._api_get(path, parse_qs(parts.query, keep_blank_values=True))
        return self._send(404, {"error": "not found"})

    def _api_get(self, path, query):
        q = {k: v[-1] for k, v in query.items()}     # the last value of a repeated key
        region = q.get("region", "")
        if path == "/api/doctor":
            return self._api(A.doctor(region))
        if path == "/api/discover/vpcs":
            return self._api(A.discover_vpcs(region))
        if path == "/api/discover/subnets":
            return self._api(A.discover_subnets(region, q.get("vpc", "")))
        if path == "/api/discover/workloads":
            return self._api(A.discover_workloads(region, q.get("tag", ""), q.get("vpcs", "")))
        if path == "/api/discover/eks":
            return self._api(A.discover_eks(region))
        if path == "/api/licences" or path.startswith("/api/licences/"):
            # the KVO password travels in a body, never a query string
            return self._send(405, {"error": "POST {action: list|check|activate|release, kvo, user, password}: "
                                             "credentials go in the body, never a URL"}, extra={"Allow": "POST"})
        return self._send(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        if not host_ok(self.headers.get("Host")):
            return self._send(403, {"error": "Host must be 127.0.0.1 or localhost: the console is loopback only"})
        if not origin_ok(self.headers.get("Origin")):
            return self._send(403, {"error": "Origin must be this console: it takes no cross-site requests"})
        path = urlsplit(self.path).path
        if path == "/run":
            b, sent = self._read_json()
            if sent:
                return
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
        if path.startswith("/stop/") or path.startswith("/api/stop/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if job:
                job.stop()
            return self._send(200, {"ok": True})
        if path.startswith("/api/"):
            return self._api_post(path)
        return self._send(404, {"error": "not found"})

    def _api_post(self, path):
        if path == "/api/plan":
            b, sent = self._read_json()
            return None if sent else self._api(A.plan(b.get("plan")))
        if path == "/api/run":
            b, sent = self._read_json()
            return None if sent else self._api(A.run(b, jobs=JOBS))
        if path.startswith("/api/answer/"):
            job = JOBS.get(path[len("/api/answer/"):])
            if job is None:
                return self._send(404, {"error": "no such job"})
            b, sent = self._read_json()
            return None if sent else self._api(A.answer(job, b))
        if path == "/api/teardown":
            b, sent = self._read_json()
            return None if sent else self._api(A.teardown(b, jobs=JOBS))
        if path == "/api/licences" or path.startswith("/api/licences/"):
            action = path[len("/api/licences/"):] if path.startswith("/api/licences/") else None
            b, sent = self._read_json()
            return None if sent else self._api(A.licences(b, action=action))
        return self._send(404, {"error": "not found"})

    # ---- static file ----
    def _file(self, rel, ctype):
        fp = os.path.normpath(os.path.join(WEB, rel))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        with open(fp, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ---- SSE ----
    def _sse(self, job_id):
        """Replay what the browser missed (everything after Last-Event-ID,
        or the whole buffer on a first connect), then stream live. Ids are
        the buffer order, so the watermark is simply the last id written;
        an event that is both in the replay and still on the queue is
        dropped by that watermark. A job that is done and whose queue is
        drained ends the stream at once, not after a keepalive timeout."""
        job = JOBS.get(job_id)
        if not job:
            return self._send(404, {"error": "no such job"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # the stream has no length: its end is the connection closing, so
        # this handler must not keep the socket for a next request
        self.close_connection = True
        sent = last_event_id(self.headers.get("Last-Event-ID"))
        try:
            self.wfile.write(b"retry: 2000\n\n")
            for ev in events_after(job, sent):
                self.wfile.write(E.to_sse(ev).encode("utf-8"))
                sent = ev["id"]
            self.wfile.flush()
            while True:
                if job.done and job.q.empty():
                    break
                try:
                    ev = job.q.get(timeout=KEEPALIVE_SECS)
                except queue.Empty:
                    if job.done:
                        break
                    self.wfile.write(b": keepalive\n\n")  # SSE comment
                    self.wfile.flush()
                    continue
                if ev["id"] > sent:
                    self.wfile.write(E.to_sse(ev).encode("utf-8"))
                    self.wfile.flush()
                    sent = ev["id"]
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
    return httpd
