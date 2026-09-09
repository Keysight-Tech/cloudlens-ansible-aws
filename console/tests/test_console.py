"""Unit tests for the live console - pure logic, no AWS, no server.
Run:  cd console && python3 -m pytest tests -q
      (or, without pytest: cd console && PYTHONPATH=. python3 tests/test_console.py;
      conftest.py puts console/ on sys.path only under pytest)
"""
import os
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


def test_stack_cmd_speaks_the_flags_deploy_stack_actually_accepts():
    """The argv is a contract with deploy-stack.sh, and it was broken.

    The console shipped "--stack", "--kvo yes" and "--vpb yes". The script
    takes --stack-name and bare --with-kvo / --no-kvo toggles, so every real
    run died on "Unknown argument: --stack" after the replay had passed. The
    literals below are read off deploy-stack.sh --help, not off our own code.
    """
    from cloudlens_console import orchestrator as O

    class J(object):
        inputs = {"key": "some-key", "kvo": "yes", "vpb": "yes"}

    cmd = O._stack_cmd(J(), "st", "us-east-1")
    assert "--stack" not in cmd, "--stack is rejected by the script"
    assert "--stack-name" in cmd and cmd[cmd.index("--stack-name") + 1] == "st"
    assert "--region" in cmd and cmd[cmd.index("--region") + 1] == "us-east-1"
    assert "--with-kvo" in cmd and "--with-vpb" in cmd
    assert "--key-name" in cmd and cmd[cmd.index("--key-name") + 1] == "some-key"
    # A bare toggle must never be followed by a value the script would then
    # try to parse as the next flag.
    assert "yes" not in cmd and "no" not in cmd

    class N(object):
        inputs = {"key": "k", "kvo": "no", "vpb": "no"}

    off = O._stack_cmd(N(), "st", "us-east-1")
    assert "--no-kvo" in off and "--no-vpb" in off
    assert "--with-kvo" not in off and "--with-vpb" not in off


def test_no_script_flow_asks_for_something_it_then_throws_away():
    """A field the visitor fills in that reaches no command is a lie.

    Found by auditing the argv after the stack flow turned out to be sending
    flags its script rejects: five inputs across three flows were collected
    and silently dropped. The exemptions below are the ones still unwired,
    listed so they stay visible instead of passing quietly. Shrink this dict,
    never grow it: a new entry means a new field that does nothing.
    """
    UNWIRED = {
        # The playbook reads customer_input.yaml, not -e vars, and the
        # inventory hardcodes tag:cloudlens=yes, so neither field reaches it.
        ("sensors", "tag"): "inventory hardcodes the discovery tag",
        ("sensors", "region"): "inventory scans regions from the environment",
        # Needs a second command (scripts/vpb_kvo_adopt.py --vpb), and the
        # flow runs exactly one. A structural change, not a missing flag.
        ("kvo", "vpb"): "requires a second script invocation",
    }

    class J(object):
        def __init__(self, inputs):
            self.inputs = inputs

    dropped = []
    for fid in ("sensors", "kvo", "mirror"):
        flow = F.FLOWS[fid]
        keys = [f["key"] for f in flow["inputs"]]
        sentinel = dict((k, "SENTINEL_" + k) for k in keys)
        argv_list = O._script_cmd(J(sentinel), flow)
        # An empty token is the other way to lie: `--kvo ""` reads as wired
        # from here and hands the script a value it cannot use.
        assert all(a for a in argv_list), \
            "%s builds an argv with an empty token: %r" % (fid, argv_list)
        argv = " ".join(argv_list)
        for k in keys:
            if "SENTINEL_" + k in argv:
                continue
            if (fid, k) in UNWIRED:
                continue
            dropped.append("%s.%s" % (fid, k))

    assert not dropped, (
        "these inputs are collected from the visitor and never reach a "
        "command: %s" % (", ".join(dropped),))

    # And the exemptions must stay honest: if one gets wired up, delete it
    # from UNWIRED rather than leaving a stale excuse behind.
    stale = []
    for (fid, k), _why in UNWIRED.items():
        flow = F.FLOWS[fid]
        keys = [f["key"] for f in flow["inputs"]]
        sentinel = dict((kk, "SENTINEL_" + kk) for kk in keys)
        if "SENTINEL_" + k in " ".join(O._script_cmd(J(sentinel), flow)):
            stale.append("%s.%s" % (fid, k))
    assert not stale, (
        "these are wired up now and should be removed from UNWIRED: %s"
        % (", ".join(stale),))


