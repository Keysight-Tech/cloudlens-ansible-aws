"""Unit tests for the live console - pure logic, no AWS, no server.
Run:  cd console && python3 -m pytest tests -q
      (or, without pytest: cd console && PYTHONPATH=. python3 tests/test_console.py;
      conftest.py puts console/ on sys.path only under pytest)
"""
import os
import re
import sys
import json

import pytest

from cloudlens_console import events as E, flows as F, orchestrator as O  # noqa


def _emitted(*evs):
    """The frames as a job's stream carries them: a frame has no id until a
    Job emits it (emit stamps the next one under the job's lock)."""
    job = O.Job("t", "stack", {})
    for ev in evs:
        assert "id" not in ev, "no frame carries an id before it is emitted"
        job.emit(ev)
    return evs


def test_event_contract_roundtrip():
    for ev in _emitted(E.hello("1", "arn", "us-east-1"), E.log("hi"), E.state("vpc", E.LIVE, "live"),
                       E.narrate("why", "good"), E.stat(created=3, elapsed=9), E.done("ok"),
                       E.error("boom", node="kvo", fix="do x")):
        frame = E.to_sse(ev)
        assert frame.startswith("id: ") and "event: " in frame and frame.endswith("\n\n")
        data = json.loads(frame.split("data: ", 1)[1].strip())
        assert data["type"] == ev["type"] and data["id"] == ev["id"]
    # an un-emitted frame has no place in a stream: to_sse refuses it
    with pytest.raises(KeyError):
        E.to_sse(E.log("never emitted"))


def test_event_ids_monotonic():
    a, b = _emitted(E.log("a"), E.log("b"))
    assert b["id"] > a["id"]
    # per job, from 1: the id is the buffer position
    assert (a["id"], b["id"]) == (1, 2)


# the keys GET /flows serves and build_site.py projects, which are now the
# only two readers of this module. A flow that grew a key neither of them
# reads is a flow with something in it that nothing draws.
FLOW_KEYS = {"id", "name", "script", "subtitle", "inputs", "nodes", "wires"}


def test_all_flows_wellformed():
    assert F.ORDER == ["stack", "sensors", "kvo", "mirror"]
    for fid, flow in F.FLOWS.items():
        assert set(flow) == FLOW_KEYS, "%s: %s" % (fid, sorted(set(flow) ^ FLOW_KEYS))
        assert flow["id"] == fid and flow["name"] and flow["script"] and flow["subtitle"]
        assert flow["inputs"] and flow["nodes"] and flow["wires"]
        for field in flow["inputs"]:
            assert set(field) == {"key", "label", "default", "placeholder"}, field
        node_ids = set(flow["nodes"])
        for node in flow["nodes"].values():
            assert set(node) == {"x", "y", "ic", "lab", "sub"}, node
        for a, b in flow["wires"]:                       # wires reference real nodes
            assert a in node_ids and b in node_ids


def test_the_fixtures_are_frames_the_published_page_can_replay():
    """The captured fixtures are what docs/console.html replays, and its
    player (build_site.CLIENT_APP) reads them directly: a frame it does
    not know is a frame it silently drops, and a fixture that never ends
    is a run that never says it finished.

    This used to go through orchestrator._rebuild, because the console
    had a server-side replay route. That route and its player are gone
    with the quick flows, so the fixtures are held to the contract of the
    one thing that still reads them."""
    import build_site

    fx_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fixtures"))
    played = set(re.findall(r'ev\.type===?"(\w+)"', build_site.CLIENT_APP))
    assert played == {E.LOG, E.STATE, E.NARRATE, E.STAT, E.DONE}, sorted(played)
    for fid in F.ORDER:
        frames = json.load(open(os.path.join(fx_dir, fid + ".json")))
        assert frames, fid
        for fr in frames:
            assert isinstance(fr.get("_delay", 0), (int, float)), fr
            ev = fr["event"]
            assert ev["type"] in played, "%s: %s is a frame the page cannot draw" % (fid, ev["type"])
            if ev["type"] == E.STATE:
                assert ev["node"] in F.FLOWS[fid]["nodes"], ev
                assert ev["status"] in (E.GHOST, E.BUSY, E.LIVE, E.FAIL), ev
            if ev["type"] == E.DONE:
                assert ev.get("summary"), ev
        # every fixture ends by saying the run finished, or the page keeps
        # its clock running and its pill on "running" for ever
        assert frames[-1]["event"]["type"] == E.DONE, fid


