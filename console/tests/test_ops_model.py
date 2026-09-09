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
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

// A DOM, only for a test that drives the SCREEN and not its pure half.
// It is installed BEFORE the files load, because each screen inits itself
// when the element it draws into exists. Enough of one for the licensing
// screen: elements that remember their handlers, and a document that
// makes one up for any id asked for, since the screens build rows as
// markup and then look the ids back up.
if (IN.flight !== undefined) {
  let seq = 0;
  const byId = {};
  const make = function(id){
    const el = {id: id, value: "", checked: false, disabled: false, textContent: "",
                innerHTML: "", className: "", type: "", hidden: false, on: {}, kids: []};
    el.classList = {toggle: function(){}, add: function(){}, remove: function(){}};
    el.setAttribute = function(){};
    el.appendChild = function(c){ el.kids.push(c); return c; };
    el.addEventListener = function(k, f){ el.on[k] = f; };
    return el;
  };
  global.document = {
    getElementById: function(id){ return byId[id] || (byId[id] = make(id)); },
    createElement: function(){ return make("::" + (++seq)); },
    createTextNode: function(t){ return {text: t}; },
    addEventListener: function(){}
  };
  global.window.confirm = function(){ return true; };
}

process.argv.slice(3).forEach(function(f){
  new Function(fs.readFileSync(f, "utf8"))();
});
const W = global.window;
const out = {};
if (IN.status !== undefined) {
  out.model = W.clOperate.operateModel(IN.status);
  out.why = W.clOperate.replayWhy(out.model, IN.stack || "", IN.region || "", false);
}
if (IN.escape !== undefined) out.escaped = W.clPlan.esc(IN.escape);
if (IN.check !== undefined) out.codeRows = W.clLicences.codeRows(IN.check);
if (IN.licences !== undefined) {
  out.licenceRows = W.clLicences.licenceRows(IN.licences);
  out.release = out.licenceRows.map(function(r){ return W.clLicences.releaseRow(r); });
}
if (IN.releaseResp !== undefined) {
  out.before = W.clLicences.released();
  out.recorded = W.clLicences.noteRelease(IN.kvo, IN.releaseResp);
}
if (IN.inst !== undefined) out.kvoAnswer = W.clTeardown.kvoAnswer(IN.inst);
if (IN.gate !== undefined) out.gate = W.clTeardown.teardownGate(IN.gate);
if (IN.gates !== undefined) out.gates = IN.gates.map(function(g){ return W.clTeardown.teardownGate(g); });
if (IN.freshModel !== undefined) out.freshModel = W.clTeardown.model();
if (IN.rows !== undefined) out.kvoRow = W.clTeardown.kvoRow(IN.rows);
if (IN.session !== undefined) out.session = IN.session.map(function(step){
  if (step.release !== undefined)
    return {recorded: W.clLicences.noteRelease(step.release.kvo, step.release.resp)};
  // what activate() and load() do when the KVO says it holds something
  if (step.holds !== undefined) return {held: W.clLicences.noteHolds(step.holds)};
  var m = {};
  Object.keys(step.gate).forEach(function(k){ m[k] = step.gate[k]; });
  // exactly what teardown.js's render() does before it gates: the record is
  // SELECTED from the licensing screen's own, never handed in
  m.released = W.clTeardown.recordFor(m.kvoAddr, W.clLicences);
  return {released: m.released, gate: W.clTeardown.teardownGate(m)};
});
/* The screen, driven a step at a time with the network HELD: a licensing
   call POSTs one operation per row and polls each to its end, so the
   interesting moment is what the operator does while one is in flight. */
if (IN.flight !== undefined) {
  const posts = [], reads = [];
  let pending = null;
  W.clUi.post = function(path, body, cb){
    posts.push({path: path, action: body.action, kvo: body.kvo});
    pending = cb;                       // held, until the test answers it
  };
  IN.flight.forEach(function(step, n){
    if (step.set) Object.keys(step.set).forEach(function(id){
      document.getElementById(id).value = step.set[id];
    });
    if (step.click) {
      const el = document.getElementById(step.click);
      if (!el.on.click) throw new Error("step " + n + ": #" + step.click + " has no click handler");
      el.on.click();
    }
    if (step.answer !== undefined) {
      if (!pending) throw new Error("step " + n + ": nothing is in flight to answer");
      const cb = pending; pending = null;
      cb({ok: true, status: 200, d: step.answer});
    }
    if (step.read !== undefined) reads.push({kvo: step.read, record: W.clLicences.released(step.read)});
    if (step.gate !== undefined) {
      const m = {};
      Object.keys(step.gate).forEach(function(k){ m[k] = step.gate[k]; });
      // exactly what teardown.js's render() does: the record is SELECTED
      m.released = W.clTeardown.recordFor(m.kvoAddr, W.clLicences);
      reads.push({gate: W.clTeardown.teardownGate(m)});
    }
  });
  // an unanswered call leaves ui.js's ticking interval running, which
  // would hold node open: fail loudly instead of hanging
  if (pending) throw new Error("a call was left in flight");
  out.flight = {posts: posts, reads: reads};
}
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
    out = _node({"status": resp, "stack": "demo", "region": "us-east-1"}, tmp_path, "ui.js", "plan.js", "operate.js")
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
    out = _node({"status": resp, "stack": "demo", "region": "us-east-1"}, tmp_path, "ui.js", "plan.js", "operate.js")
    cells = {c["key"]: c for c in out["model"]["cells"]}
    assert cells["sensors"]["text"] == "3 sensors registered in project autopilot (vController 3.1.1.1)"
    assert cells["vpb"]["state"] == "ok" and cells["vpb"]["text"] == "rule-1 packets 42"
    assert cells["vpb"]["command"].startswith("ssh -i "), "the value still names the command it ran"
    assert out["why"] == "", "with the profile there, the replay buttons are open"


