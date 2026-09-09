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

Events v2: the script's own side channel.
  deploy-stack.sh --events FILE appends one JSON object per line,
  {"seq":N,"ts":"...","type":T,...}, at the points where the script already
  knows the truth. from_script() turns one parsed line into a console event
  and iter_script_events() reads the file the way a tail loop has to. The
  types, and what each carries:
  hello    - stack, region, dry_run. The console emits its own hello (account,
             arn, region) before the script starts, so one job carries two
             hellos of different shape: the LAST hello is authoritative for
             display.
  phase    - name, status (done|failed|skipped), reason
  resource - kind (vpc|subnet|vcontroller|kvo|vpb|workloads|eks) plus what
             that kind has: id, role, zone, ip, private_ip, ingress_ip,
             egress_ip, count, tag, cluster, mode
  check    - item, status (pass|warn|fail), fix                  (--doctor)
  prompt   - question, default, kind (text|secret); the script's own id
             ("p3", what the prompt pipe answers to) arrives as prompt_id
  login    - component (vcontroller|kvo|vpb), url, user, password_in: WHERE
             the password lives, never the password
  done     - status (ok|failed|interrupted|declined), phase, reason, code,
             mode, report, profile. The same type as the console's own done,
             which carries summary/outputs instead; a renderer checks which.
  A type this module does not know arrives as a log whose text is the raw
  line, so nothing the script says is dropped on the floor.

  What every script event carries once it is a console event:
  id         - the console's own counter, the one every console event uses,
               so Last-Event-ID resumes across both sources without gaps
  script_seq - the script's seq: its line number in the file, strictly
               increasing within one file (a smaller one means the file was
               replaced and this is a new stream, not a gap)
  <type>_id  - the script's own "id" field when it had one (prompt_id,
               resource_id). It cannot stay as "id": _mk applies the data
               over {"id": counter}, so it would replace the resume id.
  ts         - the script's timestamp, untouched
"""
from __future__ import annotations
import json
import itertools
import os

_HELLO = "hello"
LOG = "log"
STATE = "state"
NARRATE = "narrate"
STAT = "stat"
DONE = "done"
ERROR = "error"

# the script's types (Events v2); hello and done are shared with the console
PHASE, RESOURCE, CHECK, PROMPT, LOGIN = "phase", "resource", "check", "prompt", "login"
SCRIPT_TYPES = {_HELLO, PHASE, RESOURCE, CHECK, PROMPT, LOGIN, DONE}

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


# ---------------------------------------------------------------- events v2
def from_script(raw):
    """One parsed JSON line from deploy-stack.sh --events as a console event,
    re-stamped with the console's own id so SSE resume works across both
    sources. seq becomes script_seq; a script "id" (a prompt's "p3", a
    resource's "vpc-...") becomes <type>_id, because _mk applies the data
    over the console id and the script's would replace it. A frame that
    already went through here (a replay fixture: script_seq, prompt_id) is
    accepted as it is. An unknown type becomes a log of the raw line."""
    data = dict(raw)
    data["script_seq"] = data.pop("seq", data.get("script_seq"))
    typ = data.pop("type", None)
    if typ not in SCRIPT_TYPES:
        return _mk(LOG, text=json.dumps(raw), stream="script", script_seq=data["script_seq"])
    if "id" in data:
        data[typ + "_id"] = data.pop("id")
    return _mk(typ, **data)


def iter_script_events(path, start_offset=0):
    """Read the script's events file from a byte offset for the tail loop:
    returns (new_offset, [events]), each event a from_script() result.

    Only newline-terminated lines are consumed; the line the writer is still
    on stays for the next call, so the offset never lands mid-line. A
    terminated line that is not a JSON object (the fragment a killed run
    left, which the next run terminates before its own hello) is skipped,
    not read as the end of the stream. Bytes that are not UTF-8 are replaced
    (errors="replace"), never fatal. A file that is not there yet (the
    script creates it at startup) or shorter than the offset (replaced)
    reads as a new stream from 0."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            if start_offset > size:
                start_offset = 0
            fh.seek(start_offset)
            chunk = fh.read()
    except FileNotFoundError:
        return 0, []
    end = chunk.rfind(b"\n") + 1          # one past the last complete line
    events = []
    for line in chunk[:end].split(b"\n"):
        if not line.strip():
            continue
        try:
            raw = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(raw, dict):
            events.append(from_script(raw))
    return start_offset + end, events