def test_engine_subprocess_has_no_controlling_tty():
    """deploy-stack.sh re-attaches /dev/tty whenever stdin is not a terminal,
    then treats the run as interactive. Launched from a console that was
    itself started in a terminal, the deploy inherited that terminal and
    stopped on "Proceed with this plan? [Y/n]" with every input supplied.
    A new session has no controlling terminal to re-attach, so every ask()
    goes to the prompt pipe instead; DEVNULL keeps the raw reads from
    blocking on it. run_engine is the only place a deploy is started from
    now, so it is the only place this rule has to hold.
    """
    import inspect
    src = inspect.getsource(O.run_engine)
    assert "start_new_session=True" in src
    assert "stdin=subprocess.DEVNULL" in src


# ------------------------------------------------------------ events v2
# deploy-stack.sh --events FILE writes one JSON object per line, {"seq":N,
# "ts":"...","type":T,...} with T in hello/phase/resource/check/prompt/login/
# done. Task 6 tails that file into the job stream through E.from_script and
# E.iter_script_events; these tests hold both to the file as the script
# writes it (deploy/deploy-stack.sh, the comment above emit_event).

SCRIPT_TS = "2026-09-08T10:00:00Z"


def _script_line(seq, typ, **fields):
    raw = {"seq": seq, "ts": SCRIPT_TS, "type": typ}
    raw.update(fields)
    return raw


def test_script_events_pass_through_with_console_ids():
    raw = {"seq": 7, "ts": "2026-09-08T10:00:00Z", "type": "phase", "name": "stack", "status": "done"}
    ev = E.from_script(raw)
    assert ev["type"] == "phase" and ev["name"] == "stack" and ev["script_seq"] == 7
    assert "id" not in ev, "the console's id is the job's to stamp, at emit"
    _emitted(ev)
    assert isinstance(ev["id"], int)   # the console's own monotonic id for SSE resume


def test_unknown_script_type_becomes_a_log_of_the_raw_line():
    raw = _script_line(3, "mystery", extra="x")
    assert "mystery" not in E.SCRIPT_TYPES
    ev = E.from_script(raw)
    assert ev["type"] == E.LOG
    assert json.loads(ev["text"]) == raw, "the raw JSON, not a summary of it"
    assert ev["script_seq"] == 3, "still a script line: the tail loop tracks seq on every one"


def test_every_script_type_round_trips_through_sse():
    samples = {
        "hello": dict(stack="st", region="us-east-1", dry_run="false"),
        E.PHASES: dict(order="stack wait bootstrap"),
        E.PHASE: dict(name="stack", status="done", reason=""),
        E.RESOURCE: dict(kind="vpc", id="vpc-0abc"),
        E.CHECK: dict(item="aws cli", status="pass"),
        E.PROMPT: dict(id="p1", question="Deploy KVO?", default="y", kind="text"),
        E.LOGIN: dict(component="kvo", url="https://1.2.3.4/", user="admin", password_in="creds file"),
        E.DONE: dict(status="ok"),
    }
    assert set(samples) == E.SCRIPT_TYPES, "a script type with no sample here"
    for seq, (typ, fields) in enumerate(sorted(samples.items()), start=11):
        ev, = _emitted(E.from_script(_script_line(seq, typ, **fields)))
        frame = E.to_sse(ev)
        assert frame.startswith("id: %d\n" % ev["id"]) and ("event: %s\n" % typ) in frame
        data = json.loads(frame.split("data: ", 1)[1].strip())
        assert data["type"] == typ and data["script_seq"] == seq and data["id"] == ev["id"]