def test_operate_model_shows_a_refusal_and_nothing_else(tmp_path, monkeypatch):
    out = _node({"status": {"error": "region must be an AWS region like us-east-1"},
                 "stack": "demo", "region": "nowhere"}, tmp_path, "ui.js", "plan.js", "operate.js")
    m = out["model"]
    assert m["error"] == "region must be an AWS region like us-east-1"
    assert m["instances"] == [] and m["cells"] == [] and m["phases"] == []
    out = _node({"status": None}, tmp_path, "ui.js", "plan.js", "operate.js")
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

    def lookup_code(self, kvo, base, tok, code, verify, timeout=None):
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
    out = _node({"check": check, "licences": listed}, tmp_path, "ui.js", "plan.js", "licences.js")
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
    out = _node({"releaseResp": ok, "kvo": "10.1.2.3"}, tmp_path, "ui.js", "plan.js", "licences.js")
    assert out["before"] is None, "nothing is recorded until a release comes back"
    assert out["recorded"]["kvo"] == "10.1.2.3" and out["recorded"]["codes"] == ["****-BBBB"]
    assert ok["clear"] is True and out["recorded"]["clear"] is True, (
        "the KVO's own answer to whether it still holds licences is what the record carries")
    # a release the KVO refused records nothing: the teardown warning has to
    # stand on evidence, not on the button having been pressed
    refused = dict(ok, released=False, results=[dict(ok["results"][0], ok=False, state="FAILED")])
    out = _node({"releaseResp": refused, "kvo": "10.1.2.3"}, tmp_path, "ui.js", "plan.js", "licences.js")
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
    out = _node({"gates": gates}, tmp_path, "ui.js", "plan.js", "teardown.js")["gates"]
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
    mine = {"kvo": "10.1.2.3", "codes": ["****-BBBB"], "clear": True}
    partial = {"kvo": "10.1.2.3", "codes": ["****-BBBB"], "clear": False}
    gates = [
        dict(armed, hasKvo=True, kvoAddr="10.1.2.3"),                                   # 0 KVO, nothing released
        dict(armed, hasKvo=True, kvoAddr="10.1.2.3", released=mine),                    # 1 this stack's own KVO
        dict(armed, hasKvo=True, kvoAddr="10.1.2.30", kvoName="demo-kvo",
             released=mine),                                                            # 2 another appliance
        dict(armed, hasKvo=True, kvoAddr="", kvoName="demo-kvo", released=mine),        # 3 KVO with no address
        dict(armed, hasKvo=None, kvoWhy="AccessDenied", released=mine),                 # 4 could not tell
        dict(armed, hasKvo=False, released=mine),                                       # 5 no KVO at all
        dict(armed, hasKvo=True, kvoAddr="10.1.2.3", kvoName="demo-kvo",
             released=partial),                                                         # 6 released, still holds
    ]
    out = _node({"gates": gates}, tmp_path, "ui.js", "plan.js", "teardown.js")["gates"]
    assert [g["armed"] for g in out] == [True] * 7, "the warning warns; the typed name is what arms it"
    # exactly one of these seven may tell the API the licences were released
    assert [g["licencesReleased"] for g in out] == [False, True, False, False, False, False, False]

    # 0 a KVO and no release at all: the original warning, word for word
    assert out[0]["warn"]["level"] == "bad"
    assert out[0]["warn"]["text"].startswith("This stack has a KVO and"), (
        "no empty brackets where the instance name is not known: " + out[0]["warn"]["text"])
    assert "Licensing screen" in out[0]["warn"]["text"] and "do not come back" in out[0]["warn"]["text"]

    # 1 the release was made against this stack's own KVO: it counts
    assert out[1]["warn"]["level"] == "good"
    assert "released from 10.1.2.3" in out[1]["warn"]["text"]
    assert "which is this stack's KVO," in out[1]["warn"]["text"]
    assert "now holds none" in out[1]["warn"]["text"], (
        "the green banner says the KVO is clear, not merely that a release happened")
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

    # 6 a release against this stack's OWN KVO that left licences on it. The
    # defect this case exists for: the record said only THAT something was
    # released, so a partial release painted the banner green and sent
    # --accept-licence-loss, which is teardown-stack.sh's last gate in a
    # non-interactive run. The counts still on the KVO went with it.
    assert out[6]["warn"]["level"] == "bad"
    assert "STILL HOLDS" in out[6]["warn"]["text"], out[6]["warn"]["text"]
    assert "NOT run with --accept-licence-loss" in out[6]["warn"]["text"]
    assert "only part of them" in out[6]["warn"]["text"]
    # and it does not read like "nobody released anything", which is a
    # different job with a different next step
    assert "nothing has been released" not in out[6]["warn"]["text"]
    assert out[6]["warn"]["text"] != out[0]["warn"]["text"]

    # the instance's own name is shown when the console read one, on the
    # matching case as well as the refusing ones
    named = _node({"gate": dict(armed, hasKvo=True, kvoName="demo-kvo", kvoAddr="10.1.2.3")},
                  tmp_path, "ui.js", "plan.js", "teardown.js")["gate"]
    assert named["warn"]["text"].startswith("This stack has a KVO (demo-kvo) and")

    # the record's host is compared, not the string: a url and a bare
    # address for the same box are the same box
    urls = _node({"gates": [dict(armed, hasKvo=True, kvoAddr="10.1.2.3",
                                 released={"kvo": "https://10.1.2.3:8443/", "codes": ["****-BBBB"],
                                           "clear": True}),
                            dict(armed, hasKvo=True, kvoAddr="10.1.2.3",
                                 released={"kvo": "https://10.1.2.33/", "codes": ["****-BBBB"],
                                           "clear": True})]},
                 tmp_path, "ui.js", "plan.js", "teardown.js")["gates"]
    assert [g["licencesReleased"] for g in urls] == [True, False]
    assert urls[0]["warn"]["level"] == "good" and urls[1]["warn"]["level"] == "bad"


