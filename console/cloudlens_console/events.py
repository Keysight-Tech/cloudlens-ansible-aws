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
  phases   - order: every phase name this run can go through, in the script's
             own PHASE_ORDER, space separated, emitted once right after the
             first hello. It is the only way the browser can draw the phases
             still to come; the phase events below only ever report one that
             has already ended.
  phase    - name, status (done|failed|skipped), reason
  resource - kind (vpc|subnet|vcontroller|kvo|vpb|workloads|eks) plus what
             that kind has: id, role, zone, ip, private_ip, ingress_ip,
             egress_ip, count, tag, mode, filter, created, cluster
             (workloads carries count, tag, mode, filter and, when the
             script tagged them itself, created=true)
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
               so Last-Event-ID resumes across both sources without gaps.
               No frame has one until a Job emits it: Job.emit stamps the
               next id under the job's lock, in buffer order, so ids and
               buffer positions agree however many producers interleave
  script_seq - the script's seq: its line number in the file, strictly
               increasing within one file (a smaller one means the file was
               replaced and this is a new stream, not a gap)
  <type>_id  - the script's own "id" field when it had one (prompt_id,
               resource_id). It cannot stay as "id": emit stamps the
               console's id over the frame, so the script's would be lost,
               and until then a lookalike would pass for a resume id.
  ts         - the script's timestamp, untouched
"""
from __future__ import annotations
import json
import os

_HELLO = "hello"
LOG = "log"
STATE = "state"
NARRATE = "narrate"
STAT = "stat"
DONE = "done"
ERROR = "error"

# the script's types (Events v2); hello and done are shared with the console
PHASES, PHASE = "phases", "phase"
RESOURCE, CHECK, PROMPT, LOGIN = "resource", "check", "prompt", "login"
SCRIPT_TYPES = {_HELLO, PHASES, PHASE, RESOURCE, CHECK, PROMPT, LOGIN, DONE}

# node states the UI understands
GHOST = "ghost"   # planned, not yet started (dim outline)
BUSY = "busy"     # creating now (amber pulse)
LIVE = "live"     # created / healthy (green glow, wires flow)
FAIL = "fail"     # failed (red)

def _mk(_type, **data):
    """A frame with no id yet. The id is the job's to give: Job.emit stamps
    it under the job's lock as the frame is appended to the buffer, so two
    producer threads can never mint out of buffer order (they did, when the
    counter lived here, and a resume from the smaller id skipped the larger).
    """
    ev = {"type": _type}
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
    """Serialize one EMITTED event as an SSE frame. The `id:` lets a
    reconnecting browser resume with Last-Event-ID without gaps or
    duplicates; a frame no job has emitted has no id, and sending one would
    be a bug, so this raises (KeyError) rather than invent one."""
    return "id: {id}\nevent: {type}\ndata: {data}\n\n".format(
        id=ev["id"], type=ev["type"], data=json.dumps(ev, separators=(",", ":"))
    )


# ---------------------------------------------------------------- events v2
def from_script(raw):
    """One parsed JSON line from deploy-stack.sh --events as a console event,
    ready for the console's own id (Job.emit stamps it) so SSE resume works
    across both sources. seq becomes script_seq; a script "id" (a prompt's
    "p3", a resource's "vpc-...") becomes <type>_id, because emit writes the
    console id over "id" and the script's would be lost. A frame that
    already went through here (a replay fixture: script_seq, prompt_id) is
    accepted as it is. An unknown type becomes a log of the raw line; so does
    a type that is not a string at all (a list or an object cannot even be
    looked up in SCRIPT_TYPES, and one such line must not end the tail)."""
    data = dict(raw)
    data["script_seq"] = data.pop("seq", data.get("script_seq"))
    typ = data.pop("type", None)
    if not isinstance(typ, str) or typ not in SCRIPT_TYPES:
        return _mk(LOG, text=json.dumps(raw), stream="script", script_seq=data["script_seq"])
    if "id" in data:
        data[typ + "_id"] = data.pop("id")
    return _mk(typ, **data)


def _script_lines(chunk):
    """The complete lines of one read as parsed JSON objects, plus the offset
    one past the last newline. A line that is not a JSON object is dropped
    here; the writer's unterminated last line is not consumed at all."""
    end = chunk.rfind(b"\n") + 1          # one past the last complete line
    raws = []
    for line in chunk[:end].split(b"\n"):
        if not line.strip():
            continue
        try:
            raw = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(raw, dict):
            raws.append(raw)
    return end, raws


def is_seq(value):
    """A seq is an int; bool is an int to Python, but never a seq. The one
    check for both sides: the file's seq here, the tail's watermark in the
    orchestrator."""
    return isinstance(value, int) and not isinstance(value, bool)


def _first_seq(raws):
    """The seq of the first line in a read that carries one."""
    for raw in raws:
        seq = raw.get("seq")
        if is_seq(seq):
            return seq
    return None


def iter_script_events(path, start_offset=0, last_seq=None):
    """Read the script's events file from a byte offset for the tail loop:
    returns (new_offset, [events]), each event a from_script() result.

    Only newline-terminated lines are consumed; the line the writer is still
    on stays for the next call, so the offset never lands mid-line. A
    terminated line that is not a JSON object (the fragment a killed run
    left, which the next run terminates before its own hello) is skipped,
    not read as the end of the stream; so is a line from_script() raises
    on, whatever it raises: one bad line never ends the tail. Bytes that
    are not UTF-8 are replaced (errors="replace"), never fatal. A file
    that is not there yet (the script creates it at startup) reads as
    (0, []).

    A replaced file is a new stream, and the offset alone cannot tell: a
    file shorter than the offset is caught by its size, but one the same
    size or longer is not. The script's contract is the other half: seq is
    the line's number in its file, strictly increasing, so a seq no larger
    than the last one handled means a different file. The caller passes
    the seq of the LAST event it handled as last_seq (the latest, not the
    largest it ever saw: a restarted stream begins at 1 again, and a
    watermark stuck at an old maximum would read every later append as one
    more replacement); when the first line read from the offset carries a
    seq <= last_seq, the file is re-read from 0 and the whole of it comes
    back, every event with a fresh console id (the browser's last-hello
    rule handles the second hello). last_seq=None (a first call) never
    restarts, and neither does a read that already starts at 0; a last_seq
    that is not a seq by is_seq (the same guard _first_seq applies to the
    file's side) is ignored, not compared,
    so a caller that tracked a string seq gets no restart rather than a
    TypeError in its tail thread. What this still cannot see: a replacement
    whose line at the offset already carries a larger seq (more, shorter
    lines than the old file had there) reads as an append, and one the
    reader had fully caught up with is an empty read until the new writer
    reaches the offset."""
    start_offset = max(0, start_offset)
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            if start_offset > size:
                start_offset = 0
            fh.seek(start_offset)
            end, raws = _script_lines(fh.read())
            if start_offset and is_seq(last_seq):
                first = _first_seq(raws)
                if first is not None and first <= last_seq:
                    start_offset = 0
                    fh.seek(0)
                    end, raws = _script_lines(fh.read())
    except FileNotFoundError:
        return 0, []
    events = []
    for raw in raws:
        try:
            events.append(from_script(raw))
        except Exception:
            continue
    return start_offset + end, events