def test_script_ts_is_preserved():
    ev = E.from_script(_script_line(1, E.CHECK, item="x", status="warn", fix="y"))
    assert ev["ts"] == SCRIPT_TS and ev["fix"] == "y"


def test_script_done_keeps_its_status():
    # The console's own done says summary/outputs; the script's says how the
    # run ended. Both are type "done", so the status must survive as-is.
    ev = E.from_script(_script_line(9, "done", status="failed", phase="stack", reason="boom"))
    assert ev["type"] == E.DONE
    assert ev["status"] == "failed" and ev["phase"] == "stack" and ev["reason"] == "boom"


def test_script_prompt_keeps_its_id_and_the_console_keeps_its_own():
    # emit stamps the console's id over "id", so a script "id" left in place
    # would be lost (and, until emit, "p3" would pass for a resume id).
    raw = _script_line(5, "prompt", id="p3", question="Deploy KVO?", default="y", kind="text")
    ev = E.from_script(raw)
    assert "id" not in ev, "a script id must never pass for the SSE resume id"
    assert ev["prompt_id"] == "p3"
    assert ev["question"] == "Deploy KVO?" and ev["kind"] == "text" and ev["default"] == "y"
    ev, after = _emitted(ev, E.log("after"))
    assert isinstance(ev["id"], int) and ev["prompt_id"] == "p3"
    assert after["id"] > ev["id"], "the counter kept going"


def test_script_resource_id_is_not_the_sse_id_either():
    # prompt is not the only script type carrying "id": a resource's id is
    # the AWS id (emit_event resource kind=vpc id=...). One rule, <type>_id.
    ev = E.from_script(_script_line(6, "resource", kind="vpc", id="vpc-0abc"))
    assert "id" not in ev and ev["resource_id"] == "vpc-0abc"
    assert ev["kind"] == "vpc"
    _emitted(ev)
    assert isinstance(ev["id"], int) and ev["resource_id"] == "vpc-0abc"


def test_iter_script_events_skips_junk_and_holds_the_offset(tmp_path):
    # The file as a killed-and-resumed run leaves it: a fragment the next run
    # terminated (line 1, never valid), a line that is JSON but no event, a
    # hello whose stack name is two bytes for one character, a resource whose
    # tag holds a byte that is not UTF-8, a line whose type is not even a
    # string, and an unterminated line the writer is still on.
    path = str(tmp_path / "events.jsonl")
    lines = [
        b'{"seq":1,"ts":"2026-09-08T10:00:00Z","ty\n',
        b'42\n',
        b'{"seq":2,"ts":"2026-09-08T10:00:00Z","type":"hello","stack":"caf\xc3\xa9","region":"us-east-1","dry_run":"false"}\n',
        b'{"seq":3,"ts":"2026-09-08T10:00:00Z","type":"resource","kind":"workloads","count":"2","tag":"a\xffb"}\n',
        b'{"seq":9,"ts":"t","type":["hello"]}\n',
    ]
    tail = b'{"seq":4,"ts":"2026-09-08T10:00:00Z","type":"phase","name":"stack"'
    with open(path, "wb") as fh:
        fh.write(b"".join(lines) + tail)

    offset, evs = E.iter_script_events(path)
    assert [e["type"] for e in evs] == ["hello", "resource", "log"], evs
    assert [e["script_seq"] for e in evs] == [2, 3, 9]
    assert evs[0]["stack"] == "café", "a multibyte character arrives whole"
    assert evs[1]["tag"] == "a�b", "a non-UTF-8 byte is replaced, never fatal"
    assert evs[2]["stream"] == "script" and json.loads(evs[2]["text"])["type"] == ["hello"], \
        "a type that is not a string is a log of the raw line, not a TypeError"
    assert all("id" not in e for e in evs), "ids are the job's, stamped at emit"
    consumed = b"".join(lines)
    assert len(consumed) != len(consumed.decode("utf-8", "replace")), \
        "bytes and characters differ here, so the next line settles which one the offset counts"
    assert offset == len(consumed), "a BYTE offset, and the unterminated tail waits for the next read"

    # the writer finishes the line: only the new line comes back, nothing repeats
    with open(path, "ab") as fh:
        fh.write(b',"status":"done","reason":""}\n')
    offset2, evs2 = E.iter_script_events(path, offset)
    assert [e["script_seq"] for e in evs2] == [4] and evs2[0]["type"] == E.PHASE
    assert offset2 == os.path.getsize(path)
    assert E.iter_script_events(path, offset2) == (offset2, [])

    # a file shorter than the offset was replaced: read it as a new stream
    with open(path, "wb") as fh:
        fh.write(lines[2])
    offset3, evs3 = E.iter_script_events(path, offset2)
    assert offset3 == len(lines[2]) and [e["script_seq"] for e in evs3] == [2]

    # a file that is not there yet (the script creates it at startup) is an
    # empty read that starts from the beginning once it appears; an offset
    # below zero is a caller bug read as 0, not an OSError from seek
    assert E.iter_script_events(str(tmp_path / "nope.jsonl"), 5) == (0, [])
    offset4, evs4 = E.iter_script_events(path, -7)
    assert offset4 == offset3 and [e["script_seq"] for e in evs4] == [2]