class _KLPartial(_KL):
    """A KVO holding two licences that gives one back: the deactivate
    succeeds, and the appliance still holds the other. This is the shape a
    real partial release has, and the one the record used to lose."""
    def __init__(self):
        _KL.__init__(self)
        self.licences = [{"activationCode": "AAAA-1111-BBBB", "product": "KVO-DEVICE", "quantity": 5},
                         {"activationCode": "CCCC-2222-DDDD", "product": "KVO-CREDIT", "quantity": 100}]

    def poll_op(self, kvo, tok, first, verify, want_result=True, timeout=120, label=None):
        if first["url"].endswith("deactivate"):
            self.licences = self.licences[1:]       # one row given back, one left
        return {"state": "SUCCESS", "result": {}}


def test_a_session_of_releases_is_read_back_per_kvo_and_only_when_it_is_clear(tmp_path, monkeypatch):
    """The whole chain, driven the way the page drives it.

    The two screens were only ever tested apart: test_ops_model handed
    teardownGate a `released` record it had built by hand, so neither of
    the two defects below could appear, because neither is in either
    function on its own. Both are in the seam.

      the record was one global bucket. `kvo` was overwritten per release
      while `codes` accumulated, so after releasing on 10.1.2.3 and then
      10.9.9.9 the screen told the operator that both codes came from
      10.9.9.9. The argv was right and the sentence somebody decides on
      was wrong.

      the record said only THAT a release happened. api._lic_release
      answers `clear`, which is the KVO's own reading of whether it still
      holds a licence, and a release that succeeded on the rows it was
      given can leave others installed. With clear false the banner went
      GREEN and licences_released:true went to the API, which is
      --accept-licence-loss on teardown-stack.sh's command line and the
      only gate a non-interactive delete has left.

    So this drives noteRelease with what api._lic_release actually
    returns, generated from the real function against stubs, and then runs
    teardownGate on the record the Teardown screen SELECTS (recordFor),
    not on one the test invented.
    """
    clear = _KL()
    monkeypatch.setattr(api, "_kvo_license", lambda: clear)
    a = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                      "rows": [{"activationCode": "AAAA-1111-BBBB", "quantity": 5}]})
    partial = _KLPartial()
    monkeypatch.setattr(api, "_kvo_license", lambda: partial)
    b = api.licences({"kvo": "10.9.9.9", "password": "pw", "action": "release",
                      "rows": [{"activationCode": "CCCC-2222-DDDD", "quantity": 100}]})
    assert a["released"] is True and a["clear"] is True
    assert b["released"] is True and b["clear"] is False, (
        "the deactivate succeeded and the KVO still holds a licence: that is a partial release")

    armed = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1",
             "hasKvo": True}
    steps = [
        {"release": {"kvo": "10.1.2.3", "resp": a}},                            # 0
        {"gate": dict(armed, kvoAddr="10.1.2.3", kvoName="demo-kvo")},          # 1 clear
        {"release": {"kvo": "10.9.9.9", "resp": b}},                            # 2
        {"gate": dict(armed, kvoAddr="10.9.9.9", kvoName="other-kvo")},         # 3 partial
        {"gate": dict(armed, kvoAddr="10.1.2.3", kvoName="demo-kvo")},          # 4 the first, again
        {"gate": dict(armed, kvoAddr="10.4.4.4", kvoName="third-kvo")},         # 5 never released
    ]
    out = _node({"session": steps}, tmp_path, "ui.js", "plan.js", "licences.js", "teardown.js")["session"]

    # 1 released, and the KVO says it holds nothing: the one case that may
    # put --accept-licence-loss on the argv
    assert out[1]["gate"]["licencesReleased"] is True
    assert out[1]["gate"]["warn"]["level"] == "good"
    assert out[1]["released"]["codes"] == ["****-BBBB"] and out[1]["released"]["clear"] is True

    # 3 released, and the KVO still holds one. The banner is red and the API
    # is told nothing was released.
    assert out[3]["released"]["clear"] is False, out[3]["released"]
    assert out[3]["gate"]["licencesReleased"] is False, (
        "a partial release satisfying the last gate is how a KVO is deleted with its counts on it")
    assert out[3]["gate"]["warn"]["level"] == "bad"
    assert "STILL HOLDS" in out[3]["gate"]["warn"]["text"]

    # 4 the second KVO's release did not join the first one's record: two
    # appliances, two records, and each keeps its own codes
    assert out[4]["released"]["kvo"] == "10.1.2.3", out[4]["released"]
    assert out[4]["released"]["codes"] == ["****-BBBB"], (
        "the code released on 10.9.9.9 was reported as 10.1.2.3's: " + str(out[4]["released"]))
    assert out[3]["released"]["codes"] == ["****-DDDD"], out[3]["released"]
    assert out[4]["gate"]["licencesReleased"] is True

    # 5 a third KVO nobody released: the warning names where the session's
    # most recent release actually came from, and refuses to count it
    assert out[5]["gate"]["licencesReleased"] is False
    assert out[5]["gate"]["warn"]["level"] == "bad"
    assert "came from 10.9.9.9" in out[5]["gate"]["warn"]["text"], out[5]["gate"]["warn"]["text"]
    assert "10.4.4.4" in out[5]["gate"]["warn"]["text"]


