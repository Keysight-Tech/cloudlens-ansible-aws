"""The pure halves of the three operations screens, run under node.

operate.js, licences.js and teardown.js are split the way watch.js is: a
pure function of one API answer (no DOM, no network, no globals) and a
render() that draws it. This file feeds those pure functions the exact
shapes api.py returns, under real node, and checks what comes back.

  operate     operateModel(): every field of /api/status keeps its own
              state, and a field with no answer never becomes a zero
  licences    codeRows()/licenceRows()/releaseRow(): the KVO's own shapes,
              a code shown only by its tail, and the session's release
              record, which is what the teardown warning turns on
  teardown    teardownGate(): the order the screen enforces, and the
              warning that stands over a stack with a KVO whose licences
              nobody released

The answers below are not invented: each is built by calling the real
api.py against stubs, so a field renamed in Python fails here.

Run:  cd console && python3 -m pytest tests/test_ops_model.py -q
"""
import json
import os
import shutil
import subprocess

import pytest

from cloudlens_console import api

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.abspath(os.path.join(HERE, ".."))
WEB = os.path.join(CONSOLE, "cloudlens_console", "web")

HARNESS = r"""
const fs = require("fs");
global.window = {};
process.argv.slice(3).forEach(function(f){
  new Function(fs.readFileSync(f, "utf8"))();
});
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const W = global.window;
const out = {};
if (IN.status !== undefined) {
  out.model = W.clOperate.operateModel(IN.status);
  out.why = W.clOperate.replayWhy(out.model, IN.stack || "", IN.region || "", false);
}
if (IN.check !== undefined) out.codeRows = W.clLicences.codeRows(IN.check);
if (IN.licences !== undefined) {
  out.licenceRows = W.clLicences.licenceRows(IN.licences);
  out.release = out.licenceRows.map(function(r){ return W.clLicences.releaseRow(r); });
}
if (IN.releaseResp !== undefined) {
  out.before = W.clLicences.released();
  out.recorded = W.clLicences.noteRelease(IN.kvo, IN.releaseResp);
}
if (IN.gate !== undefined) out.gate = W.clTeardown.teardownGate(IN.gate);
if (IN.gates !== undefined) out.gates = IN.gates.map(function(g){ return W.clTeardown.teardownGate(g); });
process.stdout.write(JSON.stringify(out));
"""


def _node(payload, tmp_path, *files):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: running the screen models needs it")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    data = tmp_path / "in.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    argv = [node, str(harness), str(data)] + [os.path.join(WEB, f) for f in files]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class _Ssh(object):
    """subprocess.Popen as api._vpb_cell drives it: one communicate() with
    the output and exit the test chose. It runs in its own session there,
    the way the doctor's child does, so a timeout can end the whole group."""
    def __init__(self, out="", err="", rc=0):
        self.out, self.err, self.rc = out, err, rc
        self.returncode, self.pid = None, 4343

    def __call__(self, argv, **kw):
        assert kw.get("start_new_session") is True, kw
        return self

    def communicate(self, timeout=None):
        self.returncode = self.rc
        return self.out, self.err


