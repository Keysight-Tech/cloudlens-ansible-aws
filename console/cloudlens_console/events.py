"""The event contract between the deploy orchestrator and the browser.

Every tick the UI renders is one of these events, and every event carries REAL
data - a line of a real subprocess, or a real AWS state transition. Nothing here
fabricates progress. When we are waiting on AWS, we emit a `stat` that says so;
we never invent a `state`.

Event types:
  hello    - first event of a job: real caller identity + region (proof it's live)
  log      - one raw stdout/stderr line from the deploy subprocess
  state    - a real AWS/resource state transition, mapped to a diagram node
  narrate  - a plain-English explanation tied to what just happened, and why
  stat     - a live metric update (elapsed, resources-created, status pill, waiting)
  done     - terminal success, with real outputs (URLs, next command)
  error    - terminal or per-node failure, with the real reason and the fix
"""
from __future__ import annotations
import json
import itertools

_HELLO = "hello"
LOG = "log"
STATE = "state"
NARRATE = "narrate"
STAT = "stat"
DONE = "done"
ERROR = "error"

# node states the UI understands
GHOST = "ghost"   # planned, not yet started (dim outline)
BUSY = "busy"     # creating now (amber pulse)
LIVE = "live"     # created / healthy (green glow, wires flow)
FAIL = "fail"     # failed (red)

_seq = itertools.count(1)


def _mk(_type, **data):
    ev = {"id": next(_seq), "type": _type}
    ev.update(data)
    return ev


def hello(account, arn, region):
    return _mk(_HELLO, account=account, arn=arn, region=region)


def log(text, stream="out"):
    return _mk(LOG, text=text, stream=stream)


def state(node, status, label=None):
    """A real resource transition. `node` is a diagram node id; `status` one of
    GHOST/BUSY/LIVE/FAIL; `label` an optional short status shown under the node."""
    ev = _mk(STATE, node=node, status=status)
    if label is not None:
        ev["label"] = label
    return ev


def narrate(text, tone="info"):
    """tone: info | good | warn | note - drives the accent of the narration line."""
    return _mk(NARRATE, text=text, tone=tone)


def stat(**kv):
    """Live metrics, e.g. stat(elapsed=42, created=6, status='CREATE_IN_PROGRESS',
    waiting=True)."""
    return _mk(STAT, **kv)


def done(summary, outputs=None):
    return _mk(DONE, summary=summary, outputs=outputs or {})


def error(text, node=None, fix=None):
    ev = _mk(ERROR, text=text)
    if node is not None:
        ev["node"] = node
    if fix is not None:
        ev["fix"] = fix
    return ev


def to_sse(ev):
    """Serialize one event as an SSE frame. The `id:` lets a reconnecting browser
    resume with Last-Event-ID without gaps or duplicates."""
    return "id: {id}\nevent: {type}\ndata: {data}\n\n".format(
        id=ev["id"], type=ev["type"], data=json.dumps(ev, separators=(",", ":"))
    )