def test_a_teardown_screen_that_has_not_asked_yet_warns_rather_than_arming_silently(tmp_path):
    """hasKvo starts null, which is the "could not tell" warning, and not
    false, which means "this stack has no KVO" and draws nothing at all.

    The audit and the KVO check are two requests fired together, and they
    are not the same length: --orphans is a handful of describe calls,
    while /api/status serially does describe-instances, up to two
    vController calls and an ssh with a 25 second timeout. The audit
    routinely finishes first, so with false as the starting value the
    screen armed with no banner over it for as long as the status took;
    and nothing reset it, so a false left by one stack stood over the
    next. teardown.js now starts at null and goes back to null on every
    audit and on every edit of the stack or the region.
    """
    fresh = _node({"freshModel": True}, tmp_path, "ui.js", "plan.js", "teardown.js")["freshModel"]
    assert fresh["hasKvo"] is None, "before the answer lands, whether there is a KVO is not known"
    assert fresh["kvoAddr"] == "" and fresh["kvoName"] == ""
    armed = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1"}
    gate = _node({"gate": dict(armed, **{k: fresh[k] for k in ("hasKvo", "kvoName", "kvoAddr", "kvoWhy")})},
                 tmp_path, "ui.js", "plan.js", "teardown.js")["gate"]
    assert gate["armed"] is True, "the typed name arms the button; the banner is what warns"
    assert gate["warn"] and gate["warn"]["level"] == "warn", gate["warn"]
    assert "could not be checked" in gate["warn"]["text"]
    assert gate["licencesReleased"] is False