# ------------------------------------------------------------ operate.js
def _status(monkeypatch, tmp_path, aws=None, creds=None, ssh=None):
    """A real api.status() answer, against stubs."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", creds or str(tmp_path / "absent.json"))
    monkeypatch.setattr(api.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    monkeypatch.delenv("CLOUDLENS_KEY_PEM", raising=False)

    def default_aws(args, region, **kw):
        if args[:2] == ["ec2", "describe-instances"]:
            return {"Reservations": [{"Instances": [
                {"InstanceId": "i-1", "InstanceType": "t3.large", "KeyName": "lab",
                 "State": {"Name": "running"}, "PublicIpAddress": "3.1.1.1",
                 "Tags": [{"Key": "Name", "Value": "demo-vcontroller"}]},
                {"InstanceId": "i-2", "InstanceType": "t3.xlarge", "KeyName": "lab",
                 "State": {"Name": "running"}, "PrivateIpAddress": "10.0.0.9",
                 "Tags": [{"Key": "Name", "Value": "demo-kvo"}]},
                {"InstanceId": "i-3", "InstanceType": "t3.xlarge", "KeyName": "lab",
                 "State": {"Name": "running"}, "PublicIpAddress": "3.1.1.3",
                 "Tags": [{"Key": "Name", "Value": "demo-vpb"}]}]}]}
        return {"TrafficMirrorSessions": [{"TrafficMirrorSessionId": "tms-1"}]}

    monkeypatch.setattr(api, "_aws", aws or default_aws)

    def no_ssh(*a, **k):
        raise AssertionError("no ssh was expected")

    monkeypatch.setattr(api.subprocess, "run", no_ssh)
    monkeypatch.setattr(api.subprocess, "Popen", ssh or no_ssh)
    resp = api.status("demo", "us-east-1")
    # api.subprocess IS the subprocess module this file runs node with, so
    # the stubs come off before the harness starts
    monkeypatch.undo()
    return resp


def test_operate_model_keeps_a_state_per_field(tmp_path, monkeypatch):
    resp = _status(monkeypatch, tmp_path)
    out = _node({"status": resp, "stack": "demo", "region": "us-east-1"}, tmp_path, "plan.js", "operate.js")
    m = out["model"]
    assert m["stack"] == "demo" and m["region"] == "us-east-1" and not m["error"]
    assert m["phases"] == api.phase_order(), "the phase list comes from the answer, not from the page"
    assert [r["role"] for r in m["instances"]] == ["vcontroller", "kvo", "vpb"]
    assert m["instances"][0]["address"] == "3.1.1.1" and m["instances"][0]["addressKind"] == "public"
    assert m["instances"][1]["address"] == "10.0.0.9" and m["instances"][1]["addressKind"] == "private"
    assert m["instanceNote"].startswith("3 instances named demo-*")
    cells = {c["key"]: c for c in m["cells"]}
    assert set(cells) == {"sensors", "mirror", "vpb"}
    # the one field that could be read is a value; the two that could not
    # each carry their reason AND the command, and no number
    assert cells["mirror"]["state"] == "ok" and cells["mirror"]["text"].startswith("1 mirror session")
    for key in ("sensors", "vpb"):
        assert cells[key]["state"] == "blind", cells[key]
        assert cells[key]["text"] and cells[key]["command"], cells[key]
        assert "0" not in cells[key]["text"].split()[0], "a field with no answer is never a count"
    # no profile file: the replay buttons say why, naming the file
    assert "deploy-profile-demo.env" in out["why"] and "Deploy screen" in out["why"]


def test_operate_model_says_the_sensor_and_vpb_numbers_it_did_read(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"url": "https://3.1.1.1/cloudlens/login", "username": "admin",
                                 "password": "pw", "project": "autopilot"}))
    monkeypatch.setattr(api, "_vc_call", lambda m, u, token=None, body=None, timeout=None:
                        (200, {"ActiveSessionCredentials": {"JwtToken": "J"}, "Accounts": {"OwningAccount": {"id": "a"}}})
                        if u.endswith("/identity/login") else (200, [{"name": "autopilot", "agentCount": 3}]))
    pem = tmp_path / ".ssh" / "lab.pem"
    pem.parent.mkdir()
    pem.write_text("k")
    resp = _status(monkeypatch, tmp_path, creds=str(creds), ssh=_Ssh(out="rule-1 packets 42\n"))
    (tmp_path / "deploy-profile-demo.env").write_text('CLOUDLENS_STACK_NAME="demo"\n')
    resp["profile"]["present"] = True
    out = _node({"status": resp, "stack": "demo", "region": "us-east-1"}, tmp_path, "plan.js", "operate.js")
    cells = {c["key"]: c for c in out["model"]["cells"]}
    assert cells["sensors"]["text"] == "3 sensors registered in project autopilot (vController 3.1.1.1)"
    assert cells["vpb"]["state"] == "ok" and cells["vpb"]["text"] == "rule-1 packets 42"
    assert cells["vpb"]["command"].startswith("ssh -i "), "the value still names the command it ran"
    assert out["why"] == "", "with the profile there, the replay buttons are open"


def test_operate_model_shows_a_refusal_and_nothing_else(tmp_path, monkeypatch):
    out = _node({"status": {"error": "region must be an AWS region like us-east-1"},
                 "stack": "demo", "region": "nowhere"}, tmp_path, "plan.js", "operate.js")
    m = out["model"]
    assert m["error"] == "region must be an AWS region like us-east-1"
    assert m["instances"] == [] and m["cells"] == [] and m["phases"] == []
    out = _node({"status": None}, tmp_path, "plan.js", "operate.js")
    assert out["model"]["error"] == "" and out["model"]["instances"] == []


# ----------------------------------------------------------- licences.js
class _KL(object):
    """kvo_license.py's surface, enough for check/list/release."""
    def __init__(self):
        self.licences = [{"activationCode": "AAAA-1111-BBBB", "product": "KVO-DEVICE", "quantity": 5}]

    def accept_eula(self, kvo, verify):
        return True

    def token(self, kvo, user, pw, verify):
        return "TOK"

    def _req(self, method, url, token=None, body=None, verify=False, timeout=30):
        if url.endswith("/licensing/licenses"):
            return 200, list(self.licences)
        return 202, {"url": "/op/" + url.rsplit("/", 1)[-1]}

    def lookup_code(self, kvo, base, tok, code, verify):
        if code.startswith("BBBB"):
            return [("CloudLens-Credit", 10, 20)], {"state": "SUCCESS"}
        return [], {"state": "FAILED"}

    def poll_op(self, kvo, tok, first, verify, want_result=True, timeout=120, label=None):
        if first["url"].endswith("deactivate"):
            self.licences = []
        return {"state": "SUCCESS", "result": {}}