def test_a_finished_node_never_goes_back_to_creating():
    """Seen live: the VPC read "creating" for a whole successful deploy.

    The resource map matches on a fragment, so one node covers several
    resources. VpcGatewayAttachment contains "Vpc", and its CREATE_IN_
    PROGRESS arrives after the VPC's own CREATE_COMPLETE. Without a guard
    that late event drags a finished node backwards and it never recovers,
    because the CREATE_COMPLETE that would relight it has already gone by.
    """
    import threading

    events = []
    stop = threading.Event()

    class J(object):
        stopped = False
        flow = "stack"

        def emit(self, ev):
            events.append(ev)
            # A stat closes one poll iteration, so stopping on it runs one
            # full pass on this thread: no worker, no sleep, no join.
            if ev.get("type") == E.STAT:
                stop.set()

        def elapsed(self):
            return 1

    class FakeCF(object):
        calls = 0

        def describe_stack_events(self, StackName):
            # Safety net: with POLL_SECS at 0, losing the stat emit would
            # spin this forever instead of failing. Two passes are plenty.
            FakeCF.calls += 1
            if FakeCF.calls >= 2:
                stop.set()
            # Oldest last, the way CloudFormation returns them.
            return {"StackEvents": [
                {"EventId": "3", "LogicalResourceId": "VpcGatewayAttachment",
                 "ResourceStatus": "CREATE_IN_PROGRESS"},
                {"EventId": "2", "LogicalResourceId": "Vpc",
                 "ResourceStatus": "CREATE_COMPLETE"},
                {"EventId": "1", "LogicalResourceId": "Vpc",
                 "ResourceStatus": "CREATE_IN_PROGRESS"},
            ]}

    import sys, types
    fake_boto3 = types.ModuleType("boto3")
    fake_sess = types.ModuleType("boto3.session")

    class S(object):
        def __init__(self, region_name=None):
            pass

        def client(self, _):
            return FakeCF()

    fake_sess.Session = S
    fake_boto3.session = fake_sess
    # Both entries go back the same way: restore what was there, delete what
    # was not. Restoring only "boto3" left a fake "boto3.session" behind for
    # whatever imported it next.
    saved = dict((m, sys.modules.get(m)) for m in ("boto3", "boto3.session"))
    sys.modules["boto3"] = fake_boto3
    sys.modules["boto3.session"] = fake_sess
    poll_secs = O.POLL_SECS
    O.POLL_SECS = 0
    try:
        O._poll_cfn(J(), F.FLOWS["stack"], "st", "us-east-1", stop)
    finally:
        O.POLL_SECS = poll_secs
        for m, mod in saved.items():
            if mod is not None:
                sys.modules[m] = mod
            else:
                sys.modules.pop(m, None)

    # The poll must say it is no longer waiting, in so many words: the page
    # clears the "waiting for the stack to appear" note on waiting=False,
    # not on the field being absent.
    assert any(e.get("type") == E.STAT and e.get("waiting") is False
               for e in events), "no stat carried waiting=False"

    vpc = [e for e in events
           if e.get("type") == E.STATE and e.get("node") == "vpc"]
    assert vpc, "the vpc node produced no state events at all"
    assert vpc[-1]["status"] == E.LIVE, (
        "a completed node ended as %r: a later matching CREATE_IN_PROGRESS "
        "dragged it backwards" % (vpc[-1]["status"],))


def test_stack_without_a_key_pair_fails_loudly_instead_of_minting_one():
    """No key means the script picks one for you, and says nothing.

    select_key_pair prompts with raw `read -rp ... || true`, not ask(), so
    under the console (no stdin, no terminal) every read returns EOF, the
    answer is empty, and the default wins: a key pair named cloudlens-key
    the visitor never chose. Refusing up front is the honest failure.
    """
    from cloudlens_console import orchestrator as O

    class J(object):
        inputs = {"kvo": "yes", "vpb": "yes"}  # no key

    try:
        O._stack_cmd(J(), "st", "us-east-1")
    except ValueError as exc:
        assert "key pair" in str(exc).lower()
    else:
        raise AssertionError("a missing key pair must raise, not mint one")