def test_one_rule_picks_the_kvo_row_on_both_sides(tmp_path, monkeypatch):
    """api.by_role took the FIRST instance of a role while teardown.js took
    the LAST, and neither preferred a running one.

    A stack carrying a stopped instance beside its live one therefore had
    two answers to "which box is the KVO": the address the console reports
    and licenses against, and the address a recorded release is matched to.
    Different answers there read as "that release came from another
    appliance" on a stack with one KVO branch, which is the refusal that
    stops a teardown. Both sides now take the first, except that a running
    instance beats one that is not running.
    """
    def one(iid, role, state, ip):
        return {"InstanceId": iid, "InstanceType": "t3.xlarge", "KeyName": "lab",
                "State": {"Name": state}, "PublicIpAddress": ip,
                "Tags": [{"Key": "Name", "Value": "demo-" + role}]}

    def aws(args, region, **kw):
        if args[:2] == ["ec2", "describe-instances"]:
            # a stopped one before AND after the live one, so "the first"
            # and "the last" are each a wrong answer and only "prefer
            # running" is the right one
            return {"Reservations": [{"Instances": [
                one("i-kvo-before", "kvo", "stopped", "3.1.1.1"),
                one("i-kvo", "kvo", "running", "3.1.1.9"),
                one("i-kvo-after", "kvo", "stopped", "3.1.1.2"),
                one("i-vpb-before", "vpb", "stopped", "3.1.1.3"),
                one("i-vpb", "vpb", "running", "3.1.1.7"),
                one("i-vpb-after", "vpb", "stopped", "3.1.1.4")]}]}
        return {"TrafficMirrorSessions": []}

    resp = _status(monkeypatch, tmp_path, aws=aws)
    # api's own pick, read where status() surfaces it: the vPB cell names
    # the address it would have gone to
    assert "@3.1.1.7" in resp["vpb"]["command"], resp["vpb"]
    rows = resp["instances"]["value"]["rows"]
    out = _node({"rows": rows}, tmp_path, "ui.js", "plan.js", "teardown.js")
    assert out["kvoRow"]["id"] == "i-kvo", out["kvoRow"]
    assert out["kvoRow"]["public_ip"] == "3.1.1.9"
    # and with only a stopped one, the stopped one is still the answer:
    # "prefer running" is not "ignore everything else"
    stopped = [r for r in rows if r["id"].startswith("i-kvo-")]
    assert len(stopped) == 2
    assert _node({"rows": stopped}, tmp_path, "ui.js", "plan.js",
                 "teardown.js")["kvoRow"]["id"] == "i-kvo-before", "with none running, the first stands"
    assert _node({"rows": []}, tmp_path, "ui.js", "plan.js", "teardown.js")["kvoRow"] is None


class _KLUnreadable(_KL):
    """A KVO whose deactivate succeeds and whose licence list then comes
    back as an HTTP error body: _req returns (code, body) and does not
    raise, so this is the shape that used to read as an empty list and
    therefore as a clear appliance."""
    def _req(self, method, url, token=None, body=None, verify=False, timeout=30):
        if url.endswith("/licensing/licenses"):
            return 500, {"error": "internal server error"}
        return _KL._req(self, method, url, token, body, verify, timeout)


def test_a_release_whose_list_could_not_be_read_never_paints_the_banner_green(tmp_path, monkeypatch):
    """The seam the whole gate hangs on, driven end to end.

    api._lic_release answers clear:null when it could not read what the
    KVO holds. licences.js must record that as "not clear" (and as its own
    kind of not-clear, so the banner can say which), and teardownGate must
    refuse to arm on it. Before the read was tri-state this exact KVO -
    deactivate SUCCESS, licence list an HTTP 500 - answered clear:true,
    which is a green banner and --accept-licence-loss on the argv of a run
    that deletes the appliance."""
    monkeypatch.setattr(api, "_kvo_license", lambda: _KLUnreadable())
    resp = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                         "rows": [{"activationCode": "AAAA-1111-BBBB", "quantity": 5}]})
    assert resp["released"] is True and resp["clear"] is None and resp["unreadable"]

    armed = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1",
             "hasKvo": True, "kvoAddr": "10.1.2.3", "kvoName": "demo-kvo"}
    out = _node({"session": [{"release": {"kvo": "10.1.2.3", "resp": resp}},
                             {"gate": dict(armed)}]},
                tmp_path, "ui.js", "plan.js", "licences.js", "teardown.js")["session"]
    rec = out[1]["released"]
    assert rec["clear"] is False and rec["unknown"] is True, rec
    assert out[1]["gate"]["licencesReleased"] is False, (
        "an unread licence list is not an empty one, and must never arm --accept-licence-loss")
    assert out[1]["gate"]["warn"]["level"] == "bad"
    assert "could not be READ" in out[1]["gate"]["warn"]["text"], out[1]["gate"]["warn"]["text"]
    # and it does not read as "the KVO still holds licences", which is a
    # different fact with a different next step
    assert "STILL HOLDS" not in out[1]["gate"]["warn"]["text"]