def test_one_line_from_script_chokes_on_never_ends_the_read(tmp_path, monkeypatch):
    # iter_script_events is the tail thread's whole read. If from_script
    # raises on one line (a shape nobody foresaw), that line is skipped and
    # the rest of the read still arrives; the thread never dies on it.
    path = str(tmp_path / "events.jsonl")
    with open(path, "wb") as fh:
        for seq, name in ((1, "stack"), (2, "poison"), (3, "kvo")):
            fh.write(json.dumps(_script_line(seq, E.PHASE, name=name, status="done")).encode() + b"\n")
    real = E.from_script

    def poisoned(raw):
        if raw.get("name") == "poison":
            raise RuntimeError("a from_script bug on this line")
        return real(raw)
    monkeypatch.setattr(E, "from_script", poisoned)
    offset, evs = E.iter_script_events(path)
    assert [e["script_seq"] for e in evs] == [1, 3], "the poison line alone is missing"
    assert offset == os.path.getsize(path), "and the read still consumed the whole file"


# The script's seq is the line's number in its file, so a seq no larger than
# the last one handled means the file was replaced (deploy-stack.sh, the
# comment above emit_event). These four hold iter_script_events to that: a
# replaced file the size check cannot see is caught by seq, and a genuine
# append never is.
def _padded(seq, typ, width, **fields):
    """One script line padded to exactly `width` bytes (newline included),
    so a test can make two files the same size on purpose."""
    line = json.dumps(_script_line(seq, typ, pad="", **fields), separators=(",", ":"))
    assert len(line) + 1 <= width, (len(line), width)
    line = line.replace('"pad":""', '"pad":"%s"' % ("x" * (width - len(line) - 1)))
    out = line.encode() + b"\n"
    assert len(out) == width
    return out


def test_a_same_size_replacement_is_caught_by_seq_not_size(tmp_path):
    path = str(tmp_path / "events.jsonl")
    old = [_padded(1, "hello", 100, stack="st"), _padded(2, E.PHASE, 100, name="stack"),
           _padded(3, E.PHASE, 100, name="kvo")]
    with open(path, "wb") as fh:
        fh.write(b"".join(old[:2]))
    offset, evs = E.iter_script_events(path)
    last_seq = max(e["script_seq"] for e in evs)
    assert (offset, last_seq) == (200, 2)
    with open(path, "ab") as fh:              # the old run wrote on...
        fh.write(old[2])
    # ...and between two polls the file was replaced by a new run's, of
    # exactly the old size: the size check sees nothing. Its line at the
    # offset carries seq 2, and 2 <= 2 says: new stream.
    new = [_padded(1, "hello", 200, stack="other"), _padded(2, E.PHASE, 100, name="stack")]
    with open(path, "wb") as fh:
        fh.write(b"".join(new))
    assert os.path.getsize(path) == 300 == len(b"".join(old)), "same size as the old file"
    offset2, evs2 = E.iter_script_events(path, offset, last_seq=last_seq)
    assert [e["script_seq"] for e in evs2] == [1, 2], "the whole new file, from 0"
    assert evs2[0]["type"] == "hello" and evs2[0]["stack"] == "other"
    assert offset2 == 300
    blind = E.iter_script_events(path, offset, last_seq=None)
    assert (blind[0], [e["script_seq"] for e in blind[1]]) == (300, [2]), \
        "without last_seq the same read passes as an append of seq 2: the old blind spot"