def test_engine_subprocess_has_no_controlling_tty():
    """deploy-stack.sh re-attaches /dev/tty whenever stdin is not a terminal,
    then treats the run as interactive. Launched from a console that was
    itself started in a terminal, the deploy inherited that terminal and
    stopped on "Proceed with this plan? [Y/n]" with every input supplied.
    A new session has no controlling terminal to re-attach, so every ask()
    takes its default; DEVNULL keeps the raw reads from blocking on it.
    """
    import inspect
    src = inspect.getsource(O._stream_subprocess)
    assert "start_new_session=True" in src
    assert "stdin=subprocess.DEVNULL" in src


def _replay_terminal(stop_after):
    """Drive a REAL replay through run_job, pressing Stop after `stop_after`
    log frames. Returns the terminal event it closed with, or None.

    The stop comes from the job's own emit(), which is deterministic and is the
    same thing POST /stop/<id> does: set job.stopped and let _run_replay notice
    on its next frame. A thread and a sleep would test the same code and flake.

    The fixture is written here rather than borrowed from fixtures/: this is
    about how run_job CLOSES a replay, and a real one's frames and delays would
    make the run slow and the assertion indirect.
    """
    import tempfile

    class StopsItself(O.Job):
        def emit(self, ev):
            super().emit(ev)
            if ev["type"] == E.LOG and \
                    len([e for e in self.buffer if e["type"] == E.LOG]) >= stop_after:
                self.stop()

    frames = [{"_delay": 0, "event": {"type": "log", "text": "line %d" % i}}
              for i in range(4)]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(frames, fh)
        path = fh.name
    try:
        job = StopsItself("replay-test", "stack", {})
        O.run_job(job, replay=path)
    finally:
        os.unlink(path)
    tail = [e for e in job.buffer if e["type"] in (E.DONE, E.ERROR)]
    return tail[-1] if tail else None


def test_a_stopped_replay_is_never_reported_as_a_success():
    """A run the visitor cut short must not be called complete.

    run_job closed every replay that reached no terminal event with
    done("Replay complete."), and a stop is exactly that: _run_replay returns
    the moment job.stopped is set. So a cancelled run reported success, and the
    only thing standing between that and the visitor was the page choosing to
    disbelieve it. Both real deploy paths already say "Stopped by operator.".
    """
    ev = _replay_terminal(stop_after=2)
    assert ev is not None, "a job that ends must say how"
    assert ev["type"] == E.ERROR, \
        "a stopped replay closed with %r" % (ev,)
    assert ev["text"] == "Stopped by operator.", \
        "the same sentence the real deploy paths use, from the same authority"


def test_a_replay_that_runs_out_is_still_complete():
    # The other direction, or the fix above is just "never report success".
    ev = _replay_terminal(stop_after=99)
    assert ev is not None and ev["type"] == E.DONE
    assert ev["summary"] == "Replay complete."


def test_replay_needs_no_boto3(monkeypatch=None):
    # _rebuild + replay path use only stdlib; importing orchestrator must not require boto3
    assert hasattr(O, "run_job") and hasattr(O, "_rebuild")


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


def test_replay_rebuilds_script_events_as_themselves():
    # A fixture recorded from a real run holds v2 frames, already in console
    # shape (script_seq, prompt_id). They fell through _rebuild to a log line,
    # so a replay lost the type; and a script done went through the console's
    # done() and lost its status.
    ev = O._rebuild(E.PHASE, {"ts": SCRIPT_TS, "name": "stack", "status": "done", "script_seq": 7})
    assert ev["type"] == E.PHASE and ev["name"] == "stack" and ev["script_seq"] == 7
    assert "id" not in ev, "a rebuilt frame takes its id from the replaying job, at emit"
    ev = O._rebuild(E.PROMPT, {"prompt_id": "p3", "question": "q", "kind": "text", "script_seq": 8})
    assert ev["prompt_id"] == "p3" and "id" not in ev
    ev = O._rebuild(E.DONE, {"status": "interrupted", "phase": "kvo", "script_seq": 9})
    assert ev["status"] == "interrupted" and ev["script_seq"] == 9
    # a fixture frame still carrying the console id it was recorded with:
    # from_script would read that "id" as the script's own and file it as
    # <type>_id, so _rebuild drops it first, as the STAT branch does
    ev = O._rebuild(E.PHASE, {"id": 999, "name": "stack", "status": "done", "script_seq": 10})
    assert "id" not in ev and "phase_id" not in ev
    # the console's own frames still take their own constructors
    ev = O._rebuild(E.DONE, {"summary": "ok", "outputs": {}})
    assert ev["summary"] == "ok" and "script_seq" not in ev



if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print("\n%d tests passed" % len(fns))
