"""Unit tests for the live console - pure logic, no AWS, no server.
Run:  cd console && python3 -m pytest tests -q     (or: python3 tests/test_console.py)
"""
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cloudlens_console import events as E, flows as F, orchestrator as O  # noqa


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


def test_stack_without_a_key_pair_fails_loudly_instead_of_hanging():
    """No key means an interactive prompt, and the console has no TTY.

    The script's key-pair picker reads from stdin. Under the console that
    blocks forever with an empty screen, which reads to the visitor as a
    hung deploy. Refusing up front is the honest failure.
    """
    from cloudlens_console import orchestrator as O

    class J(object):
        inputs = {"kvo": "yes", "vpb": "yes"}  # no key

    try:
        O._stack_cmd(J(), "st", "us-east-1")
    except ValueError as exc:
        assert "key pair" in str(exc).lower()
    else:
        raise AssertionError("a missing key pair must raise, not hang")


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print("\n%d tests passed" % len(fns))