def test_a_later_activation_takes_back_an_earlier_releases_evidence(tmp_path, monkeypatch):
    """`record` was written by noteRelease alone, so it aged into a lie.

    Release everything on a KVO (green banner, licencesReleased true),
    then activate a new code on the SAME KVO in the same session, and the
    banner stayed green off the older fact: the appliance now holds
    licences again and the teardown would still have run with
    --accept-licence-loss. Anything that says the KVO holds licences - an
    activation against it, or a list that names one - marks the record
    stale, and a stale record no longer arms the gate. The codes and the
    sentence stay, because the release did happen."""
    clear = _KL()
    monkeypatch.setattr(api, "_kvo_license", lambda: clear)
    resp = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                         "rows": [{"activationCode": "AAAA-1111-BBBB", "quantity": 5}]})
    assert resp["clear"] is True
    armed = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1",
             "hasKvo": True, "kvoAddr": "10.1.2.3", "kvoName": "demo-kvo"}
    steps = [
        {"release": {"kvo": "10.1.2.3", "resp": resp}},     # 0
        {"gate": dict(armed)},                              # 1 clear: the one green case
        {"holds": "https://10.1.2.3/"},                     # 2 an activation lands on it
        {"gate": dict(armed)},                              # 3 the same gate, after
        {"gate": dict(armed, kvoAddr="10.9.9.9", kvoName="other-kvo")},   # 4 another appliance
    ]
    out = _node({"session": steps}, tmp_path, "ui.js", "plan.js", "licences.js", "teardown.js")["session"]
    assert out[1]["gate"]["licencesReleased"] is True and out[1]["gate"]["warn"]["level"] == "good"
    # the record keeps what it knows and loses its force
    assert out[3]["released"]["codes"] == ["****-BBBB"] and out[3]["released"]["kvo"] == "10.1.2.3"
    assert out[3]["released"]["clear"] is False and out[3]["released"]["stale"] is True
    assert out[3]["gate"]["licencesReleased"] is False, (
        "a KVO that has been activated on since the release is not a released KVO")
    assert out[3]["gate"]["warn"]["level"] == "bad"
    assert "ACTIVATED on it" in out[3]["gate"]["warn"]["text"], out[3]["gate"]["warn"]["text"]
    # the host is matched the same way here as everywhere: the url form of
    # the address is the same appliance
    assert out[4]["gate"]["licencesReleased"] is False


def test_the_gate_needs_the_kvo_question_answered_not_only_an_address(tmp_path):
    """licencesReleased is what becomes --accept-licence-loss, so its
    rule is structural rather than a convention every caller keeps.

    `counts` was (ours && released.clear): it never asked whether this
    stack HAS a KVO, and leaned entirely on the page clearing kvoAddr
    whenever it cleared hasKvo. One caller forgetting that (an early
    return, a new code path) is a stack whose KVO question came back
    unknown arming on a previous stack's address. And `clear` is compared
    with === true, as licences.js writes it: a truthy value that is not
    the KVO's yes is not a yes."""
    base = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1",
            "kvoAddr": "10.1.2.3", "kvoName": "demo-kvo"}
    mine = {"kvo": "10.1.2.3", "codes": ["****-BBBB"], "clear": True}
    gates = [
        dict(base, hasKvo=True, released=mine),                             # 0 the one that counts
        dict(base, hasKvo=None, released=mine),                             # 1 could not tell
        dict(base, hasKvo=False, released=mine),                            # 2 no KVO at all
        dict(base, released=mine),                                          # 3 hasKvo not in the model
        dict(base, hasKvo=True, released=dict(mine, clear="yes")),          # 4 truthy, not the answer
        dict(base, hasKvo=True, released=dict(mine, clear=1)),              # 5 truthy, not the answer
        dict(base, hasKvo=True, released=dict(mine, stale=True)),           # 6 clear but stale
        # 7 an empty form: nothing named, so nothing to warn about yet
        dict(base, hasKvo=None, stack="", region="", typed=""),
        # 8 and 9: the record counts, and the screen is NOT armed. `counts`
        # is computed above the empty-form check, because the banner is
        # chosen on it, so both of these answered licencesReleased:true
        # beside armed:false. Nothing reads it in that state, and the one
        # value in this file that becomes --accept-licence-loss should
        # still never be true in a state the function calls unarmed.
        dict(base, hasKvo=True, released=mine, stack="", region="", typed=""),
        dict(base, hasKvo=True, released=mine, typed=""),                   # 9 name not typed back
    ]
    out = _node({"gates": gates}, tmp_path, "ui.js", "plan.js", "teardown.js")["gates"]
    assert [g["licencesReleased"] for g in out] == [True] + [False] * 9
    assert out[0]["warn"]["level"] == "good"
    assert out[1]["warn"]["level"] == "warn", "unknown warns; it does not arm"
    # the empty form draws nothing at all: the screen opens on it, and a
    # banner over a form nobody has typed in teaches the operator to
    # scroll past the one that matters
    assert out[7]["warn"] is None and out[7]["armed"] is False
    assert "Name the stack" in out[7]["why"]
    assert out[8]["armed"] is False and "Name the stack" in out[8]["why"]
    assert out[9]["armed"] is False and "Type demo" in out[9]["why"]
    # the banner still says what it knows: not armed is not "no news"
    assert out[9]["warn"]["level"] == "good"


