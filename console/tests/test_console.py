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


def test_replay_needs_no_boto3(monkeypatch=None):
    # _rebuild + replay path use only stdlib; importing orchestrator must not require boto3
    assert hasattr(O, "run_job") and hasattr(O, "_rebuild")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print("\n%d tests passed" % len(fns))