def test_a_longer_replacement_is_caught_by_seq(tmp_path):
    path = str(tmp_path / "events.jsonl")
    with open(path, "wb") as fh:
        fh.write(_padded(1, "hello", 100, stack="st") + _padded(2, E.PHASE, 100, name="stack"))
    offset, evs = E.iter_script_events(path)
    assert offset == 200 and [e["script_seq"] for e in evs] == [1, 2]
    # replaced by a longer file whose hello alone spans the old offset: the
    # read from 200 starts mid-hello (a fragment, skipped) and the first
    # complete line is seq 2 <= 2
    new = [_padded(1, "hello", 250, stack="other"), _padded(2, E.PHASE, 100, name="stack"),
           _padded(3, E.PHASE, 100, name="kvo")]
    with open(path, "wb") as fh:
        fh.write(b"".join(new))
    offset2, evs2 = E.iter_script_events(path, offset, last_seq=2)
    assert [e["script_seq"] for e in evs2] == [1, 2, 3] and evs2[0]["stack"] == "other"
    assert offset2 == 450 == os.path.getsize(path)
    assert all("id" not in e for e in evs2), "no id yet, as any event: the job stamps one at emit"


def test_a_genuine_append_is_never_a_restart(tmp_path):
    path = str(tmp_path / "events.jsonl")
    with open(path, "wb") as fh:
        fh.write(_padded(1, "hello", 100, stack="st") + _padded(2, E.PHASE, 100, name="stack"))
    offset, evs = E.iter_script_events(path)
    assert [e["script_seq"] for e in evs] == [1, 2]
    # the same run keeps writing: seq climbs, the read resumes at the offset
    with open(path, "ab") as fh:
        fh.write(_padded(3, E.PHASE, 100, name="kvo") + _padded(4, E.DONE, 100, status="ok"))
    offset2, evs2 = E.iter_script_events(path, offset, last_seq=2)
    assert [e["script_seq"] for e in evs2] == [3, 4], "only the new lines, nothing re-read"
    assert offset2 == 400 == os.path.getsize(path)
    assert E.iter_script_events(path, offset2, last_seq=4) == (400, [])


def test_the_first_read_never_restarts_and_a_read_from_zero_cannot(tmp_path):
    # A file whose seq regresses inside it (two runs, the second after a
    # truncation the reader never saw). With last_seq=None there is nothing
    # to compare against: the read is what is at the offset, no more.
    path = str(tmp_path / "events.jsonl")
    with open(path, "wb") as fh:
        fh.write(_padded(5, E.PHASE, 100, name="old") + _padded(2, E.PHASE, 100, name="new"))
    offset, evs = E.iter_script_events(path, 100)
    assert (offset, [e["script_seq"] for e in evs]) == (200, [2])
    offset, evs = E.iter_script_events(path, 100, last_seq=None)
    assert (offset, [e["script_seq"] for e in evs]) == (200, [2])
    # the same read with the old run's last seq is the restart
    offset, evs = E.iter_script_events(path, 100, last_seq=5)
    assert (offset, [e["script_seq"] for e in evs]) == (200, [5, 2])
    # and a read that already starts at 0 has nothing to restart from
    offset, evs = E.iter_script_events(path, 0, last_seq=99)
    assert (offset, [e["script_seq"] for e in evs]) == (200, [5, 2])

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print("\n%d tests passed" % len(fns))