def test_a_truncated_instance_list_never_answers_no_kvo(tmp_path, monkeypatch):
    """kvoAnswer reads api.status's by_role, which is computed over every
    instance, and not the rows, which are the first api.MAX_ROWS.

    The screen read the rows. A stack with more than 50 live instances
    whose KVO sorted past the cut answered hasKvo false, and false is the
    single value that draws no banner at all: the teardown armed with no
    warning over a KVO still holding its counts. Where an answer carries
    no by_role, a truncated list is "could not tell", which warns."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(api.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    monkeypatch.delenv("CLOUDLENS_KEY_PEM", raising=False)

    def crowd(args, region, **kw):
        if args[:2] == ["ec2", "describe-instances"]:
            rows = [{"InstanceId": "i-w%03d" % n, "InstanceType": "t3.small",
                     "State": {"Name": "running"}, "PrivateIpAddress": "10.0.0.9",
                     "Tags": [{"Key": "Name", "Value": "demo-workload-%03d" % n}]}
                    for n in range(api.MAX_ROWS + 5)]
            rows.append({"InstanceId": "i-kvo", "InstanceType": "t3.xlarge",
                         "State": {"Name": "running"}, "PublicIpAddress": "3.1.1.9",
                         "Tags": [{"Key": "Name", "Value": "demo-kvo"}]})
            return {"Reservations": [{"Instances": rows}]}
        return {"TrafficMirrorSessions": []}

    resp = _status(monkeypatch, tmp_path, aws=crowd)
    cell = resp["instances"]
    assert cell["value"]["truncated"] is True
    out = _node({"inst": cell}, tmp_path, "ui.js", "plan.js", "teardown.js")["kvoAnswer"]
    assert out["hasKvo"] is True and out["addr"] == "3.1.1.9" and out["name"] == "demo-kvo"

    # an answer with no by_role at all (an older server, or a shape this
    # page did not expect) and a truncated list: could not tell, never no
    older = {"value": {"count": cell["value"]["count"], "rows": cell["value"]["rows"], "truncated": True}}
    out = _node({"inst": older}, tmp_path, "ui.js", "plan.js", "teardown.js")["kvoAnswer"]
    assert out["hasKvo"] is None and "only the first" in out["why"], out
    # the same list untruncated is an answer, and the rows that DID arrive
    # are still evidence OF a KVO
    whole = {"value": {"count": 3, "truncated": False, "rows": [
        {"id": "i-kvo", "role": "kvo", "state": "running", "name": "demo-kvo", "public_ip": "3.1.1.9",
         "private_ip": ""}]}}
    out = _node({"inst": whole}, tmp_path, "ui.js", "plan.js", "teardown.js")["kvoAnswer"]
    assert out["hasKvo"] is True and out["addr"] == "3.1.1.9"
    part = {"value": {"count": 99, "truncated": True, "rows": whole["value"]["rows"]}}
    out = _node({"inst": part}, tmp_path, "ui.js", "plan.js", "teardown.js")["kvoAnswer"]
    assert out["hasKvo"] is True, "a KVO among the rows that arrived is still a KVO"
    none = {"value": {"count": 2, "truncated": False, "rows": [
        {"id": "i-vpb", "role": "vpb", "state": "running", "name": "demo-vpb", "public_ip": "3.1.1.3",
         "private_ip": ""}], "by_role": {"vpb": {"id": "i-vpb"}}}}
    out = _node({"inst": none}, tmp_path, "ui.js", "plan.js", "teardown.js")["kvoAnswer"]
    assert out["hasKvo"] is False and out["addr"] == "", "a whole list with no KVO is an answer"
    # a cell that carries no value is the unavailable one, with its reason
    out = _node({"inst": {"unavailable": "AccessDenied", "command": "aws ec2 describe-instances"}},
                tmp_path, "ui.js", "plan.js", "teardown.js")["kvoAnswer"]
    assert out["hasKvo"] is None and out["why"] == "AccessDenied"


def test_a_code_still_being_looked_up_is_not_a_code_the_kvo_refused(tmp_path, monkeypatch):
    """check's poll can run out of the request's budget, and poll_op
    reports that as IN_PROGRESS. Rendering it as "the KVO recognised
    nothing under this code" is how a good activation code is thrown
    away, so the row says the outcome is unknown and is not styled as a
    refusal."""
    kl = _KL()

    def timed_out(kvo, base, tok, code, verify, timeout=None):
        if code.startswith("BBBB"):
            return [("CloudLens-Credit", 10, 20)], {"state": "SUCCESS"}
        return [], {"state": "IN_PROGRESS"}

    kl.lookup_code = timed_out
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    check = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "check",
                          "codes": ["BBBB-2222-CCCC", "DDDD-3333-EEEE"]})
    rows = _node({"check": check}, tmp_path, "ui.js", "plan.js", "licences.js")["codeRows"]
    assert rows[0]["valid"] is True and rows[0]["running"] is False
    assert rows[1]["valid"] is False and rows[1]["running"] is True
    assert "outcome unknown" in rows[1]["summary"], rows[1]["summary"]
    assert "recognised nothing" not in rows[1]["summary"]


def test_the_answer_is_filed_against_the_kvo_the_question_was_sent_to(tmp_path, monkeypatch):
    """The address is read ONCE, when the call goes out, and carried to
    the callback that reads the answer.

    Every handler used to re-read the field. call() disables the buttons
    while a call runs, but not the address box, and it cannot: a licensing
    call POSTs one operation per row and polls each to its end, which is
    minutes. So an operator who retyped the address while one was in
    flight had the answer filed against whatever was in the box when it
    landed.

    Both halves are wrong, in opposite directions. A release answered
    after the edit wrote its record under an appliance nothing had been
    released from, and left the one it WAS released from with no record at
    all. An activation answered after the edit marked the OTHER appliance
    stale, which does nothing, and left this one's record saying
    clear:true, stale:false: green banner, --accept-licence-loss on the
    argv, and a KVO deleted holding a licence activated a minute earlier.
    That is the exact loss the stale rule exists to close.

    So this drives the screen itself: real API answers, the real handlers,
    and the field edited between the POST and its answer."""
    monkeypatch.setattr(api, "_kvo_license", lambda: _KL())
    creds = {"kvo": "10.1.2.3", "user": "admin", "password": "pw"}
    listed = api.licences(dict(creds, action="list"))
    released = api.licences(dict(creds, action="release",
                                 rows=[{"activationCode": "AAAA-1111-BBBB", "quantity": 5}]))
    check = api.licences(dict(creds, action="check", codes=["BBBB-2222-CCCC"]))
    activated = api.licences(dict(creds, action="activate", codes=["BBBB-2222-CCCC,10"]))
    assert released["released"] is True and released["clear"] is True
    assert activated["activated"] == 1

    armed = {"stack": "demo", "region": "us-east-1", "typed": "demo", "auditFor": "demo/us-east-1",
             "hasKvo": True, "kvoAddr": "10.1.2.3", "kvoName": "demo-kvo"}
    steps = [
        {"set": {"licKvo": "10.1.2.3", "licPass": "pw"}},
        {"click": "licLoad"},                          # 1 list: the KVO holds one licence
        {"answer": listed},
        {"click": "licRel0"},                          # 2 release that row, against 10.1.2.3
        {"set": {"licKvo": "10.9.9.9"}},               #   the operator retypes the box, mid-flight
        {"answer": released},                          #   and the answer lands
        {"read": "10.1.2.3"},                          # 0
        {"read": "10.9.9.9"},                          # 1
        {"gate": dict(armed)},                         # 2
        # and the same during an activation, which is the case that ends
        # in a green banner over an appliance holding a fresh licence
        {"set": {"licKvo": "10.1.2.3", "licEntry": "BBBB-2222-CCCC"}},
        {"click": "licAdd"},
        {"click": "licCheck"},
        {"answer": check},
        {"click": "licActivate"},                      # 3 activate, against 10.1.2.3
        {"set": {"licKvo": "10.9.9.9"}},               #   retyped again
        {"answer": activated},
        {"read": "10.1.2.3"},                          # 3
        {"read": "10.9.9.9"},                          # 4
        {"gate": dict(armed)},                         # 5
    ]
    out = _node({"flight": steps}, tmp_path, "ui.js", "plan.js", "licences.js", "teardown.js")["flight"]

    # every call was addressed to the appliance in the box when the button
    # was pressed, which is the value the callback must also use
    assert [p["action"] for p in out["posts"]] == ["list", "release", "check", "activate"]
    assert {p["kvo"] for p in out["posts"]} == {"10.1.2.3"}, out["posts"]

    rec, other, gate = out["reads"][0], out["reads"][1], out["reads"][2]["gate"]
    assert rec["record"], "the release was filed against the appliance it was sent to"
    assert rec["record"]["kvo"] == "10.1.2.3" and rec["record"]["codes"] == ["****-BBBB"]
    assert rec["record"]["clear"] is True and rec["record"]["stale"] is False
    assert other["record"] is None, (
        "a release was recorded against an appliance nothing was released from: " + str(other["record"]))
    assert gate["licencesReleased"] is True and gate["warn"]["level"] == "good"

    after, still_none, gate2 = out["reads"][3], out["reads"][4], out["reads"][5]["gate"]
    assert after["record"]["stale"] is True and after["record"]["clear"] is False, (
        "the activation landed on 10.1.2.3, so 10.1.2.3's release stopped being evidence: " +
        str(after["record"]))
    assert still_none["record"] is None
    assert gate2["licencesReleased"] is False, (
        "a KVO activated on since its release must not arm --accept-licence-loss")
    assert gate2["warn"]["level"] == "bad" and "ACTIVATED on it" in gate2["warn"]["text"]


def test_the_plan_pages_escaper_is_the_shared_one(tmp_path):
    """plan.js kept an esc() of its own over [&<>"], and wizard.js takes
    ITS escaper from plan.js: the screen that builds the most markup by
    concatenation - VPC names, instance names, AWS error text - used the
    weaker of the console's two rules, and a value carrying an apostrophe
    walked out of any single-quoted attribute. There is one escaper, in
    ui.js, and this is it."""
    out = _node({"escape": "a\'b<c>&\"d"}, tmp_path, "ui.js", "plan.js")
    assert out["escaped"] == "a&#39;b&lt;c&gt;&amp;&quot;d", out["escaped"]