def test_licence_rows_are_the_kvos_own_shapes_and_show_only_a_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_kvo_license", lambda: _KL())
    creds = {"kvo": "10.1.2.3", "user": "admin", "password": "pw"}
    check = api.licences(dict(creds, action="check", codes=["BBBB-2222-CCCC", "ZZZZ-9999-XXXX"]))
    listed = api.licences(dict(creds, action="list"))
    out = _node({"check": check, "licences": listed}, tmp_path, "plan.js", "licences.js")
    rows = out["codeRows"]
    assert [r["valid"] for r in rows] == [True, False]
    assert rows[0]["tail"] == "****-CCCC" and rows[1]["tail"] == "****-XXXX"
    assert rows[0]["summary"] == "CloudLens-Credit 10 of 20"
    assert rows[0]["quantity"] == 10, "the default is what the entitlement says is available"
    assert rows[1]["quantity"] == 0 and "recognised nothing" in rows[1]["summary"]
    # the row keeps the code, because activating needs it; nothing the
    # screen DRAWS carries it
    assert rows[0]["code"] == "BBBB-2222-CCCC"
    for shown in (rows[0]["tail"], rows[0]["summary"], rows[1]["tail"], rows[1]["summary"]):
        assert "BBBB-2222" not in shown and "ZZZZ-9999" not in shown, shown
    assert out["licenceRows"] == [{"code": "AAAA-1111-BBBB", "tail": "****-BBBB",
                                   "product": "KVO-DEVICE", "quantity": 5}]
    assert out["release"] == [{"activationCode": "AAAA-1111-BBBB", "quantity": 5}], (
        "exactly what /api/licences/release takes")


def test_only_a_release_the_kvo_confirmed_is_recorded(tmp_path, monkeypatch):
    kl = _KL()
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    body = {"kvo": "10.1.2.3", "password": "pw", "action": "release",
            "rows": [{"activationCode": "AAAA-1111-BBBB", "quantity": 5}]}
    ok = api.licences(dict(body))
    assert ok["released"] is True
    out = _node({"releaseResp": ok, "kvo": "10.1.2.3"}, tmp_path, "plan.js", "licences.js")
    assert out["before"] is None, "nothing is recorded until a release comes back"
    assert out["recorded"]["kvo"] == "10.1.2.3" and out["recorded"]["codes"] == ["****-BBBB"]
    # a release the KVO refused records nothing: the teardown warning has to
    # stand on evidence, not on the button having been pressed
    refused = dict(ok, released=False, results=[dict(ok["results"][0], ok=False, state="FAILED")])
    out = _node({"releaseResp": refused, "kvo": "10.1.2.3"}, tmp_path, "plan.js", "licences.js")
    assert out["recorded"] is None


# ----------------------------------------------------------- teardown.js
def test_the_teardown_gate_is_the_order_the_screen_promises(tmp_path):
    base = {"stack": "demo", "region": "us-east-1", "typed": "", "auditFor": "", "hasKvo": False}
    gates = [
        dict(base, stack="", region=""),                                    # 0 nothing named
        dict(base),                                                         # 1 no audit yet
        dict(base, auditFor="demo/us-east-1"),                              # 2 audited, nothing typed
        dict(base, auditFor="demo/us-east-1", typed="dem"),                 # 3 typed wrong
        dict(base, auditFor="demo/us-east-1", typed="demo"),                # 4 armed
        dict(base, auditFor="other/us-east-1", typed="demo"),               # 5 audited another stack
        dict(base, auditFor="demo/us-east-1", typed="demo", running=True),  # 6 an engine holds it
    ]
    out = _node({"gates": gates}, tmp_path, "plan.js", "teardown.js")["gates"]
    assert [g["armed"] for g in out] == [False, False, False, False, True, False, False]
    assert "Name the stack" in out[0]["why"]
    assert "audit first" in out[1]["why"] and "read-only" in out[1]["why"]
    assert out[2]["why"] == "Type demo to arm the teardown." and out[3]["why"] == out[2]["why"]
    assert "audit first" in out[5]["why"], "an audit of another stack is not this stack's"
    assert "one engine per stack" in out[6]["why"]
    assert all(g["warn"] is None for g in out), "no KVO, no licence warning"


