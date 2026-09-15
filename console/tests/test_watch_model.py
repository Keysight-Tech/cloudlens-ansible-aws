"""The Watch screen's model, held to the events the script actually writes.

web/watch.js is split in two on purpose: applyEvent(model, ev) is a pure
function of a model and one frame (no DOM, no network, no globals) and
render(model) does every bit of the DOM. This file tests the first half, in
the only two ways that are worth anything without a browser:

  contract   deploy-stack.sh's own emit_event calls are parsed out of the
             script, turned into the JSON lines it writes, and read back
             through events.from_script. Every field watch.js says it reads
             (its READS table) has to be a field the script emits for that
             type, and every field access in the file has to be in that
             table. A key renamed in the script, or a field read that no
             event carries, fails here instead of quietly emptying a card.
  behaviour  the same frames are fed through the real applyEvent under node,
             and the model that comes back is checked. Skipped, with the
             reason, on a machine with no node.
  screen     render() drawn into a DOM stub, also under node, for the two
             things a redraw carries between frames that the model does not
             hold: the open prompt's input box and the log lines already on
             the page. Neither can be tested from the model, and both have
             already been the site of a defect.

Run:  cd console && python3 -m pytest tests/test_watch_model.py -q
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from cloudlens_console import events as E

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.abspath(os.path.join(HERE, ".."))
WEB = os.path.join(CONSOLE, "cloudlens_console", "web")
WATCH = os.path.join(WEB, "watch.js")
APP = os.path.join(WEB, "app.js")
DEPLOY = os.path.abspath(os.path.join(CONSOLE, "..", "deploy", "deploy-stack.sh"))
TS = "2026-09-09T10:00:00Z"

# fields that exist on a console event of that type and never on the script's
CONSOLE_ONLY = {
    "hello": {"account", "arn"},      # the console's own hello, before the engine starts
    "done": {"summary"},              # the console's done; the script's says status
}
# types the console alone produces: no script emit call to compare them with.
# `answered` is one of them - the console mints it where it writes an answer
# to the FIFO, and its contract is with events.py, held below.
CONSOLE_TYPES = {"log", "error", "answered"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------- what watch.js reads
def _reads():
    """The READS table in watch.js: {type: {field, ...}}."""
    block = re.search(r"var READS=\{(.*?)\n\};", _read(WATCH), re.S)
    assert block, "watch.js declares var READS={...};"
    table = {}
    for m in re.finditer(r"(\w+):\[([^\]]*)\]", block.group(1)):
        table[m.group(1)] = set(re.findall(r'"([^"]+)"', m.group(2)))
    assert table, block.group(1)
    return table


def _js_object_keys(name):
    """The keys of a flat `var NAME={a:"x",b:"y"}` object in watch.js."""
    return set(re.findall(r"(\w+):", _js_object_body(name)))


def _js_object_values(name):
    """The quoted values of that same object."""
    return set(re.findall(r':"([^"]*)"', _js_object_body(name)))


def _js_object_body(name):
    m = re.search(r"var %s=\{(.*?)\};" % name, _read(WATCH), re.S)
    assert m, "watch.js declares var %s={...};" % name
    return m.group(1)


def _app_icon_keys():
    """The keys of app.js's IC set, which is what window.clConsole.icons is:
    one `name:'<svg ...>'` per line."""
    m = re.search(r"var IC=\{(.*?)\n\};", _read(APP), re.S)
    assert m, "app.js declares var IC={...};"
    return set(re.findall(r"^\s*(\w+):", m.group(1), re.M))


# --------------------------------------------------- what the script emits
def _script_emits():
    """{type: {key, ...}} from deploy-stack.sh's own emit_event / emit_done
    calls. Comments are dropped first, or the sink's own `# emit_event TYPE
    key=value` documentation would be read as a call."""
    lines = [l for l in _read(DEPLOY).splitlines() if not l.lstrip().startswith("#")]
    src = re.sub(r"\\\n\s*", " ", "\n".join(lines))     # a call can wrap on a backslash
    emits = {}
    for m in re.finditer(r"\bemit_event\s+([a-z_]+)([^\n;]*)", src):
        emits.setdefault(m.group(1), set()).update(re.findall(r"\b([a-z_][a-z0-9_]*)=", m.group(2)))
    for m in re.finditer(r"\bemit_done\b([^\n;]*)", src):
        emits.setdefault("done", set()).update(re.findall(r"\b([a-z_][a-z0-9_]*)=", m.group(1)))
    assert set(emits) >= {"hello", "phases", "phase", "resource", "check", "prompt", "login", "done"}, sorted(emits)
    return emits


def _resource_kinds():
    """Every kind= the script's resource events carry."""
    src = "\n".join(l for l in _read(DEPLOY).splitlines() if not l.lstrip().startswith("#"))
    return set(re.findall(r"emit_event resource kind=(\w+)", src))


def _phase_order():
    return re.search(r'^PHASE_ORDER="([^"]+)"', _read(DEPLOY), re.M).group(1).split()


# ------------------------------------------------------------ the contract
def test_every_field_the_model_reads_is_one_the_script_emits():
    """The drift test. from_script files the script's own `id` as
    <type>_id (the console's id is stamped over `id` at emit), so that
    rename is applied to the script's side before the comparison."""
    reads, emits = _reads(), _script_emits()
    for typ, fields in sorted(reads.items()):
        if typ in CONSOLE_TYPES:
            continue
        assert typ in emits, "watch.js reads a %s event nothing emits" % typ
        emitted = set(emits[typ])
        if "id" in emitted:
            emitted = (emitted - {"id"}) | {typ + "_id"}
        missing = fields - CONSOLE_ONLY.get(typ, set()) - emitted
        assert not missing, "watch.js reads %s.%s, which deploy-stack.sh never writes" % (
            typ, sorted(missing))


def test_the_screen_reads_every_field_the_operator_came_for():
    """The other direction, for the fields the screen exists to show: a
    login has to say where the password is, a failure has to say which
    phase and why, a done has to name its report."""
    reads = _reads()
    assert {"component", "url", "user", "password_in"} <= reads["login"]
    assert {"status", "phase", "reason", "report"} <= reads["done"]
    assert {"item", "status", "fix"} <= reads["check"]
    assert {"name", "status", "reason"} <= reads["phase"]
    assert {"question", "default", "kind", "prompt_id"} <= reads["prompt"]
    assert {"ingress_ip", "egress_ip", "count", "cluster"} <= reads["resource"]


def test_no_event_field_named_password_is_read_or_emitted():
    """password_in names WHERE the password is. There is no password field
    in any event, and this screen must never grow one."""
    for typ, fields in _reads().items():
        assert "password" not in fields, typ
    for typ, keys in _script_emits().items():
        assert "password" not in keys, typ


def test_every_field_access_in_watch_js_is_declared_in_reads():
    """READS is the file's own account of what it reads, and the tests above
    only check that account. This checks the account is complete: every
    ev.field and ev["field"] in the source is in the table."""
    src = _read(WATCH)
    used = set(re.findall(r"\bev\.([A-Za-z_]\w*)", src)) | set(re.findall(r'\bev\["([^"]+)"\]', src))
    used -= {"type", "id"}          # the envelope every frame carries
    declared = set()
    for fields in _reads().values():
        declared |= fields
    assert used, "watch.js reads fields off the frames"
    assert used <= declared, "read but not declared in READS: %s" % sorted(used - declared)


def test_the_model_knows_every_resource_kind_and_every_phase_the_script_has():
    """A kind or a phase added to deploy-stack.sh fails here until the Watch
    screen names it: an unnamed kind would draw a node labelled with its own
    raw kind, and an unnamed phase would map to no node at all."""
    assert _resource_kinds() <= _js_object_keys("NODE_LABEL"), sorted(
        _resource_kinds() - _js_object_keys("NODE_LABEL"))
    assert _js_object_keys("NODE_ICON") == _js_object_keys("NODE_LABEL")
    order = set(_phase_order())
    assert _js_object_keys("PHASE_NODE") <= order, "a phase nothing runs: %s" % sorted(
        _js_object_keys("PHASE_NODE") - order)


def test_every_node_icon_names_an_icon_the_instrument_really_has():
    """NODE_ICON's VALUES, not just its keys: each one is a key into app.js's
    IC set, which reaches this screen as window.clConsole.icons. icon()
    returns "" for a name that set has not got, so an icon renamed in app.js
    empties every chip on the topology and nothing fails anywhere - the chips
    just go blank. This is what fails instead."""
    want, have = _js_object_values("NODE_ICON"), _app_icon_keys()
    assert want, "NODE_ICON maps each node kind to an icon"
    assert have, "app.js declares the icon set"
    assert want <= have, "watch.js draws icons app.js has not got: %s" % sorted(want - have)


def test_the_answered_frame_carries_exactly_what_the_model_reads():
    """`answered` is the console's own frame, not the script's: it is what
    says a question was settled, which nothing in the stream said before.
    Its fields and watch.js's account of them are one set, so a field added
    on one side and not the other fails here. It stays out of SCRIPT_TYPES,
    the set of types deploy-stack.sh writes: an `answered` line appearing on
    the events file is not the console's own record of an answer, and is
    read as the raw log line it is."""
    ev = E.answered("p1", "********")
    assert ev["type"] == E.ANSWERED == "answered"
    assert set(ev) - {"type"} == _reads()["answered"] == {"prompt_id", "shown"}
    assert E.ANSWERED not in E.SCRIPT_TYPES
    assert E.from_script({"seq": 4, "ts": TS, "type": "answered",
                          "prompt_id": "p1", "shown": "x"})["type"] == E.LOG


def test_the_answered_frame_is_a_type_the_screen_listens_for():
    """A named SSE event with no addEventListener is never delivered, so
    watch.js's TYPES is what carries this frame to the page at all: without
    it the browser would be sent every answer and hear none of them."""
    m = re.search(r"var TYPES=\[([^\]]*)\];", _read(WATCH))
    assert m, "watch.js declares var TYPES=[...];"
    types = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert "answered" in types, sorted(types)
    assert E.SCRIPT_TYPES <= types, "a script type the screen never hears: %s" % sorted(
        E.SCRIPT_TYPES - types)


def test_the_phases_event_is_a_type_the_console_relays():
    """The phase list only reaches the browser if events.py knows the type;
    an unknown one arrives as a log of the raw line and no timeline is
    drawn."""
    assert "phases" in E.SCRIPT_TYPES
    ev = E.from_script({"seq": 2, "ts": TS, "type": "phases", "order": "stack wait"})
    assert ev["type"] == "phases" and ev["order"] == "stack wait"


# ------------------------------------------------------- the recorded runs
def _frames(emits, rows):
    """The rows as deploy-stack.sh writes them: one JSON object per line,
    seq counted from 1, ts on every one. Each row's keys are held to the
    keys the script's own emit calls for that type use, so a fixture here
    cannot drift into a shape the script never writes."""
    out = []
    for seq, (typ, fields) in enumerate(rows, start=1):
        if typ in emits:
            unknown = set(fields) - set(emits[typ])
            assert not unknown, "deploy-stack.sh never writes %s.%s" % (typ, sorted(unknown))
        row = {"seq": seq, "ts": TS, "type": typ}
        row.update(fields)
        out.append(row)
    return out


def _run_rows(order):
    """One deploy as the script reports it: the interview, the stack, the
    logins, a phase that fails, and the done."""
    return [
        ("hello", {"stack": "", "region": "us-east-1", "dry_run": "false"}),
        ("phases", {"order": " ".join(order)}),
        ("prompt", {"id": "p1", "question": "Stack name [cloudlens-stack]: ",
                    "default": "cloudlens-stack", "kind": "text"}),
        ("prompt", {"id": "p2", "question": "KVO admin password: ", "kind": "secret"}),
        ("hello", {"stack": "lab", "region": "us-east-1", "dry_run": "false"}),
        ("check", {"item": "aws cli present", "status": "pass"}),
        ("check", {"item": "AWS credentials", "status": "fail", "fix": "aws configure"}),
        ("phase", {"name": "stack", "status": "done", "reason": ""}),
        ("resource", {"kind": "vpc", "id": "vpc-0abc"}),
        ("resource", {"kind": "subnet", "id": "subnet-mgmt", "role": "mgmt", "zone": "us-east-1a"}),
        ("resource", {"kind": "subnet", "id": "subnet-in", "role": "ingress"}),
        ("resource", {"kind": "subnet", "id": "subnet-eg", "role": "egress"}),
        ("resource", {"kind": "vcontroller", "ip": "1.2.3.4", "private_ip": "10.0.0.4"}),
        ("resource", {"kind": "kvo", "ip": "1.2.3.5", "private_ip": "10.0.0.5"}),
        ("resource", {"kind": "vpb", "ip": "1.2.3.6", "ingress_ip": "10.0.1.6", "egress_ip": "10.0.2.6"}),
        ("login", {"component": "vcontroller", "url": "https://1.2.3.4/cloudlens/login",
                   "user": "admin", "password_in": "cloudlens-vcontroller-creds.json"}),
        ("login", {"component": "kvo", "url": "https://1.2.3.5/", "user": "admin",
                   "password_in": "KVO factory default"}),
        ("login", {"component": "vpb", "url": "ssh -p 9022 admin@1.2.3.6", "user": "admin",
                   "password_in": "lab.pem (EC2 key pair, no password)"}),
        ("resource", {"kind": "workloads", "count": "", "tag": "cloudlens=yes", "mode": "tag"}),
        ("resource", {"kind": "workloads", "count": "3", "tag": "cloudlens=yes",
                      "mode": "tag", "created": "true"}),
        ("resource", {"kind": "eks", "cluster": "lab-eks", "mode": "daemonset"}),
        ("phase", {"name": "wait", "status": "done", "reason": ""}),
        ("phase", {"name": "sensors", "status": "skipped", "reason": "no tagged instances"}),
        ("phase", {"name": "license", "status": "failed", "reason": "KVO licensing did not complete"}),
        ("done", {"status": "ok", "report": "deploy-report-lab-us-east-1.html",
                  "profile": "deploy-profile-lab.env"}),
    ]


def _events(rows, tmp_path):
    """The rows through the real reader: the file the script appends to,
    read by events.iter_script_events, with the console ids a Job stamps."""
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
    offset, evs = E.iter_script_events(str(path))
    assert len(evs) == len(rows), (len(evs), len(rows))
    for i, ev in enumerate(evs, start=1):
        ev["id"] = i
    return evs


def test_the_recorded_run_carries_every_field_the_model_reads(tmp_path):
    """No node needed: the frames the script writes, read back the way the
    console reads them, carry the fields watch.js goes looking for."""
    emits = _script_emits()
    evs = _events(_frames(emits, _run_rows(_phase_order())), tmp_path)
    reads = _reads()
    seen = {}
    for ev in evs:
        seen.setdefault(ev["type"], set()).update(k for k, v in ev.items() if v != "")
    assert "prompt_id" in seen["prompt"], "the script's id must arrive as prompt_id"
    assert "resource_id" in seen["resource"], "the script's id must arrive as resource_id"
    for typ in ("phases", "phase", "resource", "check", "login", "prompt", "done"):
        wanted = reads[typ] - CONSOLE_ONLY.get(typ, set())
        # the run above exercises each type; every field it reads shows up on
        # at least one frame of that type, with a value
        assert wanted & seen[typ], (typ, sorted(seen[typ]))
    assert seen["login"] >= {"component", "url", "user", "password_in"}


# ---------------------------------------------------------- under node
HARNESS = r"""
const fs = require("fs");
global.window = {};
new Function(fs.readFileSync(process.argv[2], "utf8"))();
const W = global.window.clWatch;
const evs = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
let m = W.emptyModel();
evs.forEach(function(ev){ W.applyEvent(m, ev); });
const out = {
  model: m,
  verdict: W.verdict(m),
  open: W.openPrompt(m),
  states: {},
  logMax: W.LOG_MAX,
  phaseMax: W.PHASE_MAX
};
Object.keys(m.nodes).forEach(function(k){ out.states[k] = W.nodeState(m, k); });
// answering the open question moves the screen on to the next one
if (out.open) {
  W.noteAnswer(m, out.open.prompt_id, "********", null);
  out.afterAnswer = W.openPrompt(m) ? W.openPrompt(m).prompt_id : null;
  out.answered = m.prompts.map(function(p){ return p.answer; });
}
process.stdout.write(JSON.stringify(out));
"""


# The same model, read without answering anything: what a page that has just
# attached to a run holds, which is the whole point of the `answered` frame.
REPLAY_HARNESS = r"""
const fs = require("fs");
global.window = {};
new Function(fs.readFileSync(process.argv[2], "utf8"))();
const W = global.window.clWatch;
let m = W.emptyModel();
JSON.parse(fs.readFileSync(process.argv[3], "utf8")).forEach(function(ev){ W.applyEvent(m, ev); });
const open = W.openPrompt(m);
process.stdout.write(JSON.stringify({
  // renderModal opens on exactly this and nothing else, so a null here is
  // "no modal" as surely as reading the DOM would be
  open: open ? open.prompt_id : null,
  prompts: m.prompts.map(function(p){ return {id: p.prompt_id, answer: p.answer, kind: p.kind}; })
}));
"""


def _node(evs, tmp_path, harness_js=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: running watch.js needs it")
    harness = tmp_path / "harness.js"
    harness.write_text(harness_js or HARNESS, encoding="utf-8")
    frames = tmp_path / "frames.json"
    frames.write_text(json.dumps(evs), encoding="utf-8")
    proc = subprocess.run([node, str(harness), WATCH, str(frames)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def order():
    return _phase_order()


def test_applyEvent_builds_the_run_from_its_events(tmp_path, order):
    evs = _events(_frames(_script_emits(), _run_rows(order)), tmp_path)
    out = _node(evs, tmp_path)
    m = out["model"]

    # the timeline: the whole list up front, from the script's own event,
    # and only the phases that reported carry a status
    assert m["phaseOrder"] == order
    assert m["phases"]["stack"]["status"] == "done"
    assert m["phases"]["sensors"] == {"name": "sensors", "status": "skipped",
                                      "reason": "no tagged instances"}
    assert m["phases"]["license"]["status"] == "failed"
    assert "vpb" not in m["phases"], "a phase that never reported has no status"

    # the identity chip: the last hello wins, and it is the script's
    assert m["hello"]["stack"] == "lab" and m["hello"]["region"] == "us-east-1"
    assert m["hello"]["dryRun"] is False

    # the topology: one node per resource, the three subnets keyed by role
    assert set(m["nodes"]) == {"vpc", "subnet:mgmt", "subnet:ingress", "subnet:egress",
                               "vcontroller", "kvo", "vpb", "workloads", "eks"}
    assert m["nodes"]["vpc"]["resource_id"] == "vpc-0abc"
    assert m["nodes"]["vpb"]["ingress_ip"] == "10.0.1.6"
    assert m["nodes"]["vpb"]["egress_ip"] == "10.0.2.6"
    assert m["nodes"]["eks"]["cluster"] == "lab-eks"
    # workloads reported twice: the empty count never overwrote the real one
    assert m["nodes"]["workloads"]["count"] == "3"
    assert m["nodes"]["workloads"]["created"] == "true"

    # a failed phase reddens the node it is unambiguously about, and only it
    assert out["states"]["kvo"] == "fail", "license failed, and license is KVO's"
    assert out["states"]["vcontroller"] == "live"
    assert out["states"]["vpc"] == "live"

    # the logins: where the password is, never what it is
    assert [l["component"] for l in m["logins"]] == ["vcontroller", "kvo", "vpb"]
    assert m["logins"][1]["password_in"] == "KVO factory default"
    assert m["logins"][2]["url"].startswith("ssh "), "the vPB announces a command, not a URL"

    assert [c["status"] for c in m["checks"]] == ["pass", "fail"]
    assert m["checks"][1]["fix"] == "aws configure"
    assert m["done"]["report"] == "deploy-report-lab-us-east-1.html"
    assert out["verdict"] == "complete"
    assert m["lastId"] == len(evs)


def test_no_value_the_model_holds_is_a_password(tmp_path, order):
    """Nothing in the model is called password, at any depth: the only thing
    it holds about one is the sentence saying where it lives."""
    out = _node(_events(_frames(_script_emits(), _run_rows(order)), tmp_path), tmp_path)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k != "password", node
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out["model"])


def test_the_prompts_are_the_questions_the_run_is_blocked_on(tmp_path, order):
    evs = _events(_frames(_script_emits(), _run_rows(order)), tmp_path)
    out = _node(evs, tmp_path)
    ids = [p["prompt_id"] for p in out["model"]["prompts"]]
    assert ids == ["p1", "p2"], ids
    assert out["model"]["prompts"][1]["kind"] == "secret"
    assert out["model"]["prompts"][0]["def"] == "cloudlens-stack"
    # the open one is the last unanswered question; answering it moves on
    assert out["open"]["prompt_id"] == "p2"
    assert out["afterAnswer"] == "p1", "p1 was never answered, so it is still open"
    assert out["answered"] == [None, "********"]


def test_a_replayed_frame_does_not_duplicate_what_it_already_said(tmp_path, order):
    """A reconnect replays from the last id, but a stream re-read from 0
    (a fresh attach) sends every frame again. The same prompt, login,
    resource or check must land on the same entry, not a second one."""
    rows = _run_rows(order)
    evs = _events(_frames(_script_emits(), rows + rows), tmp_path)
    out = _node(evs, tmp_path)
    m = out["model"]
    assert [p["prompt_id"] for p in m["prompts"]] == ["p1", "p2"]
    assert [l["component"] for l in m["logins"]] == ["vcontroller", "kvo", "vpb"]
    assert set(m["nodes"]) == {"vpc", "subnet:mgmt", "subnet:ingress", "subnet:egress",
                               "vcontroller", "kvo", "vpb", "workloads", "eks"}
    # checks were the one list that still grew on a replay: a doctor run's
    # table doubled, every row twice, on nothing worse than a reconnect
    assert [c["item"] for c in m["checks"]] == ["aws cli present", "AWS credentials"]
    assert m["checks"][1]["status"] == "fail" and m["checks"][1]["fix"] == "aws configure"


def test_a_check_that_reports_again_updates_its_row_and_keeps_the_fix_it_carried(tmp_path):
    """The same item twice is the same row, and the second report is the one
    that stands: a check that failed and then passed reads pass. The fix from
    the failing frame stays in the MODEL - copy() will not overwrite a known
    value with an empty one, and a doctor re-run may fail the item again - so
    it is the passing ROW, not the model, that declines to print it. That half
    is test_a_passing_check_stops_telling_the_reader_how_to_fix_it."""
    evs = [{"type": "check", "item": "AWS credentials", "status": "fail",
            "fix": "aws configure", "id": 1},
           {"type": "check", "item": "AWS credentials", "status": "pass", "id": 2},
           {"type": "check", "item": "", "status": "warn", "id": 3},
           {"type": "check", "item": "", "status": "warn", "id": 4}]
    m = _node(evs, tmp_path)["model"]
    assert [c["item"] for c in m["checks"]] == ["AWS credentials", "", ""], m["checks"]
    assert m["checks"][0]["status"] == "pass"
    assert m["checks"][0]["fix"] == "aws configure", "the fix the first frame carried stands"


def test_a_malformed_phase_list_cannot_become_a_thousand_rows(tmp_path):
    """The timeline is drawn from `order`, one row per name. A frame that
    repeated a name, or carried hundreds of them, drew every one: this is a
    dozen short names in the script and nothing else has to be entertained."""
    order = " ".join(["stack", "stack", "wait"] + ["p%d" % i for i in range(500)])
    out = _node([{"type": "phases", "order": order, "id": 1}], tmp_path)
    got = out["model"]["phaseOrder"]
    assert out["phaseMax"] <= 100, out["phaseMax"]
    assert len(got) == out["phaseMax"], len(got)
    assert got[:3] == ["stack", "wait", "p0"], "deduped, in the order the frame gave"
    assert len(set(got)) == len(got), "one row per name"


def test_a_failed_run_says_so_and_an_unknown_type_changes_nothing(tmp_path, order):
    rows = [
        ("hello", {"stack": "lab", "region": "us-east-1", "dry_run": "false"}),
        ("phases", {"order": " ".join(order)}),
        ("phase", {"name": "stack", "status": "failed", "reason": "no Elastic IPs left"}),
        ("done", {"status": "failed", "phase": "stack", "reason": "CloudFormation rolled back",
                  "code": "1"}),
    ]
    evs = _events(_frames(_script_emits(), rows), tmp_path)
    evs.append({"type": "mystery", "id": 99, "what": "?"})
    out = _node(evs, tmp_path)
    m = out["model"]
    assert out["verdict"] == "failed"
    assert m["done"]["reason"] == "CloudFormation rolled back" and m["done"]["code"] == "1"
    assert m["phases"]["stack"]["status"] == "failed"
    assert not m["nodes"], "nothing was reported, so nothing is drawn"
    assert m["events"] == len(evs), "an unknown frame is counted and otherwise ignored"


def test_the_raw_drawer_keeps_the_tail_and_counts_the_whole_run(tmp_path, order):
    """log frames are the console's own (one per line of the engine's
    stdout), so they are built here rather than from a script emit."""
    evs = [{"type": "log", "text": "line %d" % i, "id": i} for i in range(1, 1201)]
    out = _node(evs, tmp_path)
    m = out["model"]
    assert m["logCount"] == 1200, "the badge counts every line the run printed"
    assert len(m["logs"]) == out["logMax"], "the drawer keeps the tail"
    assert m["logs"][-1] == "line 1200"


def test_a_console_error_ends_the_run(tmp_path):
    """The engine that could not start: no script event ever arrives, and
    the screen must not sit on "running" forever."""
    evs = [{"type": "error", "id": 1, "text": "could not start the engine: no such file",
            "fix": "Check that the deploy script exists and can run."}]
    out = _node(evs, tmp_path)
    assert out["verdict"] == "failed"
    assert out["model"]["ended"] is True
    assert out["model"]["error"]["fix"].startswith("Check that")


# ------------------------------------------------- the answers in the stream
def _replayed(tmp_path, rows):
    """One SSE replay as a page attaching mid-run receives it: the script's
    own rows through the real reader, the console's `answered` frames spliced
    in where job.answer emitted them, and ids stamped in buffer order the way
    Job.emit stamps them. An answered row cannot go through the script reader:
    it is not a type deploy-stack.sh writes, and from_script would rightly
    read it as a log line."""
    script = [r for r in rows if r[0] != "answered"]
    from_script = iter(_events(_frames(_script_emits(), script), tmp_path))
    out = [E.answered(f["prompt_id"], f["shown"]) if t == "answered" else next(from_script)
           for t, f in rows]
    for i, ev in enumerate(out, start=1):
        ev["id"] = i
    return out


ASKED = [
    ("hello", {"stack": "lab", "region": "us-east-1", "dry_run": "false"}),
    ("prompt", {"id": "p1", "question": "Stack name [cloudlens-stack]: ",
                "default": "cloudlens-stack", "kind": "text"}),
    ("answered", {"prompt_id": "p1", "shown": "lab"}),
    ("prompt", {"id": "p2", "question": "KVO admin password: ", "kind": "secret"}),
]


def test_a_replay_leaves_open_only_the_question_with_no_answer(tmp_path):
    """The defect this frame exists for. The answer used to live only in the
    page that typed it, so a reload of a run that had already answered
    replayed every prompt unanswered: the modal opened on the newest one, on
    a question the engine was not waiting on, and there is no cancel on it.
    Answering it reached job.answer's pending-prompt check and came back 409,
    with the modal still open. With the answer in the stream the replay
    settles p1 and only p2 - the one the run really is blocked on - is open."""
    out = _node(_replayed(tmp_path, ASKED), tmp_path, REPLAY_HARNESS)
    assert [p["id"] for p in out["prompts"]] == ["p1", "p2"]
    assert out["prompts"][0]["answer"] == "lab", "the frame's shown, on the question it names"
    assert out["prompts"][1]["answer"] is None
    assert out["open"] == "p2", "exactly one question is open, and it is the last one"


def test_a_replay_of_an_answered_run_opens_no_modal_at_all(tmp_path):
    """The other resumed page: every question the run asked has an answer in
    the stream, so nothing is open and the overlay never appears. The secret
    shows as the asterisks job.answer put on it; the value it masks was never
    in the stream to replay."""
    rows = ASKED + [("answered", {"prompt_id": "p2", "shown": "********"})]
    out = _node(_replayed(tmp_path, rows), tmp_path, REPLAY_HARNESS)
    assert out["open"] is None, "no open question, so renderModal has nothing to open on"
    assert [p["answer"] for p in out["prompts"]] == ["lab", "********"]
    assert out["prompts"][1]["kind"] == "secret", "still the question it was"


def test_an_answered_frame_for_a_question_never_asked_changes_nothing(tmp_path):
    """The prompt always reaches the buffer before an answer to it can (a job
    has nothing pending until it emits one), so this cannot arrive orphaned in
    a run; if it ever did, it would not invent a question nobody was asked."""
    rows = [("hello", {"stack": "lab", "region": "us-east-1", "dry_run": "false"}),
            ("answered", {"prompt_id": "ghost", "shown": "x"})]
    out = _node(_replayed(tmp_path, rows), tmp_path, REPLAY_HARNESS)
    assert out["prompts"] == [] and out["open"] is None


# ------------------------------------------------- the screen, under a DOM
# The half above is applyEvent, which never touches the DOM. This half is
# render(), run against a DOM small enough to read: the two pieces of state
# a redraw carries between frames (the open prompt's input, and the log
# lines already on the page) are exactly the two the model does not hold,
# so they cannot be tested from the model at all.
DOM_HARNESS = r"""
const fs = require("fs");

/* Elements hold children, a value, a type and a dataset. scrollHeight is ten
   pixels a line against a fifty-pixel viewport, so "already at the bottom"
   means in here what it means in a browser. Nothing else is pretended. */
function El(tag){
  this.tag=tag; this.dataset={}; this.className=""; this.value=""; this.type="text";
  this.hidden=false; this.disabled=false; this.focused=false;
  this.scrollTop=0; this.clientHeight=50; this.kids=[]; this._text=""; this._html="";
}
El.prototype.addEventListener=function(t,fn){ (this.on=this.on||{})[t]=fn; };
El.prototype.focus=function(){this.focused=true;};
El.prototype.appendChild=function(c){this.kids.push(c);return c;};
El.prototype.removeChild=function(c){var i=this.kids.indexOf(c);if(i>=0)this.kids.splice(i,1);return c;};
Object.defineProperty(El.prototype,"textContent",{
  get:function(){return this._text;},
  set:function(v){this._text=String(v);this.kids=[];}});
Object.defineProperty(El.prototype,"innerHTML",{
  get:function(){return this._html;},
  set:function(v){this._html=String(v);this.kids=[];}});
Object.defineProperty(El.prototype,"firstChild",{get:function(){return this.kids[0]||null;}});
Object.defineProperty(El.prototype,"childNodes",{get:function(){return this.kids;}});
Object.defineProperty(El.prototype,"scrollHeight",{get:function(){return this.kids.length*10;}});

const els={};
function $(id){ if(!els[id])els[id]=new El("div"); return els[id]; }
global.document={getElementById:$, createElement:function(t){return new El(t);}};
global.localStorage={getItem:function(){return null;},setItem:function(){}};
let queue=[];
global.requestAnimationFrame=function(fn){queue.push(fn);return queue.length;};
function flush(){ const q=queue; queue=[]; q.forEach(function(fn){fn();}); return q.length; }

/* the rest of the browser the file touches: the stream it opens, the answer
   dialog it puts up, and the one request it sends */
let stream=null;
global.EventSource=function(url){ this.url=url; this.readyState=0; this.heard={};
  stream=this;
  this.addEventListener=function(t,fn){ this.heard[t]=fn; };
  this.close=function(){ this.closed=true; }; };
global.EventSource.CLOSED=2;
let confirmSays=false; const confirmed=[];
const sent=[]; let reply={ok:true,status:200};
/* holdNext parks the NEXT request instead of answering it, so a POST can be
   left in flight while the page is driven on to another run; release(...)
   is how it finally comes back. send() reads the body, Stop does not. */
let holdNext=false, release=null;
function answered(body){ return {ok:true,status:200,text:function(){return Promise.resolve(body||"{}");}}; }
global.fetch=function(url,opts){
  sent.push({url:url,method:opts&&opts.method,body:opts&&opts.body});
  if(holdNext){ holdNext=false; return new Promise(function(res){ release=res; }); }
  return Promise.resolve(reply);
};
global.window={confirm:function(msg){ confirmed.push(msg); return confirmSays; }};
const settle=function(){ return Promise.resolve().then().then().then(); };
new Function(fs.readFileSync(process.argv[2],"utf8"))();
const W=global.window.clWatch;
flush();                                    // the draw init() asked for on load
const out={};

function frame(ev){ stream.heard[ev.type]({data:JSON.stringify(ev)}); }
function lines(){ return $("wLog").kids.map(function(k){ return k.textContent; }); }

/* 1. two runs, one prompt id. p1, p2, p4 are per-run counters: every run has
      a p4, and two runs' p4 are not the same question. attach() is how the
      page gets to a run: the wizard's Launch calls it, and so does the
      resume form, which is what makes this a plain afternoon's sequence. */
W.attach("runA");
frame({id:1,type:"prompt",prompt_id:"p4",question:"KVO admin password: ",kind:"secret"});
flush();
out.runA={question:$("wPromptQ").textContent,type:$("wPromptInput").type,hidden:$("wPrompt").hidden};
$("wPromptInput").value="Zq7-CANARY-typed-into-run-A";     // and the operator types

W.attach("runB");
frame({id:1,type:"prompt",prompt_id:"p4",question:"Region [us-east-1]: ",kind:"text",
       "default":"us-east-1"});
flush();
out.runB={question:$("wPromptQ").textContent,type:$("wPromptInput").type,
          value:$("wPromptInput").value,hidden:$("wPrompt").hidden,
          hint:$("wPromptHint").textContent};

/* the same run and the same question, redrawn by the next frame to arrive:
   the box is left alone, which is the whole reason the modal has a key */
$("wPromptInput").value="us-west-2";
frame({id:2,type:"log",text:"still going"});
flush();
out.sameQuestion={value:$("wPromptInput").value,question:$("wPromptQ").textContent};

/* answered: the modal closes, and takes what was typed with it */
frame({id:3,type:"answered",prompt_id:"p4",shown:"us-west-2"});
flush();
out.closed={hidden:$("wPrompt").hidden,value:$("wPromptInput").value,
            key:$("wPrompt").dataset.promptId};

/* 2. the log drawer, appended to and not rebuilt */
const c=W.emptyModel(); c.job="runL"; c.stream="live";
for(let i=1;i<=3;i++)W.applyEvent(c,{type:"log",text:"line "+i,id:i});
W.render(c); flush();
$("wLog").kids.forEach(function(k){ k.fromTheFirstDraw=true; });
for(let i=4;i<=6;i++)W.applyEvent(c,{type:"log",text:"line "+i,id:i});
W.render(c); flush();
out.log={lines:lines(),count:$("wLogCount").textContent,scrollTop:$("wLog").scrollTop,
         kept:$("wLog").kids.filter(function(k){ return k.fromTheFirstDraw; }).length};

/* a reader scrolled up in the drawer is reading it: the tail is not yanked
   back under them */
$("wLog").scrollTop=0;
W.applyEvent(c,{type:"log",text:"line 7",id:7});
W.render(c); flush();
out.scrolledUp={scrollTop:$("wLog").scrollTop,last:lines()[lines().length-1],nodes:lines().length};

/* past LOG_MAX the page keeps the tail, as the model does */
for(let i=8;i<=W.LOG_MAX+120;i++)W.applyEvent(c,{type:"log",text:"line "+i,id:i});
W.render(c); flush();
out.overflow={nodes:$("wLog").kids.length,first:lines()[0],last:lines()[lines().length-1],
              count:$("wLogCount").textContent,logMax:W.LOG_MAX};

/* another run: the drawer is that run's, not the last one's tail */
const d=W.emptyModel(); d.job="runM";
W.applyEvent(d,{type:"log",text:"a fresh run",id:1});
W.render(d); flush();
out.newRun={lines:lines()};

/* 3. one draw per frame, and of the newest model */
const e1=W.emptyModel(); e1.job="jx";
const e2=W.emptyModel(); e2.job="jy";
W.render(e1); W.render(e1); W.render(e2);
out.queued=queue.length;
flush();
out.drawn=$("wRunId").textContent;
W.render(e2);
out.queuedAgain=queue.length;
flush();

/* 4. the checks table: an item that failed with a fix and then passed. The
      model keeps the fix (a later frame that omits a field is "not known",
      not "cleared"), and the row is where it stops being printed. */
const ck=W.emptyModel(); ck.job="runK";
W.applyEvent(ck,{type:"check",item:"AWS credentials",status:"fail",fix:"aws configure",id:1});
W.render(ck); flush();
out.checkWhileFailing=$("wChecks").innerHTML;
W.applyEvent(ck,{type:"check",item:"AWS credentials",status:"pass",id:2});
W.applyEvent(ck,{type:"check",item:"kubectl",status:"warn",fix:"brew install kubectl",id:3});
W.render(ck); flush();
out.checkAfterPassing=$("wChecks").innerHTML;
out.checkModelFix=ck.checks[0].fix;

/* 5. Stop, through the button's own handler on a really attached run */
(async function(){
  W.attach("job-77");
  stream.heard.hello({data:JSON.stringify({type:"hello",stack:"lab",region:"us-east-1",
                                           dry_run:"false",id:1})});
  flush();
  const stop=$("wStop").on.click, btn=$("wStop");
  out.attached={hidden:btn.hidden,disabled:btn.disabled,run:$("wRunId").textContent};

  stop.call(btn);                          // asked, and the operator says no
  await settle(); flush();
  out.stopDeclined={asked:confirmed.length,question:confirmed[0],
                    sent:sent.length,disabled:btn.disabled};

  confirmSays=true;
  stop.call(btn);
  await settle(); flush();
  out.stopSent={sent:sent.length,url:sent[0]&&sent[0].url,method:sent[0]&&sent[0].method,
                disabled:btn.disabled,said:$("wConn").textContent};

  // a frame arriving while the run winds down must not wipe what was said
  stream.heard.log({data:JSON.stringify({type:"log",text:"tearing down",id:2})});
  flush();
  out.afterAFrame={said:$("wConn").textContent};

  // a stop the server refused says so, and gives the button back
  reply={ok:false,status:409};
  btn.disabled=false;
  stop.call(btn);
  await settle(); flush();
  out.stopRefused={sent:sent.length,disabled:btn.disabled,said:$("wConn").textContent};

  /* 6. an answer POST still in flight when the operator attaches to another
        run. Both runs number their prompts from p1, so run C's answer must
        not land on run D's question of the same name, and must not empty the
        box run D's operator is already typing a secret into. */
  W.attach("runC");
  frame({id:1,type:"prompt",prompt_id:"p1",question:"Region [us-east-1]: ",kind:"text",
         "default":"us-east-1"});
  flush();
  $("wPromptInput").value="eu-west-1";
  holdNext=true;                             // the POST goes out and stays out
  const before=sent.length;
  $("wPromptForm").on.submit({preventDefault:function(){}});
  await settle(); flush();
  out.inFlight={sent:sent.length-before,url:sent[sent.length-1].url,
                note:$("wPromptNote").textContent};

  W.attach("runD");
  frame({id:1,type:"prompt",prompt_id:"p1",question:"KVO admin password: ",kind:"secret"});
  flush();
  $("wPromptInput").value="Zq7-CANARY-typed-into-run-D";
  release(answered());                       // run C's answer comes back now
  await settle(); await settle(); flush();
  out.crossRun={question:$("wPromptQ").textContent,value:$("wPromptInput").value,
                type:$("wPromptInput").type,hidden:$("wPrompt").hidden,
                answers:W.model().prompts.map(function(p){return p.answer;}),
                open:W.openPrompt(W.model()) ? W.openPrompt(W.model()).prompt_id : null};

  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def dom(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: running watch.js needs it")
    harness = tmp_path_factory.mktemp("dom") / "dom.js"
    harness.write_text(DOM_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), WATCH], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_second_run_refills_the_modal_instead_of_keeping_the_last_runs(dom):
    """The defect, reproduced and then fixed. Prompt ids are per-run counters,
    so run A's p4 and run B's p4 are two different questions with one name.
    The modal decided whether to refill on the bare id, and attach() reset the
    model but no DOM, so attaching to run B while run A's p4 was on screen
    left run A's question there, run A's password input, and run A's operator's
    unsent secret in the box - and Send posted that secret as the answer to
    run B's question, which was a text question, so the `answered` frame
    carried it in the clear to every attached page and every later replay.
    The key is the job and the id now, so run B's p4 is a different key."""
    assert dom["runA"] == {"question": "KVO admin password: ", "type": "password", "hidden": False}
    b = dom["runB"]
    assert b["question"] == "Region [us-east-1]: ", "run B's question, not run A's"
    assert b["type"] == "text", "run B's kind, not run A's password field"
    assert b["value"] == "", "nothing run A typed is still in the box"
    assert b["hint"] == "Enter alone takes the default: us-east-1"
    assert b["hidden"] is False


def test_a_redraw_of_the_same_question_does_not_touch_what_is_being_typed(dom):
    """The other half of the same key: while the question has not changed, a
    redraw leaves the box alone. The operator is typing into it, and frames
    arrive the whole time."""
    assert dom["sameQuestion"] == {"value": "us-west-2", "question": "Region [us-east-1]: "}


def test_closing_the_modal_empties_the_box(dom):
    """Nothing typed outlives the question it was typed for, even before
    another run attaches."""
    assert dom["closed"] == {"hidden": True, "value": "", "key": ""}


def test_the_log_drawer_appends_only_what_is_new(dom):
    """It used to rebuild all 400 lines on every frame, which threw away any
    selection the operator had made in it while output was still streaming.
    The nodes from the first draw are still the same nodes after the second."""
    assert dom["log"]["lines"] == ["line %d" % i for i in range(1, 7)]
    assert dom["log"]["kept"] == 3, "the first three nodes were not rebuilt"
    assert dom["log"]["count"] == "6 lines"


def test_the_log_drawer_follows_the_tail_unless_the_reader_scrolled_up(dom):
    """There was no scrollTop anywhere: the drawer never followed the run. It
    does now, for a reader already at the bottom, and only for them."""
    assert dom["log"]["scrollTop"] == 60, "six lines, ten pixels each, pinned to the tail"
    assert dom["scrolledUp"]["scrollTop"] == 0, "a reader scrolled up is left there"
    assert dom["scrolledUp"]["last"] == "line 7", "and still gets the line"
    assert dom["scrolledUp"]["nodes"] == 7


def test_the_log_drawer_keeps_the_tail_and_starts_over_on_another_run(dom):
    o = dom["overflow"]
    assert o["nodes"] == o["logMax"] == 400, o
    assert o["last"] == "line 520" and o["first"] == "line 121", o
    assert o["count"] == "520 lines", "the badge counts every line the run printed"
    assert dom["newRun"]["lines"] == ["a fresh run"], "a new model is a new drawer"


def test_the_frames_of_one_animation_frame_become_one_draw(dom):
    """A run that prints two thousand lines used to redraw all nine panels two
    thousand times, when the browser can show one draw a frame. The renders
    collapse into one, and it draws the newest model."""
    assert dom["queued"] == 1, "three renders, one frame asked for"
    assert dom["drawn"] == "job jy", "and it drew the last model, not the first"
    assert dom["queuedAgain"] == 1, "the next render asks for the next frame"


def test_stop_asks_before_it_ends_a_run(dom):
    """/stop TERMs the run's whole process group and KILLs what is left after
    three seconds: a misclick ended a deploy mid-phase and left half a stack
    in the account, with no dialog and nothing said. It asks now, naming the
    stack, and a declined dialog sends nothing at all. api.teardown makes the
    operator type the stack name back for the same reason."""
    assert dom["attached"]["run"] == "job job-77"
    d = dom["stopDeclined"]
    assert d["asked"] == 1 and d["sent"] == 0, "declined: no request left the page"
    assert "lab" in d["question"], "the dialog names the stack: %r" % d["question"]
    assert d["disabled"] is False, "and the button is still there to press"


def test_stop_says_what_came_of_it(dom):
    """A stop the server refused used to look exactly like one that worked:
    no .then, no .catch, nothing on the screen either way."""
    s = dom["stopSent"]
    assert s["sent"] == 1 and s["url"] == "/stop/job-77" and s["method"] == "POST"
    assert s["disabled"] is True, "the button is spent while the run winds down"
    assert "Stop sent to stack lab" in s["said"], s["said"]
    assert dom["afterAFrame"]["said"] == s["said"], \
        "the frames the winding-down run keeps sending do not wipe the sentence"
    r = dom["stopRefused"]
    assert "409" in r["said"] and "still going" in r["said"], r["said"]
    assert r["disabled"] is False, "a refused stop gives the button back"


def test_a_passing_check_stops_telling_the_reader_how_to_fix_it(dom):
    """A check that fails and later passes keeps the fix in the model, because
    copy() will not overwrite a known value with an empty one and a doctor
    re-run may fail again. The ROW is where it stops being printed: a PASS
    line still saying `aws configure` reads as an instruction to go and repair
    credentials that already work. warn and fail keep theirs."""
    assert "aws configure" in dom["checkWhileFailing"], dom["checkWhileFailing"]
    after = dom["checkAfterPassing"]
    assert "PASS" in after and "aws configure" not in after, after
    assert "brew install kubectl" in after, "a warn still says what to do: %s" % after
    assert dom["checkModelFix"] == "aws configure", \
        "the model still holds it; only the passing row declines to print it"


def test_an_answer_in_flight_lands_on_the_run_it_was_sent_for(dom):
    """The last place a question could still cross runs. send() captured the
    prompt and the input box but not the job, so attaching to another run
    while the POST was in flight ran the continuation against the NEW model:
    it emptied the box run D's operator was typing into, and noteAnswer looked
    p1 up in run D's prompts - prompt ids are per-run counters, so run D's own
    open question was marked answered with run C's text, the modal closed on a
    question the engine is still blocked on, and openPrompt could never reopen
    it because the answer was no longer null."""
    assert dom["inFlight"]["sent"] == 1 and dom["inFlight"]["url"] == "/api/answer/runC"
    assert dom["inFlight"]["note"] == "sending...", dom["inFlight"]
    c = dom["crossRun"]
    assert c["question"] == "KVO admin password: ", "run D's question is still the one on screen"
    assert c["type"] == "password" and c["hidden"] is False
    assert c["value"] == "Zq7-CANARY-typed-into-run-D", "what is being typed is untouched"
    assert c["answers"] == [None], "run D's question is still unanswered"
    assert c["open"] == "p1", "and the engine is still blocked on it"
