"""Loopback-only HTTP server: serves the UI, starts jobs, streams SSE.

Routes (nothing else is exposed):
  GET  /                 -> the premium UI (web/index.html)
  GET  /flows            -> the four flows as JSON (the UI renders from this)
  POST /run              -> {flow, inputs, replay?}  starts a job, returns {job_id}
  GET  /events/<job_id>  -> Server-Sent Events for that job (supports Last-Event-ID)
  POST /stop/<job_id>    -> cancels a running job

Bind is 127.0.0.1 only - the console is never reachable off the machine.
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
VERSION = "1.0"

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
        if path == "/health":
            # First branch on purpose: readable by ANY origin without a pairing
            # code, because the public page must probe before the visitor has
            # typed anything. So it must leak nothing - no account, region,
            # flow list, hostname or path. Liveness and a version, and that is
            # all. Keep any pairing guard added later BELOW this line.
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
    return httpd