def test_the_licence_warning_stands_until_this_stacks_own_kvo_is_released(tmp_path):
    """A release counts only for the appliance it was made against.

    licences_released:true is what puts --accept-licence-loss on
    teardown-stack.sh's argv, and /api/teardown trusts the body by design,
    so this page is the only place the session's release record can be tied
    to the KVO that is about to be deleted. Releasing on KVO A and then
    tearing down a stack whose KVO is B released nothing of B's, and
    satisfying the script's licence gate on that evidence is exactly the
    stranding this screen exists to prevent. The recorded host and the
    kvo-role instance's address are compared as whole hosts: 10.1.2.3 is
    not 10.1.2.30.
    """
    armed = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1"}
    mine = {"kvo": "10.1.2.3", "codes": ["****-BBBB"]}
    gates = [
        dict(armed, hasKvo=True, kvoAddr="10.1.2.3"),                                   # 0 KVO, nothing released
        dict(armed, hasKvo=True, kvoAddr="10.1.2.3", released=mine),                    # 1 this stack's own KVO
        dict(armed, hasKvo=True, kvoAddr="10.1.2.30", kvoName="demo-kvo",
             released=mine),                                                            # 2 another appliance
        dict(armed, hasKvo=True, kvoAddr="", kvoName="demo-kvo", released=mine),        # 3 KVO with no address
        dict(armed, hasKvo=None, kvoWhy="AccessDenied", released=mine),                 # 4 could not tell
        dict(armed, hasKvo=False, released=mine),                                       # 5 no KVO at all
    ]
    out = _node({"gates": gates}, tmp_path, "plan.js", "teardown.js")["gates"]
    assert [g["armed"] for g in out] == [True] * 6, "the warning warns; the typed name is what arms it"
    # exactly one of these six may tell the API the licences were released
    assert [g["licencesReleased"] for g in out] == [False, True, False, False, False, False]

    # 0 a KVO and no release at all: the original warning, word for word
    assert out[0]["warn"]["level"] == "bad"
    assert out[0]["warn"]["text"].startswith("This stack has a KVO and"), (
        "no empty brackets where the instance name is not known: " + out[0]["warn"]["text"])
    assert "Licensing screen" in out[0]["warn"]["text"] and "do not come back" in out[0]["warn"]["text"]

    # 1 the release was made against this stack's own KVO: it counts
    assert out[1]["warn"]["level"] == "good"
    assert "released from 10.1.2.3" in out[1]["warn"]["text"]
    assert "which is this stack's KVO," in out[1]["warn"]["text"]
    assert "--accept-licence-loss" in out[1]["warn"]["text"]

    # 2 a release from a host that only LOOKS like this one's: named, both
    # of them, and not counted
    assert out[2]["warn"]["level"] == "bad"
    assert ("came from 10.1.2.3, not from this stack's KVO (demo-kvo) at 10.1.2.30"
            in out[2]["warn"]["text"]), out[2]["warn"]["text"]
    assert "NOT run with --accept-licence-loss" in out[2]["warn"]["text"]

    # 3 a KVO whose address the console could not read cannot be matched
    assert out[3]["warn"]["level"] == "bad"
    assert "has no address in the console's answer" in out[3]["warn"]["text"]
    assert "NOT run with --accept-licence-loss" in out[3]["warn"]["text"]

    # 4 a KVO that could not be checked is warned about, not assumed away
    assert out[4]["warn"]["level"] == "warn" and "AccessDenied" in out[4]["warn"]["text"]

    # 5 no KVO: nothing to strand, so nothing to say
    assert out[5]["warn"] is None

    # the instance's own name is shown when the console read one, on the
    # matching case as well as the refusing ones
    named = _node({"gate": dict(armed, hasKvo=True, kvoName="demo-kvo", kvoAddr="10.1.2.3")},
                  tmp_path, "plan.js", "teardown.js")["gate"]
    assert named["warn"]["text"].startswith("This stack has a KVO (demo-kvo) and")

    # the record's host is compared, not the string: a url and a bare
    # address for the same box are the same box
    urls = _node({"gates": [dict(armed, hasKvo=True, kvoAddr="10.1.2.3",
                                 released={"kvo": "https://10.1.2.3:8443/", "codes": ["****-BBBB"]}),
                            dict(armed, hasKvo=True, kvoAddr="10.1.2.3",
                                 released={"kvo": "https://10.1.2.33/", "codes": ["****-BBBB"]})]},
                 tmp_path, "plan.js", "teardown.js")["gates"]
    assert [g["licencesReleased"] for g in urls] == [True, False]
    assert urls[0]["warn"]["level"] == "good" and urls[1]["warn"]["level"] == "bad"
