"""The API: every route a face on an existing command, and every input
checked before it reaches one. No route here talks to AWS or a KVO: the
aws CLI, the deploy script and kvo_license.py are stubbed, and the server
tests run a real loopback server on an ephemeral port.

What these hold:
  plan       goes through profile.py (the one allowlist), refuses unknown
             keys and unwritable values, validates the typed ones, and its
             resolved rows never invent a default the script does not apply
  discover   argv lists to the aws CLI with --output json, never a shell;
             ids, regions and tags validated before they reach an argv
  doctor     deploy-stack.sh --doctor --region R --events F, read back from
             the events file; never --prompt-pipe
  run        writes deploy-profile-<stack>.env from the validated plan (mode
             600, no secret in it), starts the engine with the secrets as
             environment only, and registers the job; one engine per stack,
             decided under a lock, so two requests at once yield one job
             and one 409
  teardown   the typed-name gate, the exact flags teardown-stack.sh parses,
             the licence-loss flag only when the body says licences were
             released, --orphans for the read-only audit
  licences   kvo_license.py's own functions; the password reaches the KVO
             and nothing else
  server     Last-Event-ID replay from the job buffer, ids stamped by emit in
             buffer order, every reader of one job seeing every event, the
             Host guard on every POST and on the /api/ and /events/ GETs, the
             Sec-Fetch-Site guard on those GETs, the origin's port, body caps,
             JSON errors, the 500 that names only the exception's type (the
             page routes included) and the silent drop of a client that
             hung up

Run:  cd console && python3 -m pytest tests/test_api.py -q
"""
import http.client
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse

import pytest

from cloudlens_console import api, events as E, orchestrator as O, profile as P, server

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEPLOY = os.path.join(REPO, "deploy", "deploy-stack.sh")
TEARDOWN = os.path.join(REPO, "deploy", "teardown-stack.sh")
GOOD = {"CLOUDLENS_STACK_NAME": "demo", "CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_TAPPING": "sensors"}


# ------------------------------------------------------------------ plan
def test_plan_rejects_bad_stack_name_and_renders_profile(monkeypatch):
    r = api.plan({"CLOUDLENS_STACK_NAME": "bad name!", "CLOUDLENS_REGION": "us-east-1"})
    assert r["errors"]
    r = api.plan({"CLOUDLENS_STACK_NAME": "demo", "CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_TAPPING": "sensors"})
    assert not r.get("errors") and 'CLOUDLENS_TAPPING="sensors"' in r["profile_text"]


STACK_SAMPLES = ("demo", "a", "Demo-2", "x-", "cloudlens-lab-01", "bad name!", "abc\n", "abc\r", "-abc", "a_b",
                 "2demo", "de mo", "", "dé", "abc ", " abc", "abc\t")


def _bash_stack_rule():
    """The regex off deploy-stack.sh's valid_stack_name(), read, not assumed."""
    src = open(DEPLOY).read()
    m = re.search(r'^valid_stack_name\(\) \{ \[\[ "\$1" =~ (\S+) \]\]; \}', src, re.M)
    assert m, "valid_stack_name() not found in deploy-stack.sh"
    return m.group(1)


def test_stack_rule_is_the_scripts_own():
    # Semantic, not textual: the script's own regex is run by /bin/bash (3.2
    # on macOS, the one the script runs under) over every sample, and the
    # Python validator has to agree on each. re.match let "abc\n" through
    # where bash's =~ ^...$ does not; fullmatch agrees with bash.
    regex = _bash_stack_rule()
    assert api.STACK.pattern == regex
    probe = '[[ "$1" =~ %s ]]' % regex
    for sample in STACK_SAMPLES:
        rc = subprocess.run(["/bin/bash", "-c", probe, "_", sample], stdin=subprocess.DEVNULL).returncode
        assert rc in (0, 1), (sample, rc)
        assert api.stack_ok(sample) is (rc == 0), "bash says %s for %r, the API says %s" % (
            "valid" if rc == 0 else "invalid", sample, api.stack_ok(sample))
    assert api.stack_ok("a" * api.STACK_MAX) and not api.stack_ok("a" * (api.STACK_MAX + 1)), "CloudFormation's cap"
    assert not api.stack_ok(None) and not api.stack_ok(5)


class _NoPrompt(object):
    """A job that refuses every answer: for tests where the prompt id must
    be refused before job.answer is reached."""
    id = "j"

    def answer(self, prompt_id, text):
        raise AssertionError("a refused prompt id reached job.answer: %r" % prompt_id)


# Each validator takes a control-character tail appended to a value that is
# otherwise valid, so the refusal can only be the control character's; the
# callable is True when the API refused it. Nothing may start or shell out:
# the test stubs every exit and would raise.
CONTROL_VALIDATORS = [
    ("stack (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_STACK_NAME="abc" + t)).get("errors"))),
    ("stack (teardown)", lambda t: "error" in api.teardown(
        {"stack": "abc" + t, "region": "us-east-1", "confirm_name": "abc" + t}, start=_never, jobs={})),
    ("confirm_name", lambda t: "error" in api.teardown(
        {"stack": "abc", "region": "us-east-1", "confirm_name": "abc" + t}, start=_never, jobs={})),
    ("region (discover)", lambda t: "error" in api.discover_vpcs("us-east-1" + t)),
    ("region (doctor)", lambda t: "error" in api.doctor("us-east-1" + t)),
    ("region (teardown)", lambda t: "error" in api.teardown(
        {"stack": "abc", "region": "us-east-1" + t, "confirm_name": "abc"}, start=_never, jobs={})),
    ("region (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_REGION="us-east-1" + t)).get("errors"))),
    ("zone (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_COLLECTOR_ZONE="us-east-1a" + t)).get("errors"))),
    ("vpc (discover)", lambda t: "error" in api.discover_subnets("us-east-1", "vpc-0a0a0a0a" + t)),
    ("vpc (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_EXISTING_VPC_ID="vpc-0a0a0a0a" + t)).get("errors"))),
    ("subnet (plan)", lambda t: bool(
        api.plan(dict(GOOD, CLOUDLENS_EXISTING_SUBNET_ID="subnet-0123abcd" + t)).get("errors"))),
    ("sg (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_COLLECTOR_MGMT_SG="sg-0123abcd" + t)).get("errors"))),
    ("eks (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_EKS_CLUSTER="prod" + t)).get("errors"))),
    ("tag key (discover)", lambda t: "error" in api.discover_workloads("us-east-1", "k" + t + "=v", "")),
    ("tag key (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_DISCOVERY_TAG_KEY="k" + t)).get("errors"))),
    ("tag value (discover)", lambda t: "error" in api.discover_workloads("us-east-1", "k=v" + t, "")),
    ("tag value (plan)", lambda t: bool(api.plan(dict(GOOD, CLOUDLENS_DISCOVERY_TAG_VALUE="v" + t)).get("errors"))),
    ("kvo host", lambda t: "error" in api.licences({"kvo": "kvo.example.net" + t, "action": "list"})),
    ("kvo user", lambda t: "error" in api.licences({"kvo": "1.2.3.4", "action": "list", "user": "admin" + t})),
    ("release row code", lambda t: "error" in api.licences(
        {"kvo": "1.2.3.4", "action": "release", "rows": [{"activationCode": "AAAA-1111" + t, "quantity": 1}]})),
    ("job id", lambda t: not server.job_id_ok("abc" + t)),
    ("prompt id", lambda t: "error" in api.answer(_NoPrompt(), {"prompt_id": "p1" + t, "text": "x"})),
]


def _never(*a, **k):
    raise AssertionError("a refused input started something: %r" % (a[:2],))


@pytest.mark.parametrize("tail", ["\n", "\r", "\x00"], ids=["newline", "cr", "nul"])
@pytest.mark.parametrize("name,rejects", CONTROL_VALIDATORS, ids=[v[0] for v in CONTROL_VALIDATORS])
def test_control_characters_are_rejected_by_every_validator(name, rejects, tail, monkeypatch):
    # Reproduced before the fix: "abc\n" as a stack name reached the argv as
    # --stack-name "abc\n", "us-east-1\n" passed _check_region, and so on
    # for the vpc, subnet, EKS name and the KVO host: Python's $ accepts a
    # trailing newline. Every typed check now runs the control-character
    # check first and fullmatch after, so the three tails are refused by
    # every validator, and nothing is started or shelled out on the way.
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)
    monkeypatch.setattr(api, "_aws", _never)
    monkeypatch.setattr(api, "_kvo_license", _never)
    assert rejects(tail), "%s accepted %r" % (name, "abc" + tail)
    assert rejects(tail * 2)


@pytest.mark.parametrize("name,rejects", CONTROL_VALIDATORS, ids=[v[0] for v in CONTROL_VALIDATORS])
def test_the_control_matrix_accepts_its_own_base_value(name, rejects, monkeypatch):
    # The positive control for the matrix above: with no tail the same
    # value is accepted, so a refusal up there is the tail's alone and not
    # a validator that refuses everything. Accepted is the validator
    # saying so, or the call going past it into a stub, which raises.
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)
    monkeypatch.setattr(api, "_aws", _never)
    monkeypatch.setattr(api, "_kvo_license", _never)
    try:
        refused = rejects("")
    except AssertionError as exc:
        assert str(exc).startswith("a refused"), exc   # a stub's own words: the value went past the validator
        refused = False
    assert not refused, "%s refuses its own base value" % name


def test_plan_refuses_unknown_keys_and_unwritable_values():
    r = api.plan(dict(GOOD, CLOUDLENS_ADMIN_USER="-oProxyCommand=x", PATH="/x"))
    assert any("CLOUDLENS_ADMIN_USER" in e for e in r["errors"]), r
    assert any("PATH" in e for e in r["errors"]), r
    assert "profile_text" not in r, "a refused plan renders nothing"
    # what profile.render cannot write faithfully is an error here, not a raise
    r = api.plan(dict(GOOD, CLOUDLENS_KVO_NAME='say "hi"'))
    assert any("CLOUDLENS_KVO_NAME" in e for e in r["errors"]), r
    r = api.plan(dict(GOOD, CLOUDLENS_KVO_NAME="a\nb"))
    assert r["errors"]
    # values are strings: a bool would render as "True", which the script does not read
    r = api.plan(dict(GOOD, CLOUDLENS_DEPLOY_KVO=True))
    assert any("CLOUDLENS_DEPLOY_KVO" in e for e in r["errors"]), r
    # the two the file name and the region come from are required
    assert api.plan({"CLOUDLENS_REGION": "us-east-1"})["errors"]
    assert api.plan({"CLOUDLENS_STACK_NAME": "demo"})["errors"]
    assert api.plan("not a plan")["errors"]


def test_plan_validates_typed_values():
    bad = {
        "CLOUDLENS_REGION": "us-east-1x",
        "CLOUDLENS_EXISTING_VPC_ID": "vpc-xyz",
        "CLOUDLENS_EXISTING_SUBNET_ID": "sub-0123abcd",
        "CLOUDLENS_COLLECTOR_MGMT_SG": "sg-0123ABCD",
        "CLOUDLENS_EKS_CLUSTER": "-bad",
        "CLOUDLENS_DISCOVERY_TAG_KEY": "k" * 129,
        "CLOUDLENS_DISCOVERY_TAG_VALUE": "a\tb",
        "CLOUDLENS_COLLECTOR_ZONE": "us-east-1",
    }
    for k, v in bad.items():
        r = api.plan(dict(GOOD, **{k: v}))
        assert any(k in e for e in r.get("errors", [])), (k, v, r)
    good = {
        "CLOUDLENS_EXISTING_VPC_ID": "vpc-0123abcd", "CLOUDLENS_EXISTING_SUBNET_ID": "subnet-0123abcd0123abcd0",
        "CLOUDLENS_COLLECTOR_MGMT_SG": "sg-0123abcd", "CLOUDLENS_EKS_CLUSTER": "prod_1-a",
        "CLOUDLENS_DISCOVERY_TAG_KEY": "cloudlens", "CLOUDLENS_DISCOVERY_TAG_VALUE": "yes, really",
        "CLOUDLENS_COLLECTOR_ZONE": "us-east-1a", "CLOUDLENS_KVO_NAME": "",
    }
    r = api.plan(dict(GOOD, **good))
    assert not r.get("errors"), r
    # an empty value is written, as the interview does for a field it did not use
    assert 'CLOUDLENS_KVO_NAME=""' in r["profile_text"]


def test_plan_resolved_rows_come_from_the_plan_only():
    r = api.plan(GOOD)
    rows = {row["key"]: row for row in r["resolved"]}
    assert set(rows) == set(P.allowed_keys()), "one row per profile key, in the file's order"
    assert [row["key"] for row in r["resolved"]] == P.allowed_keys()
    for k, row in rows.items():
        if k in GOOD:
            assert row["value"] == GOOD[k] and "note" not in row
        else:
            assert row["value"] is None and "asks" in row["note"], row
    assert r["stack"] == "demo" and r["region"] == "us-east-1"
    assert r["profile_file"] == "deploy-profile-demo.env"


# -------------------------------------------------------------- discover
def test_this_suite_never_inherits_the_browser_suites_stub_aws():
    """The fast suite's environment is its own, whichever way it was run.

    tests/browser puts a stub executable named `aws` first on PATH and
    points api.VC_CREDS_FILE at a file of its choosing. Both used to be set
    by a session-scoped fixture and put back only at session teardown, and
    tests/browser sorts before the modules beside it, so `pytest tests
    tests/browser` ran all of THIS suite with that stub in front of the
    real CLI. Nothing here shells out to aws today, which is why it never
    showed; a suite whose environment depends on how it was invoked is a
    suite that can start showing it at any time.

    Run either way, this holds. The stub is recognised by its own contents,
    since a machine with no aws installed has nothing else to compare."""
    assert os.environ.get("CLOUDLENS_TEST_AWS") is None, (
        "the browser suite's answers file is still pointed at from this suite's environment")
    found = shutil.which("aws")
    if found:
        with open(found, "rb") as fh:
            head = fh.read(4096)
        assert b"CLOUDLENS_TEST_AWS" not in head, (
            "the aws on PATH is tests/browser's stub, not the CLI: " + found)
    default = (os.environ.get("CLOUDLENS_VC_CREDS_FILE")
               or os.path.join(os.path.expanduser("~"), ".cloudlens-vcontroller-creds.json"))
    assert api.VC_CREDS_FILE == default, (
        "api.VC_CREDS_FILE is still where a fixture put it: " + api.VC_CREDS_FILE)


def test_aws_is_an_argv_list_with_json_output_never_a_shell(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout='{"Vpcs": []}', stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    assert api._aws(["ec2", "describe-vpcs"], "us-east-1") == {"Vpcs": []}
    argv, kw = calls[0]
    assert isinstance(argv, list) and argv[0] == "aws"
    assert argv[-4:] == ["--region", "us-east-1", "--output", "json"]
    assert not kw.get("shell") and kw["stdin"] is subprocess.DEVNULL
    assert kw["timeout"] == api.AWS_TIMEOUT

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 254, stdout="", stderr="\nAn error occurred (AuthFailure)\n")

    monkeypatch.setattr(api.subprocess, "run", failing)
    with pytest.raises(api.AwsError, match="AuthFailure"):
        api._aws(["ec2", "describe-vpcs"], "us-east-1")

    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file", "aws")

    monkeypatch.setattr(api.subprocess, "run", missing)
    with pytest.raises(api.AwsError, match="not installed"):
        api._aws(["ec2", "describe-vpcs"], "us-east-1")
    # and the route answers an AwsError as a 502, not a traceback
    r = api.discover_vpcs("us-east-1")
    assert "not installed" in r["error"] and r["http"] == 502

    def denied(argv, **kw):
        raise PermissionError(13, "Permission denied", "aws")

    # any other OSError from the exec (a CLI on PATH that cannot run) is
    # an answer too: it used to escape as a traceback and a dropped connection
    monkeypatch.setattr(api.subprocess, "run", denied)
    with pytest.raises(api.AwsError, match="could not be run: Permission denied"):
        api._aws(["ec2", "describe-vpcs"], "us-east-1")
    assert api.discover_vpcs("us-east-1")["http"] == 502


def test_discover_vpcs_and_subnets(monkeypatch):
    calls = []
    canned = {
        ("ec2", "describe-vpcs"): {"Vpcs": [
            {"VpcId": "vpc-0a0a0a0a", "CidrBlock": "10.0.0.0/16", "Tags": [{"Key": "Name", "Value": "prod"}]},
            {"VpcId": "vpc-0b0b0b0b", "CidrBlock": "172.31.0.0/16"}]},
        ("ec2", "describe-subnets"): {"Subnets": [
            {"SubnetId": "subnet-01010101", "AvailabilityZone": "us-east-1a", "CidrBlock": "10.0.1.0/24",
             "MapPublicIpOnLaunch": True, "Tags": [{"Key": "Name", "Value": "pub-a"}]},
            # auto-assigns public IPs but has no internet route: "public" to
            # the CLI's pick_subnet, not reachable from the internet
            {"SubnetId": "subnet-02020202", "AvailabilityZone": "us-east-1b", "CidrBlock": "10.0.2.0/24",
             "MapPublicIpOnLaunch": True},
            {"SubnetId": "subnet-03030303", "AvailabilityZone": "us-east-1c", "CidrBlock": "10.0.3.0/24",
             "MapPublicIpOnLaunch": False},
            # an igw route but no auto-assign: the field missing altogether is False, as the CLI prints "private"
            {"SubnetId": "subnet-04040404", "AvailabilityZone": "us-east-1d", "CidrBlock": "10.0.4.0/24"}]},
        ("ec2", "describe-route-tables"): {"RouteTables": [
            # the main table: no internet route
            {"Associations": [{"Main": True}], "Routes": [{"GatewayId": "local"}]},
            # subnet-01010101 has its own table with an igw route; subnet-03030303 one with a NAT only
            {"Associations": [{"SubnetId": "subnet-01010101"}],
             "Routes": [{"GatewayId": "local"}, {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}]},
            {"Associations": [{"SubnetId": "subnet-03030303"}],
             "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1"}]},
            {"Associations": [{"SubnetId": "subnet-04040404"}],
             "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}]}]},
    }

    def fake_aws(args, region, **kw):
        calls.append((list(args), region))
        return canned[tuple(args[:2])]

    monkeypatch.setattr(api, "_aws", fake_aws)
    assert api.discover_vpcs("us-east-1") == [
        {"id": "vpc-0a0a0a0a", "cidr": "10.0.0.0/16", "name": "prod"},
        {"id": "vpc-0b0b0b0b", "cidr": "172.31.0.0/16", "name": ""}]
    subnets = api.discover_subnets("us-east-1", "vpc-0a0a0a0a")
    # `public` is MapPublicIpOnLaunch, the column deploy-stack.sh's
    # pick_subnet() prints as public/private, so the wizard and the
    # interview agree; `igw_route` is the route-table truth. The two
    # disagree on 02 (auto-assign, no route) and 04 (route, no auto-assign).
    assert [(s["id"], s["az"], s["cidr"], s["public"], s["igw_route"]) for s in subnets] == [
        ("subnet-01010101", "us-east-1a", "10.0.1.0/24", True, True),     # its own table routes to an igw
        ("subnet-02020202", "us-east-1b", "10.0.2.0/24", True, False),    # falls to the main table: no igw
        ("subnet-03030303", "us-east-1c", "10.0.3.0/24", False, False),   # NAT is not an internet gateway
        ("subnet-04040404", "us-east-1d", "10.0.4.0/24", False, True)]    # igw route, no auto-assign
    assert subnets[0]["name"] == "pub-a"
    # the vpc reached the filter as a JSON structure, not a comma-split shorthand
    flt = [c for c in calls if c[0][:2] == ["ec2", "describe-subnets"]][0][0]
    assert json.loads(flt[flt.index("--filters") + 1]) == [{"Name": "vpc-id", "Values": ["vpc-0a0a0a0a"]}]
    # validation before any call
    n = len(calls)
    assert "region" in api.discover_vpcs("US-EAST-1")["error"]
    assert "vpc" in api.discover_subnets("us-east-1", "vpc-zz")["error"]
    assert "vpc" in api.discover_subnets("us-east-1", "")["error"]
    assert len(calls) == n, "a refused input never reaches the aws CLI"


def test_discover_workloads_and_eks(monkeypatch):
    calls = []

    def inst(i, vpc="vpc-0a0a0a0a"):
        return {"InstanceId": "i-%08x" % i, "VpcId": vpc, "SubnetId": "subnet-01010101", "State": {"Name": "running"},
                "Placement": {"AvailabilityZone": "us-east-1a"}, "PrivateIpAddress": "10.0.1.%d" % (i % 250),
                "InstanceType": "t3.small", "PlatformDetails": "Linux/UNIX",
                "Tags": [{"Key": "Name", "Value": "web-%d" % i}]}

    def fake_aws(args, region, **kw):
        calls.append(list(args))
        if args[:2] == ["ec2", "describe-instances"]:
            return {"Reservations": [{"Instances": [inst(i) for i in range(30)]},
                                     {"Instances": [inst(i) for i in range(30, 60)]}]}
        if args[:2] == ["eks", "list-clusters"]:
            return {"clusters": ["prod", "lab"]}
        raise AssertionError(args)

    monkeypatch.setattr(api, "_aws", fake_aws)
    r = api.discover_workloads("us-east-1", "cloudlens=yes, really", "vpc-0a0a0a0a,vpc-0b0b0b0b")
    assert r["count"] == 60 and len(r["rows"]) == api.MAX_ROWS and r["truncated"]
    assert r["rows"][0] == {"id": "i-00000000", "name": "web-0", "vpc": "vpc-0a0a0a0a", "subnet": "subnet-01010101",
                            "az": "us-east-1a", "state": "running", "type": "t3.small",
                            "platform": "Linux/UNIX", "private_ip": "10.0.1.0", "public_ip": ""}
    argv = calls[0]
    filters = json.loads(argv[argv.index("--filters") + 1])
    # the tag value keeps its comma (one value), the state filter is the one
    # workload_selection.py applies, and the vpc list is the vpc-id filter
    assert {"Name": "tag:cloudlens", "Values": ["yes, really"]} in filters
    assert {"Name": "instance-state-name", "Values": ["running"]} in filters
    assert {"Name": "vpc-id", "Values": ["vpc-0a0a0a0a", "vpc-0b0b0b0b"]} in filters
    # no vpcs: no vpc filter at all
    api.discover_workloads("us-east-1", "cloudlens=yes", "")
    assert not any(f["Name"] == "vpc-id" for f in json.loads(calls[1][calls[1].index("--filters") + 1]))
    assert api.discover_eks("us-east-1") == [{"name": "prod"}, {"name": "lab"}]
    n = len(calls)
    assert "tag" in api.discover_workloads("us-east-1", "novalue", "")["error"]
    assert "tag" in api.discover_workloads("us-east-1", "=v", "")["error"]
    assert "tag" in api.discover_workloads("us-east-1", "k=a\x00b", "")["error"]
    assert "tag" in api.discover_workloads("us-east-1", ("k" * 129) + "=v", "")["error"]
    assert "vpc" in api.discover_workloads("us-east-1", "k=v", "vpc-0a0a0a0a,nope")["error"]
    assert "region" in api.discover_eks("eu")["error"]
    assert len(calls) == n


# ---------------------------------------------------------------- doctor
class FakeProc(object):
    """subprocess.Popen as doctor() drives it: records the argv and the
    keyword arguments, writes the events file on communicate() and answers
    with the stdout, stderr and exit the test chose. `hang` raises
    TimeoutExpired from the first communicate, as a real one does, and
    answers the second (the reap after the kill)."""
    def __init__(self, seen, lines, rc, out="", err="", hang=False):
        self.seen, self.lines, self.rc, self.out, self.err, self.hang = seen, lines, rc, out, err, hang
        self.pid = 4242
        self.returncode = None

    def __call__(self, argv, **kw):
        self.seen["argv"], self.seen["kw"], self.seen["calls"], self.seen["timeouts"] = argv, kw, 0, []
        return self

    def communicate(self, timeout=None):
        self.seen["calls"] += 1
        self.seen["timeouts"].append(timeout)
        if self.hang and self.seen["calls"] == 1:
            raise subprocess.TimeoutExpired(self.seen["argv"], timeout)
        argv = self.seen["argv"]
        with open(argv[argv.index("--events") + 1], "a") as fh:
            for line in self.lines:
                fh.write(line + "\n")
        self.returncode = self.rc
        return self.out, self.err


class FakeSsh(object):
    """subprocess.Popen as _vpb_cell drives it, which is doctor()'s shape:
    the argv and the keyword arguments recorded, one communicate() that
    answers with the stdout, stderr and exit the test chose. `hang` raises
    TimeoutExpired from it, as a real one does when the box says nothing."""
    def __init__(self, ran, rc=0, out="", err="", hang=False):
        self.ran, self.rc, self.out, self.err, self.hang = ran, rc, out, err, hang
        self.pid = 4343
        self.returncode = None

    def __call__(self, argv, **kw):
        self.ran.append((argv, kw))
        return self

    def communicate(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired(self.ran[-1][0], timeout)
        self.returncode = self.rc
        return self.out, self.err


def test_doctor_runs_the_script_and_reads_its_check_events(monkeypatch):
    seen = {}
    monkeypatch.setattr(api.subprocess, "Popen", FakeProc(seen, [
        '{"seq":1,"ts":"t","type":"check","item":"AWS CLI 2.15","status":"pass"}',
        '{"seq":2,"ts":"t","type":"check","item":"No creds","status":"fail","fix":"aws sso login"}',
        '{"seq":3,"ts":"t","type":"check","item":"Bucket","status":"warn","fix":"allow https"}',
        '{"seq":4,"ts":"t","type":"done","status":"failed","mode":"doctor"}'], rc=1, out="[FAIL] No creds\n"))
    r = api.doctor("eu-west-2")
    argv = seen["argv"]
    assert argv[:2] == ["bash", api.DEPLOY] and "--doctor" in argv
    assert argv[argv.index("--region") + 1] == "eu-west-2"
    assert "--prompt-pipe" not in argv, "the doctor asks nothing; a pipe would block it"
    assert seen["kw"]["stdin"] is subprocess.DEVNULL
    assert seen["kw"]["start_new_session"], "no controlling terminal: the script would re-attach /dev/tty"
    assert seen["timeouts"] == [api.DOCTOR_TIMEOUT], "the wait is bounded by DOCTOR_TIMEOUT, nothing else"
    assert r["checks"] == [
        {"item": "AWS CLI 2.15", "status": "pass", "fix": ""},
        {"item": "No creds", "status": "fail", "fix": "aws sso login"},
        {"item": "Bucket", "status": "warn", "fix": "allow https"}]
    assert r["ok"] is False and r["exit"] == 1
    assert not os.path.exists(argv[argv.index("--events") + 1]), "the events file is cleaned up"

    monkeypatch.setattr(api.subprocess, "Popen", FakeProc(
        seen, ['{"seq":1,"ts":"t","type":"check","item":"x","status":"pass"}'], rc=0))
    assert api.doctor("us-east-1")["ok"] is True

    # a doctor that hangs: its GROUP is killed (the pgid is the leader's pid,
    # never the leader alone), the leader reaped, and the answer is a 504
    killed = []
    monkeypatch.setattr(api.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(api.subprocess, "Popen", FakeProc(seen, [], rc=-9, hang=True))
    r = api.doctor("us-east-1")
    assert r["http"] == 504 and "timed out" in r["error"]
    assert killed == [(4242, signal.SIGKILL)] and seen["calls"] == 2, "killpg on the pgid, then the reap"
    assert seen["timeouts"] == [api.DOCTOR_TIMEOUT, None], "the reap after the kill has no timeout to hit"

    monkeypatch.setattr(api.subprocess, "Popen", FakeProc(seen, [], rc=2, err="bash: syntax error"))
    r = api.doctor("us-east-1")
    assert r["http"] == 502 and "syntax error" in r["error"]
    assert "region" in api.doctor("nope")["error"]


# the doctor's shape when a probe hangs: a child (curl, the aws CLI)
# holding the script's stdout, the script waiting on it
SLOW_DOCTOR = r'''#!/usr/bin/env bash
echo $$ > "$PID_FILE"
sleep 30 &
wait
'''


def _group_gone(pgid):
    """True once no process is left in the group (ESRCH); EPERM on macOS is
    the members still being reaped, so not yet."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def test_a_doctor_that_hangs_is_killed_with_its_children(tmp_path, monkeypatch):
    # Reproduced before the fix: subprocess.run's timeout killed the leader
    # alone, its child kept the stdout pipe open, and the read after the
    # kill sat on that pipe for the child's whole life (30 s here; a probe's
    # own timeout in the real script). Now the group gets KILL and the
    # answer comes at the timeout, with nothing of the doctor left behind.
    script = tmp_path / "slow-doctor.sh"
    script.write_text(SLOW_DOCTOR)
    script.chmod(0o755)
    monkeypatch.setattr(api, "DEPLOY", str(script))
    monkeypatch.setattr(api, "DOCTOR_TIMEOUT", 0.5)
    monkeypatch.setenv("PID_FILE", str(tmp_path / "pid"))
    t0 = time.monotonic()
    r = api.doctor("us-east-1")
    took = time.monotonic() - t0
    assert r["http"] == 504 and "timed out after 0.5s" in r["error"], r
    assert took < 5, "the doctor answered only when its child had died: %.1fs" % took
    # the script writes its pid on its first line; a slow bash can still be
    # between the redirect's open and the write when the doctor answers,
    # and int("") on that empty file was a flake, not a finding
    pid_file = tmp_path / "pid"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip()):
        time.sleep(0.05)
    assert pid_file.exists() and pid_file.read_text().strip(), "the doctor never wrote its pid"
    pid = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _group_gone(pid):
        time.sleep(0.05)
    assert _group_gone(pid), "the child outlived the kill"


# ------------------------------------------------------------------- run
def test_run_writes_the_profile_and_starts_the_engine_with_secrets_in_env_only(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    started = {}

    def start(job, cmd, cwd, env):
        started.update(job=job, cmd=cmd, cwd=cwd, env=env)

    jobs = {}
    body = {"plan": dict(GOOD, CLOUDLENS_DEPLOY_KVO="true"),
            "secrets": {"CLOUDLENS_MIRROR_ACCESS_KEY": "AKIAEXAMPLE", "CLOUDLENS_MIRROR_SECRET_KEY": "s3cr3t/value",
                        "CLOUDLENS_VC_PASSWORD": ""},
            "kvo_codes": ["1234-ABCD-5678-EF00", "AAAA-BBBB-CCCC-DDDD,5"]}
    r = api.run(body, jobs=jobs, start=start)
    assert not r.get("errors") and not r.get("error"), r
    job = started["job"]
    assert r["job_id"] == job.id and jobs[job.id] is job
    assert job.flow_id == "engine-deploy"
    # the profile: the validated plan, mode 600, in the engine's cwd
    path = os.path.join(str(tmp_path), "deploy-profile-demo.env")
    assert started["cmd"][:4] == ["bash", api.DEPLOY, "--profile", path]
    assert started["cwd"] == str(tmp_path)
    assert r["profile_file"] == "deploy-profile-demo.env" and job.inputs["profile"] == path, \
        "the answer names the file as plan() does, relative to the repo root; the engine gets the path"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    text = open(path).read()
    assert 'CLOUDLENS_DEPLOY_KVO="true"' in text and 'CLOUDLENS_STACK_NAME="demo"' in text
    # activation codes ride argv (the script's only way to take them); the
    # env secrets ride env; an empty secret is not passed at all
    assert started["cmd"][4:] == ["--kvo-codes", "1234-ABCD-5678-EF00", "--kvo-codes", "AAAA-BBBB-CCCC-DDDD,5"]
    assert started["env"] == {"CLOUDLENS_MIRROR_ACCESS_KEY": "AKIAEXAMPLE", "CLOUDLENS_MIRROR_SECRET_KEY": "s3cr3t/value"}
    # and no secret is anywhere but there
    everywhere = text + json.dumps(job.inputs) + json.dumps(job.buffer) + json.dumps(r)
    for leak in ("s3cr3t", "AKIAEXAMPLE", "1234-ABCD", "AAAA-BBBB"):
        assert leak not in everywhere, leak
    assert job.buffer and job.buffer[0]["type"] == E.NARRATE, "the stream opens with the command it runs"


# The real script's argv parser (the two flags run_engine appends), then the
# lines scripts/kvo_license.py and the dry run print: `code[:14]...` per
# code, the whole code in the dry-run command line, and a password a phase
# echoed. The done's reason carries a code too, as the script's free text may.
FAKE_LICENSE_ENGINE = r'''#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do case $1 in --events) ev=$2; shift 2;; --prompt-pipe) pipe=$2; shift 2;; *) shift;; esac; done
echo '[license] 1234-ABCD-5678... = KVO-DEVICE  avail=5 total=5 -> activating 5'
echo '[license] 1234-ABCD-5678-EFGH-9012... = KVO-DEVICE'
echo 'dry run: python3 scripts/kvo_license.py --codes 1234-ABCD-5678-EFGH-9012,5 AAAA-BBBB-CCCC-DDDD'
echo 'pw is hunter2secret'
echo 'plain line stays plain'
echo '{"seq":1,"ts":"t","type":"done","status":"ok","reason":"activated 1234-ABCD-5678-EFGH-9012"}' >> "$ev"
exit 0
'''


def test_run_registers_codes_and_secrets_and_the_engine_redacts_them(tmp_path, monkeypatch):
    # Reproduced before the fix: kvo_license.py prints code[:14]... per
    # code and the engine turns every stdout line into a log event, so 14
    # of the 19 characters of each code landed in job.buffer and the
    # browser. run() now registers each code and each secret with the job
    # and the engine's stdout loop and verdict go through job.redact.
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    script = tmp_path / "fake-deploy.sh"
    script.write_text(FAKE_LICENSE_ENGINE)
    script.chmod(0o755)
    monkeypatch.setattr(api, "DEPLOY", str(script))
    jobs = {}
    r = api.run({"plan": GOOD, "secrets": {"CLOUDLENS_VC_PASSWORD": "hunter2secret"},
                 "kvo_codes": ["1234-ABCD-5678-EFGH-9012,5", "AAAA-BBBB-CCCC-DDDD"]}, jobs=jobs)
    assert not r.get("errors"), r
    job = jobs[r["job_id"]]
    assert sorted(job.redactions) == ["1234-ABCD-5678-EFGH-9012", "AAAA-BBBB-CCCC-DDDD", "hunter2secret"]
    deadline = time.monotonic() + 10
    while not job.done and time.monotonic() < deadline:
        time.sleep(0.05)
    assert job.done, [e["type"] for e in job.buffer]
    blob = json.dumps(job.buffer)
    for leak in ("1234-ABCD-5678", "EFGH-9012", "AAAA-BBBB-CCCC", "hunter2secret"):
        assert leak not in blob, leak
    assert O.REDACTED in blob
    texts = [e["text"] for e in job.buffer if e["type"] == E.LOG]
    assert "[license] [redacted]... = KVO-DEVICE  avail=5 total=5 -> activating 5" in texts, texts
    assert "[license] [redacted]... = KVO-DEVICE" in texts
    assert "dry run: python3 scripts/kvo_license.py --codes [redacted],5 [redacted]" in texts, "the quantity stays"
    assert "pw is [redacted]" in texts and "plain line stays plain" in texts
    last = job.buffer[-1]
    assert last["type"] == E.DONE and last["status"] == "ok"
    assert last["reason"] == "activated [redacted]", "the script's own done is redacted too"
    # the narrate that opens the stream never carried them either
    assert job.buffer[0]["type"] == E.NARRATE and "1234" not in job.buffer[0]["text"]


def test_run_refuses_a_secret_or_a_code_the_stream_could_not_blank(tmp_path, monkeypatch):
    """Everything registered with a job is replaced wherever it appears in
    that run's output, so a very short one rewrites words that are not the
    secret: orchestrator.MIN_REDACTION carries the run this cost. The
    refusal belongs here, next to the other validation and before the
    engine lock, so a typo at the launch is a 400 that names the field
    rather than a raise inside the launch or, worse, a run whose frames
    are being rewritten.

    Neither value is echoed back: a refusal that quotes a near-miss is
    most of a secret."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    jobs = {}
    r = api.run({"plan": GOOD, "secrets": {"CLOUDLENS_VC_PASSWORD": "pw"}}, jobs=jobs, start=_never)
    assert r["errors"] and "CLOUDLENS_VC_PASSWORD" in r["errors"][0], r
    assert "pw" not in r["errors"][0].replace("password", ""), "the value is never echoed"
    assert jobs == {} and not os.listdir(str(tmp_path)), "nothing was started and no profile was written"

    r = api.run({"plan": GOOD, "kvo_codes": ["A123", "AAAA-BBBB-CCCC-DDDD"]}, jobs={}, start=_never)
    assert r["errors"] == ["kvo_codes: entry 1 is an activation code of under 6 characters. The console "
                           "blanks every code from the run's own output, and one that short cannot be "
                           "blanked without corrupting the console's own frames."], r
    # the quantity is not part of the code and is not measured with it
    assert api._unredactable({}, ["A12345,999999"]) == [], "six characters of code is enough"
    assert api._unredactable({"CLOUDLENS_KVO_ADMIN_PASS": "admin"}, [])


def test_run_refuses_secrets_it_does_not_know_and_bad_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    calls = []
    start = lambda *a, **k: calls.append(a)
    # a profile key is never a secret (it would bypass the file), and an
    # unknown name is never passed through to the engine's environment
    # a control character in a secret (a newline, a tab, not only NUL) ends
    # the argument or the form field the script puts the value in
    for bad in ({"CLOUDLENS_REGION": "us-east-1"}, {"PATH": "/x"}, {"AWS_SECRET_ACCESS_KEY": "x"},
                {"CLOUDLENS_MIRROR_SECRET_KEY": 5}, {"CLOUDLENS_MIRROR_SECRET_KEY": "a\x00b"},
                {"CLOUDLENS_MIRROR_SECRET_KEY": "a\nb"}, {"CLOUDLENS_VC_PASSWORD": "pw\t"},
                {"CLOUDLENS_KVO_ADMIN_PASS": "\x7fpw"}):
        r = api.run({"plan": GOOD, "secrets": bad}, jobs={}, start=start)
        assert r.get("errors"), bad
    assert "control character" in api._check_secrets({"CLOUDLENS_VC_PASSWORD": "a\nb"})[1][0]
    assert api._check_secrets({"CLOUDLENS_VC_PASSWORD": "p&ss w0rd/+="})[0] == {"CLOUDLENS_VC_PASSWORD": "p&ss w0rd/+="}
    r = api.run({"plan": GOOD, "secrets": "nope"}, jobs={}, start=start)
    assert r["errors"]
    r = api.run({"plan": GOOD, "kvo_codes": ["has space"]}, jobs={}, start=start)
    assert any("kvo_codes" in e for e in r["errors"]), r
    r = api.run({"plan": dict(GOOD, CLOUDLENS_STACK_NAME="../etc")}, jobs={}, start=start)
    assert r["errors"]
    assert api.run("x", jobs={}, start=start)["errors"]
    assert not calls and not os.listdir(str(tmp_path)), "nothing written, nothing started"


def test_a_stack_in_flight_refuses_a_second_run_and_a_teardown(tmp_path, monkeypatch):
    # Reproduced before the fix: two POST /api/run for one stack started two
    # engines on one profile file and one CloudFormation stack name, and a
    # teardown could start while the deploy was still in its phases.
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    jobs, started = {}, []
    start = lambda job, cmd, cwd, env: started.append(cmd)
    r = api.run({"plan": GOOD}, jobs=jobs, start=start)
    job = jobs[r["job_id"]]
    profile = tmp_path / "deploy-profile-demo.env"
    with open(str(profile), "a") as fh:
        fh.write("# marker: a refused run must not rewrite this file\n")
    # held from the registration on: the engine thread has not reached its
    # Popen (this stub never does), and that window is exactly where a
    # second request used to slip through
    assert not job.running() and not job.done
    r2 = api.run({"plan": GOOD}, jobs=jobs, start=start)
    assert r2 == {"error": "stack demo already has a run in progress (job %s)" % job.id, "http": 409}
    assert "marker" in profile.read_text(), "refused before anything was written"
    r3 = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "demo"}, jobs=jobs, start=start)
    assert r3["http"] == 409 and job.id in r3["error"]
    r4 = api.teardown({"stack": "demo", "region": "us-east-1", "orphans_only": True}, jobs=jobs, start=start)
    assert r4["http"] == 409, "the audit is a teardown-stack.sh process on the stack too"
    # the same name in another region, or another stack here, is not this stack
    assert "error" not in api.run({"plan": dict(GOOD, CLOUDLENS_REGION="eu-west-2")}, jobs=jobs, start=start)
    assert "error" not in api.run({"plan": dict(GOOD, CLOUDLENS_STACK_NAME="other")}, jobs=jobs, start=start)
    assert len(started) == 3
    # a job whose group is open holds the stack whatever its flow: the
    # running() half of the rule, for a job the engine did not register
    flow = O.Job("flow", "stack", {"stack": "third", "region": "us-east-1"})
    flow._group_open = True
    jobs["flow"] = flow
    assert api.run({"plan": dict(GOOD, CLOUDLENS_STACK_NAME="third")}, jobs=jobs, start=start)["http"] == 409
    # the runner's verdict is the release: the stack is free again, and a
    # teardown in flight blocks a run the same way
    job.emit(E.done("engine exited 0"))
    r5 = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "demo"}, jobs=jobs, start=start)
    assert "error" not in r5
    r6 = api.run({"plan": GOOD}, jobs=jobs, start=start)
    assert r6["http"] == 409 and r5["job_id"] in r6["error"]
    jobs[r5["job_id"]].emit(E.error("engine exited 1"))
    assert "error" not in api.run({"plan": GOOD}, jobs=jobs, start=start), "an error is terminal too"


def _race(tmp_path, monkeypatch, calls):
    """Run `calls` (each a callable taking jobs and a starter) on threads at
    once, with a starter that blocks every launched job in the window
    between its registration and the engine's Popen, and return the
    answers once all of them are in. The blocked starter is the window
    itself: nothing sets running() until it returns."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    jobs, results, launched = {}, [], []
    release = threading.Event()

    def start(job, cmd, cwd, env):
        launched.append(job)
        release.wait(5)

    ts = [threading.Thread(target=lambda c=c: results.append(c(jobs, start))) for c in calls]
    for t in ts:
        t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not launched:
        time.sleep(0.01)
    assert launched, "no call reached its starter"
    release.set()
    for t in ts:
        t.join(5)
    assert len(results) == len(calls) and not any(t.is_alive() for t in ts)
    return jobs, results, launched


def _one_job_one_409(jobs, results, launched):
    ok = [r for r in results if "job_id" in r]
    refused = [r for r in results if r.get("http") == 409]
    assert len(ok) == 1 and len(refused) == 1, results
    assert refused[0]["error"] == "stack demo already has a run in progress (job %s)" % ok[0]["job_id"]
    assert list(jobs) == [ok[0]["job_id"]] and launched == [jobs[ok[0]["job_id"]]], "one job, started once"


def test_two_simultaneous_runs_for_one_stack_yield_one_job_and_one_409(tmp_path, monkeypatch):
    # Reproduced before the fix: _in_flight keyed on running(), which the
    # engine thread sets at its Popen, 5-20 ms after run() had registered
    # the job. A second POST /api/run in that window passed the guard, and
    # both wrote the profile and started an engine on one stack. The
    # decision and the registration are one step under a lock now, and a
    # registered engine job holds its stack until its terminal event.
    run = lambda jobs, start: api.run({"plan": GOOD}, jobs=jobs, start=start)
    _one_job_one_409(*_race(tmp_path, monkeypatch, [run, run]))


def test_a_run_and_a_teardown_launched_together_yield_one_job_and_one_409(tmp_path, monkeypatch):
    # the same window from the other side: a teardown that arrives while
    # the deploy's engine thread has not reached its Popen
    run = lambda jobs, start: api.run({"plan": GOOD}, jobs=jobs, start=start)
    tear = lambda jobs, start: api.teardown(
        {"stack": "demo", "region": "us-east-1", "confirm_name": "demo"}, jobs=jobs, start=start)
    _one_job_one_409(*_race(tmp_path, monkeypatch, [run, tear]))
    _one_job_one_409(*_race(tmp_path, monkeypatch, [tear, run]))


def test_secret_env_names_are_the_ones_the_script_reads_and_never_profile_keys():
    src = open(DEPLOY).read()
    assert set(api.SECRET_ENV).isdisjoint(P.allowed_keys())
    for name in api.SECRET_ENV:
        assert re.search(r"\$\{%s[:}-]" % name, src), "%s is not read by deploy-stack.sh" % name
    # the ones the task named: the KVO mirroring key pair and the two admin passwords
    assert {"CLOUDLENS_MIRROR_ACCESS_KEY", "CLOUDLENS_MIRROR_SECRET_KEY",
            "CLOUDLENS_VC_PASSWORD", "CLOUDLENS_KVO_ADMIN_PASS"} == set(api.SECRET_ENV)


def test_the_real_starter_runs_the_engine_on_a_daemon_thread(monkeypatch):
    got = {}

    def fake_engine(job, cmd, cwd=None, env=None, wired=True):
        got.update(job=job, cmd=cmd, cwd=cwd, env=env, wired=wired, thread=threading.current_thread())

    monkeypatch.setattr(api.O, "run_engine", fake_engine)
    job = O.Job("j", "engine-deploy", {})
    api._start_engine(job, ["bash", "x"], "/cwd", {"K": "v"})
    got["thread"].join(5)
    assert got["cmd"] == ["bash", "x"] and got["cwd"] == "/cwd" and got["env"] == {"K": "v"} and got["wired"]
    assert got["thread"].daemon and got["thread"] is not threading.current_thread()
    api._start_teardown(job, ["bash", "t"], "/cwd", None)
    got["thread"].join(5)
    assert got["cmd"] == ["bash", "t"] and got["wired"] is False, "teardown-stack.sh knows no --events"


def test_a_starter_that_fails_frees_the_stack(tmp_path, monkeypatch):
    """_launch: a starter that raises before run_engine runs (a thread the OS
    refused) has already registered its job, and the job holds its stack
    until its terminal event. The failure is that event, the exception
    still reaches the caller, and the next run for the stack is accepted."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    jobs = {}

    def refused(job, cmd, cwd, env):
        raise RuntimeError("can't start new thread (/private/detail)")

    with pytest.raises(RuntimeError):
        api.run({"plan": GOOD}, jobs=jobs, start=refused)
    assert len(jobs) == 1, "the job was registered before the starter ran"
    failed = next(iter(jobs.values()))
    last = failed.buffer[-1]
    assert last["type"] == E.ERROR and last["text"] == "could not start the engine: RuntimeError", failed.buffer
    assert failed.done and not failed.running()
    assert "private" not in json.dumps(failed.buffer), "the exception's message is for stderr, not the stream"
    assert api._in_flight(jobs, "demo", "us-east-1") is None

    started = {}
    r = api.run({"plan": GOOD}, jobs=jobs, start=lambda job, cmd, cwd, env: started.update(job=job))
    assert "error" not in r and "errors" not in r, r
    assert r["job_id"] == started["job"].id and r["job_id"] != failed.id
    assert api._in_flight(jobs, "demo", "us-east-1") == started["job"].id, "the new job holds the stack now"


# -------------------------------------------------------------- teardown
def test_teardown_requires_typed_name():
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "nope"}, start=lambda *a, **k: "j")
    assert r["error"].startswith("type the stack name")


def _teardown_flags():
    """The flags teardown-stack.sh's own case statement accepts: every
    label of its argument parser, each alternative of an a|b) label."""
    src = open(TEARDOWN).read()
    case = re.search(r'while \[\[ \$# -gt 0 \]\]; do\n  case "\$1" in\n(.*?)\n  esac', src, re.S)
    assert case, "teardown-stack.sh arg parser not found"
    return set(f for label in re.findall(r"^\s*([-a-z|]+)\)", case.group(1), re.M) for f in label.split("|"))


def test_teardown_speaks_the_flags_the_script_parses():
    flags = _teardown_flags()
    for f in ("--stack-name", "--region", "--yes", "--orphans", "--accept-licence-loss"):
        assert f in flags, f
    assert "--stack" not in flags and "--events" not in flags and "--prompt-pipe" not in flags
    started = []
    jobs = {}
    start = lambda job, cmd, cwd, env: started.append((job, cmd, cwd, env))
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "demo"}, start=start, jobs=jobs)
    job, cmd, cwd, env = started[-1]
    assert r["job_id"] == job.id and jobs[job.id] is job and job.flow_id == "engine-teardown"
    assert cmd == ["bash", api.TEARDOWN, "--stack-name", "demo", "--region", "us-east-1", "--yes"]
    assert cwd == api.REPO and env is None
    # the narrate that opens the stream shows the flags from --stack-name
    # on: it used to skip that flag, so the name read as a stray word
    assert job.buffer[0]["type"] == E.NARRATE
    assert job.buffer[0]["text"] == "engine: bash deploy/teardown-stack.sh --stack-name demo --region us-east-1 --yes"
    assert "--accept-licence-loss" not in cmd, "the licence-loss flag needs a release first"
    # each job holds the stack until its verdict (see _in_flight), so the
    # next teardown of demo waits on a done here
    job.emit(E.done("engine exited 0"))
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "demo", "licences_released": True},
                     start=start, jobs=jobs)
    assert started[-1][1][-2:] == ["--yes", "--accept-licence-loss"]
    started[-1][0].emit(E.done("engine exited 0"))
    # the read-only audit needs no typed name and carries --orphans
    r = api.teardown({"stack": "demo", "region": "us-east-1", "orphans_only": True}, start=start, jobs=jobs)
    assert not r.get("error") and r["audit"] is True
    assert "--orphans" in started[-1][1] and "--accept-licence-loss" not in started[-1][1]
    n = len(started)
    assert api.teardown({"stack": "bad name", "region": "us-east-1", "confirm_name": "bad name"}, start=start)["error"]
    assert api.teardown({"stack": "demo", "region": "nowhere", "confirm_name": "demo"}, start=start)["error"]
    assert api.teardown({"stack": "demo", "region": "us-east-1"}, start=start)["error"].startswith("type the stack name")
    assert api.teardown([], start=start)["error"]
    assert len(started) == n


# ---------------------------------------------------------------- answer
def test_answer_route_maps_onto_job_answer():
    class J(object):
        id = "j"
        calls = []

        def answer(self, prompt_id, text):
            self.calls.append((prompt_id, text))
            if prompt_id != "p1":
                raise ValueError("The prompt waiting is p1, not %s." % prompt_id)

    j = J()
    assert api.answer(j, {"prompt_id": "p1", "text": "ABCD"}) == {"ok": True}
    r = api.answer(j, {"prompt_id": "p9", "text": "x"})
    assert r["error"].startswith("The prompt waiting is p1") and r["http"] == 409
    assert api.answer(j, {"prompt_id": "p1"})["error"]
    assert api.answer(j, {"prompt_id": 3, "text": "x"})["error"]
    assert api.answer(j, {"prompt_id": "p1", "text": "a\nb"})["error"]
    assert api.answer(j, {"prompt_id": "p1", "text": "x" * (api.MAX_ANSWER + 1)})["error"]
    assert j.calls == [("p1", "ABCD"), ("p9", "x")]


def _asked(tmp_path, job_id, kind):
    """A job with a real question pending and a real FIFO whose read end is
    already open, so job.answer takes the path a run takes: the line reaches
    a reader, and only a write that got there emits anything. Returns the job
    and that read fd. O_RDONLY|O_NONBLOCK opens a FIFO with no writer on it
    straight away, so the answer's own open cannot lose a race with a reader
    thread and make this test a flake."""
    job = O.Job(job_id, "stack", {})
    job.pipe_path = str(tmp_path / (job_id + ".fifo"))
    os.mkfifo(job.pipe_path)
    job.emit(E.from_script({"seq": 1, "ts": "t", "type": "prompt", "id": "p1",
                            "question": "KVO admin password: ", "kind": kind}))
    return job, os.open(job.pipe_path, os.O_RDONLY | os.O_NONBLOCK)


def _strings(node, out=None):
    """Every string anywhere in a structure, at any depth."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            out.append(str(k))
            _strings(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _strings(v, out)
    elif isinstance(node, str):
        out.append(node)
    return out


def test_an_answered_question_says_so_in_the_stream(tmp_path):
    """The ok this route returns is not the record: the record is the frame
    job.answer emits, because that is the only thing a page attaching to the
    run later can read. Without it every replayed question looked unanswered
    and the Watch screen re-opened its modal on one the engine had moved past."""
    job, rfd = _asked(tmp_path, "jans", "text")
    try:
        assert api.answer(job, {"prompt_id": "p1", "text": "lab-2"}) == {"ok": True}
        assert os.read(rfd, 4096) == b"lab-2\n", "the engine gets the line it is blocked on"
    finally:
        os.close(rfd)
    ev = job.buffer[-1]
    assert ev["type"] == E.ANSWERED == "answered"
    assert ev["prompt_id"] == "p1" and ev["shown"] == "lab-2"
    assert ev["id"] > job.buffer[0]["id"], "emit stamped it, in buffer order, after the question"


def test_the_value_of_a_secret_answer_is_in_no_frame_of_the_stream(tmp_path):
    """The engine gets what was typed. The stream gets asterisks, and the
    typed value appears nowhere in it: not on the answered frame, not on any
    other, not in the bytes the browser would be sent."""
    typed = "Zq7-CANARY-never-in-the-stream"
    job, rfd = _asked(tmp_path, "jsec", "secret")
    try:
        assert api.answer(job, {"prompt_id": "p1", "text": typed}) == {"ok": True}
        assert os.read(rfd, 4096) == (typed + "\n").encode(), "the engine gets the real value"
    finally:
        os.close(rfd)
    ev = job.buffer[-1]
    assert ev["type"] == E.ANSWERED and ev["prompt_id"] == "p1"
    assert ev["shown"] == O.MASKED == "********"
    for text in _strings(job.buffer):
        assert typed not in text, text
    assert typed not in "".join(E.to_sse(e) for e in job.buffer)


def test_a_refused_answer_puts_nothing_in_the_stream(tmp_path):
    """A 409 is an answer that never reached the engine, so the question is
    still open - and the stream has to keep saying so, or a reload would show
    it settled by an answer nothing took."""
    job, rfd = _asked(tmp_path, "jref", "text")
    before = len(job.buffer)
    try:
        r = api.answer(job, {"prompt_id": "p9", "text": "x"})
        assert r["http"] == 409 and r["error"].startswith("The prompt waiting is p1")
        # no writer ever opened the pipe, so the read end is at end of file:
        # nothing reached the engine either
        assert os.read(rfd, 4096) == b""
    finally:
        os.close(rfd)
    assert len(job.buffer) == before, "no frame: the question is exactly as open as it was"
    assert job.pending_prompt == "p1"


# -------------------------------------------------------------- licences
class FakeKL(object):
    """kvo_license.py's surface as the API uses it, recording every call."""
    def __init__(self, licences=None, ents=None, states=None, eula=True):
        self.calls = []
        self.licences = licences if licences is not None else []
        self.ents = ents or {}
        self.states = states or {}
        self.eula = eula

    def accept_eula(self, kvo, verify):
        self.calls.append(("eula", kvo))
        return self.eula

    def token(self, kvo, user, pw, verify):
        self.calls.append(("token", kvo, user, pw, verify))
        if pw == "wrong":
            # what urllib raises on the KVO's 401: an HTTPError whose code is the status
            raise urllib.error.HTTPError("https://%s/auth/realms/keysight/protocol/openid-connect/token" % kvo,
                                         401, "Unauthorized", {}, None)
        if pw == "down":
            raise urllib.error.URLError("[Errno 61] Connection refused")
        return "TOK"

    def _req(self, method, url, token=None, body=None, verify=False, timeout=30):
        self.calls.append(("req", method, url, token, body))
        if url.endswith("/licensing/licenses"):
            return 200, list(self.licences)
        return 202, {"url": "/op/" + url.rsplit("/", 1)[-1]}

    def lookup_code(self, kvo, base, tok, code, verify, timeout=None):
        # the timeout is recorded: check loops over a paste of codes inside
        # one request, and kvo_license.py's poll_op applies its own 120
        # second default per code to a caller that passes none
        self.calls.append(("lookup", code, timeout))
        return self.ents.get(code, []), {"state": "SUCCESS" if code in self.ents else "FAILED"}

    def poll_op(self, kvo, tok, first, verify, want_result=True, timeout=120, label=None):
        self.calls.append(("poll", first))
        op = first["url"].rsplit("/", 1)[-1]
        state = self.states.get(op, "SUCCESS")
        if op == "deactivate" and state == "SUCCESS":
            self.licences = []
        return {"state": state, "result": {"op": op}}


def test_licences_list_check_activate_release(monkeypatch):
    kl = FakeKL(licences=[{"activationCode": "AAAA-1111", "product": "KVO-DEVICE", "quantity": 5}],
                ents={"BBBB-2222": [("CloudLens-Credit", 10, 20)], "CCCC-3333": [("KVO-DEVICE", 0, 5)]})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    creds = {"kvo": "10.1.2.3", "user": "admin", "password": "hunter2"}
    r = api.licences(dict(creds, action="list"))
    assert r["licences"] == kl.licences and r["count"] == 1
    assert ("token", "10.1.2.3", "admin", "hunter2", False) in kl.calls
    assert ("req", "GET", "https://10.1.2.3/api/v2/licensing/licenses", "TOK", None) in kl.calls
    assert "hunter2" not in json.dumps(r), "the password reaches the KVO and nothing else"
    # check: one lookup per code, the entitlements as rows
    r = api.licences(dict(creds, action="check", codes=["BBBB-2222", "ZZZZ-9999"]))
    assert r["codes"] == [
        {"code": "BBBB-2222", "valid": True, "state": "SUCCESS", "running": False,
         "entitlements": [{"product": "CloudLens-Credit", "available": 10, "total": 20}]},
        {"code": "ZZZZ-9999", "valid": False, "state": "FAILED", "running": False, "entitlements": []}]
    # every lookup was given what was left of the request's budget, not
    # kvo_license.py's own 120 second default per code
    looks = [c[2] for c in kl.calls if c[0] == "lookup"]
    assert len(looks) == 2 and all(0 < t <= api.OP_BUDGET for t in looks), looks
    # activate: the available quantity unless one is given; a 0-available code is skipped
    r = api.licences(dict(creds, action="activate", codes=["BBBB-2222,3", "CCCC-3333"]))
    posts = [c for c in kl.calls if c[0] == "req" and c[1] == "POST" and c[2].endswith("/operations/activate")]
    assert [c[4] for c in posts] == [[{"activationCode": "BBBB-2222", "quantity": 3}]]
    assert r["results"][0]["ok"] and r["results"][0]["state"] == "SUCCESS"
    assert r["results"][1]["ok"] is False and r["results"][1]["state"] == "nothing-available"
    assert r["activated"] == 1 and r["licences"] == kl.licences
    # release: operations/deactivate per row, polled; released when every row succeeded
    r = api.licences(dict(creds, action="release", rows=[{"activationCode": "AAAA-1111", "quantity": 5}]))
    posts = [c for c in kl.calls if c[0] == "req" and c[1] == "POST" and c[2].endswith("/operations/deactivate")]
    assert [c[4] for c in posts] == [[{"activationCode": "AAAA-1111", "quantity": 5}]]
    assert r["released"] is True and r["clear"] is True and r["licences"] == []
    assert r["results"] == [{"code": "AAAA-1111", "quantity": 5, "state": "SUCCESS", "ok": True,
                             "running": False, "result": {"op": "deactivate"}}]
    assert r["running"] == 0 and "unreadable" not in r
    # the action may also come from the route path
    assert api.licences(creds, action="list")["count"] == 0


def test_licences_refuses_bad_input_and_reports_auth_without_the_password(monkeypatch):
    kl = FakeKL()
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    creds = {"kvo": "kvo.example.net", "password": "wrong"}
    r = api.licences(dict(creds, action="list"))
    assert r["http"] == 401 and r["error"] == "KVO rejected the username or password"
    r = api.licences(dict(creds, action="list", password="down"))
    assert r["http"] == 502 and "login failed" in r["error"] and "down" not in r["error"]
    n = len(kl.calls)
    assert "action" in api.licences({"kvo": "1.2.3.4"})["error"]
    assert "kvo" in api.licences({"kvo": "https://1.2.3.4/x", "action": "list"})["error"]
    assert "kvo" in api.licences({"kvo": "1.2.3.4:443", "action": "list"})["error"]
    assert "codes" in api.licences({"kvo": "1.2.3.4", "action": "check"})["error"]
    assert "codes" in api.licences({"kvo": "1.2.3.4", "action": "check", "codes": ["bad code"]})["error"]
    assert "rows" in api.licences({"kvo": "1.2.3.4", "action": "release"})["error"]
    assert "quantity" in api.licences({"kvo": "1.2.3.4", "action": "release",
                                       "rows": [{"activationCode": "AAAA-1111", "quantity": 0}]})["error"]
    assert api.licences("nope")["error"]
    assert "user and password" in api.licences({"kvo": "1.2.3.4", "action": "list", "password": "a\nb"})["error"]
    assert "user and password" in api.licences({"kvo": "1.2.3.4", "action": "list",
                                                "password": "x" * (api.MAX_PASSWORD + 1)})["error"]
    assert "user and password" in api.licences({"kvo": "1.2.3.4", "action": "list",
                                                "user": "u" * (api.MAX_USER + 1)})["error"]
    assert len(kl.calls) == n, "a refused body never reaches the KVO"
    # the failed deactivate is reported as not released
    kl2 = FakeKL(licences=[{"activationCode": "AAAA-1111", "quantity": 5}], states={"deactivate": "FAILED"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl2)
    r = api.licences({"kvo": "1.2.3.4", "action": "release", "rows": [{"code": "AAAA-1111", "quantity": 5}]})
    assert r["released"] is False and r["clear"] is False and r["results"][0]["ok"] is False


def test_licences_eula_and_login_refusals_have_their_own_answers(monkeypatch):
    kl = FakeKL(eula=False)
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    creds = {"kvo": "10.1.2.3", "password": "hunter2"}
    # accept_eula's False went unread, and the login then failed on the
    # EULA redirect's HTML with an error that read as a broken KVO
    r = api.licences(dict(creds, action="list", accept_eula=True))
    assert r["http"] == 502 and r["error"] == "the KVO's EULA is pending and could not be accepted"
    assert ("eula", "10.1.2.3") in kl.calls and not any(c[0] == "token" for c in kl.calls), "no login was tried"
    kl.eula = True
    assert api.licences(dict(creds, action="list", accept_eula=True))["count"] == 0
    assert kl.calls.count(("eula", "10.1.2.3")) == 2
    api.licences(dict(creds, action="list"))
    assert kl.calls.count(("eula", "10.1.2.3")) == 2, "the EULA is only touched when the body asks"

    # a 403 from the token endpoint is the same refusal as a 401; any other
    # status is the 502 with the KVO's words
    def forbidden(kvo, user, pw, verify):
        raise urllib.error.HTTPError("https://x/token", 403, "Forbidden", {}, None)

    def broken(kvo, user, pw, verify):
        raise urllib.error.HTTPError("https://x/token", 503, "Service Unavailable", {}, None)

    kl.token = forbidden
    r = api.licences(dict(creds, action="list"))
    assert r["http"] == 401 and r["error"] == "KVO rejected the username or password"
    kl.token = broken
    r = api.licences(dict(creds, action="list"))
    assert r["http"] == 502 and "503" in r["error"] and "hunter2" not in r["error"]


class _ListKL(FakeKL):
    """FakeKL whose GET licenses answers exactly what the test chose.

    kvo_license.py's _req does not raise on an HTTP status error: it hands
    back (code, body), and the body of a 500 is a dict, of a KVO still
    behind its EULA is an HTML page, of one that answered nothing is None.
    Every one of those has to be told apart from the empty list a KVO
    holding no licences returns."""
    def __init__(self, code, body, **kw):
        FakeKL.__init__(self, **kw)
        self.list_code, self.list_body = code, body

    def _req(self, method, url, token=None, body=None, verify=False, timeout=30):
        if url.endswith("/licensing/licenses"):
            self.calls.append(("req", method, url, token, body))
            return self.list_code, self.list_body
        return FakeKL._req(self, method, url, token, body, verify, timeout)


LICENCE_ROW = {"activationCode": "AAAA-1111", "product": "KVO-DEVICE", "quantity": 5}
HTML_EULA = "<html><head><title>End User Licence Agreement</title></head><body>...</body></html>"


@pytest.mark.parametrize("code,body,clear,count,readable", [
    (200, [], True, 0, True),                       # the KVO holds nothing, and said so
    (200, [LICENCE_ROW], False, 1, True),           # it holds one, and said so
    (500, {"error": "internal server error"}, None, 0, False),
    (200, HTML_EULA, None, 0, False),               # the EULA redirect's page
    (200, None, None, 0, False),                    # no body at all
    (200, {"licenses": [LICENCE_ROW]}, False, 1, False),   # a wrapped list: rows prove holdings
    (200, {"licenses": []}, None, 0, False),        # an empty envelope proves nothing
])
def test_an_unreadable_licence_list_is_never_reported_as_clear(code, body, clear, count, readable,
                                                               monkeypatch):
    """`clear` is the KVO's own answer to "do you still hold licences",
    and it is the last thing between a stack and --accept-licence-loss.

    It was computed as `not (rows if isinstance(rows, list) else [])`. An
    HTTP error body, an HTML EULA page, a None and a wrapped list all
    became [], [] became "the KVO holds nothing", licences.js recorded
    that as this session's evidence and teardown.js turned it into
    --accept-licence-loss on the argv of a run that deletes the KVO. Four
    shapes, none of them a reading, all of them clear. The script puts the
    cost of one of those at 1500 counts.

    So the read is tri-state: True only on a genuine empty list, False on
    any answer that named a licence, and None with a reason otherwise. The
    page treats a missing or null `clear` as not-clear, which is why None
    is the right value for "I could not tell" and False is not: False
    reads as an answer the KVO gave."""
    kl = _ListKL(code, body)
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                      "rows": [{"activationCode": "AAAA-1111", "quantity": 5}]})
    assert r["clear"] is clear, r
    assert r["count"] == count and len(r["licences"]) == count
    assert ("unreadable" in r) is (not readable), r
    if not readable:
        assert r["unreadable"] and "list" in r["unreadable"]
        # the reason says the shape and the status, never the KVO's own
        # words: an error body is arbitrary text on an operator's screen
        assert "internal server error" not in r["unreadable"]
        assert "<html" not in r["unreadable"]
    # the deactivate itself succeeded either way: `released` is about the
    # rows this call asked for, `clear` about the appliance
    assert r["released"] is True
    # and the same answer through list, which the Licensing screen reads
    listed = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "list"})
    assert listed["clear"] is clear and listed["count"] == count


def test_a_licence_list_the_kvo_never_answered_is_not_an_empty_one(monkeypatch):
    """_req can also raise: a socket that never opened is not a KVO that
    holds nothing."""
    class _Broken(FakeKL):
        def _req(self, method, url, token=None, body=None, verify=False, timeout=30):
            if url.endswith("/licensing/licenses"):
                raise urllib.error.URLError("[Errno 61] Connection refused")
            return FakeKL._req(self, method, url, token, body, verify, timeout)

    monkeypatch.setattr(api, "_kvo_license", lambda: _Broken())
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "list"})
    assert r["clear"] is None and r["count"] == 0
    assert "URLError" in r["unreadable"], r


def test_a_poll_that_ran_out_of_its_budget_is_not_a_success(monkeypatch):
    """kvo_license.py's poll_op returns IN_PROGRESS in exactly one case:
    it hit its timeout with the KVO still working. _op_ok read that as
    success ("FAIL" not in it, "ERROR" not in it), so a row nobody watched
    to its end was counted as done.

    That is not a rare shape. OP_BUDGET is shared across the rows of one
    call, so the LAST rows of a full call are the ones that get a second
    or two: rows 4 and 5 of a five-code activate. "5 of 5 activated" then
    included two operations with no outcome, and on the release side a
    row that never finished sat beside `clear` and turned the teardown
    banner green."""
    assert api._op_ok("IN_PROGRESS") is False and api._op_running("IN_PROGRESS") is True
    assert api._op_ok("in_progress") is False, "the state is compared case-insensitively"
    assert api._op_ok("SUCCESS") is True and api._op_running("SUCCESS") is False
    assert api._op_ok("FAILED") is False and api._op_ok("") is False and api._op_ok(None) is False

    kl = FakeKL(ents={"BBBB-2222": [("CloudLens-Credit", 10, 20)]}, states={"activate": "IN_PROGRESS"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "activate", "codes": ["BBBB-2222,3"]})
    assert r["results"][0]["ok"] is False and r["results"][0]["running"] is True
    assert r["activated"] == 0 and r["running"] == 1, "a row still running is not a row activated"

    # a release whose poll timed out, over a KVO whose list could not be
    # read either: neither half may be reported as done
    kl = _ListKL(503, {"error": "service unavailable"}, licences=[LICENCE_ROW],
                 states={"deactivate": "IN_PROGRESS"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                      "rows": [{"activationCode": "AAAA-1111", "quantity": 5}]})
    assert r["released"] is False and r["running"] == 1
    assert r["results"][0]["running"] is True and r["results"][0]["ok"] is False
    assert r["clear"] is None, "an unread list beside an unfinished release is not a clear KVO"

    # and one whose list DID come back, still holding the licence
    kl = FakeKL(licences=[LICENCE_ROW], states={"deactivate": "IN_PROGRESS"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                      "rows": [{"activationCode": "AAAA-1111", "quantity": 5}]})
    assert r["released"] is False and r["clear"] is False and r["count"] == 1


def test_only_a_state_that_says_done_is_read_as_done(monkeypatch):
    """_op_ok was a deny-list: truthy, not IN_PROGRESS, no FAIL, no ERROR.

    poll_op stops at the FIRST state that is neither empty nor
    IN_PROGRESS, so any word a KVO uses for "accepted, not finished yet" -
    PENDING, QUEUED - arrives here as the last word on the row, and the
    deny-list read every one of them as a success. On an activate that is
    entitlement reported as spent with nobody having watched it land; on a
    release it is a row that sits beside `clear` and turns the teardown
    banner green. So it is an allow-list of what is known to mean done,
    and everything else is running-or-unknown, which is the safe
    direction: a real "done" word missing from _OP_DONE costs one row
    reported as unknown, and a wrong guess costs a licence count.

    The other shape is the empty state. poll_op reads `state` out of the
    body it polls and leaves it "" when that body is not the JSON object
    it expects (an HTML page from a pending EULA, a plain-text error, no
    body at all), and "" never breaks its loop, so an empty state means
    the poll ran to its deadline having never read one. That is a
    TIMEOUT, and it came back ok:false running:false, which the page
    prints as "refused": the operator was told the KVO rejected a code it
    never answered about."""
    assert api._op_ok("SUCCESS") is True and api._op_running("SUCCESS") is False
    assert api._op_ok("success") is True, "the state is compared case-insensitively"
    # a refusal IS an outcome, and the only one that may be shown as one
    for word in ("FAILED", "FAILURE", "INTERNAL_ERROR", "error"):
        assert api._op_ok(word) is False, word
        assert api._op_running(word) is False, word
    # everything else: not done, not refused, not known
    for word in ("IN_PROGRESS", "PENDING", "QUEUED", "ACCEPTED", "COMPLETED", "", None):
        assert api._op_ok(word) is False, word
        assert api._op_running(word) is True, "%r is not an outcome this code knows" % (word,)

    # a KVO that answers PENDING to an activate: nothing is counted as
    # activated, and the row says its outcome is unknown
    kl = FakeKL(ents={"BBBB-2222": [("CloudLens-Credit", 10, 20)]}, states={"activate": "PENDING"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "activate", "codes": ["BBBB-2222,3"]})
    assert r["activated"] == 0 and r["running"] == 1, r
    assert r["results"][0]["ok"] is False and r["results"][0]["running"] is True

    # and a release whose poll never read a state at all: the timeout that
    # used to be reported as a refusal. The KVO still holds the row it was
    # asked to give back, which is the other half of the same answer.
    kl = FakeKL(licences=[LICENCE_ROW], states={"deactivate": ""})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release",
                      "rows": [{"activationCode": "AAAA-1111", "quantity": 5}]})
    assert r["released"] is False and r["running"] == 1, r
    assert r["results"][0]["ok"] is False and r["results"][0]["running"] is True, (
        "a poll that never read a state is a timeout, not a code the KVO refused")
    assert r["clear"] is False and r["count"] == 1


class _TimedLookupKL(FakeKL):
    """FakeKL that records the timeout each lookup was given and lets the
    lookup take time on a clock the test holds."""
    def __init__(self, clock, per_lookup=0.0, **kw):
        FakeKL.__init__(self, **kw)
        self.clock, self.per_lookup, self.timeouts = clock, per_lookup, []

    def lookup_code(self, kvo, base, tok, code, verify, timeout=None):
        self.timeouts.append(timeout)
        self.clock[0] += self.per_lookup
        return FakeKL.lookup_code(self, kvo, base, tok, code, verify, timeout)


def test_check_is_capped_in_rows_and_shares_the_requests_polling_budget(monkeypatch):
    """check is where a customer's whole paste lands, and it was the one
    licensing action with neither bound: MAX_LIST let 50 codes through,
    and _lookup passed no timeout at all, so kvo_license.py's poll_op
    applied its own 120 second default PER CODE. Fifty codes was one
    request that could hold the browser's connection for an hour and a
    half. It spends nothing, so its row cap is its own (MAX_CHECK), but it
    shares the same OP_BUDGET as the calls that do."""
    clock = [1000.0]
    monkeypatch.setattr(api, "_now", lambda: clock[0])
    kl = _TimedLookupKL(clock, per_lookup=100.0, ents={"BBBB-2222": [("CloudLens-Credit", 10, 20)]})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "check",
                      "codes": ["BBBB-2222", "CCCC-3333", "DDDD-4444"]})
    assert len(r["codes"]) == 3
    # one budget, spent across the rows, and never below 1: not three
    # fresh 120 second defaults
    assert kl.timeouts == [api.OP_BUDGET, api.OP_BUDGET - 100, 1], kl.timeouts

    n = len(kl.calls)
    many = ["AAAA-%04d" % i for i in range(api.MAX_CHECK + 1)]
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "check", "codes": many})
    assert r.get("error") and str(api.MAX_CHECK) in r["error"] and str(len(many)) in r["error"], r
    assert len(kl.calls) == n, "a body over the limit never reaches the KVO, not even to log in"
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "check",
                      "codes": ["AAAA-%04d" % i for i in range(api.MAX_CHECK)]})
    assert len(r["codes"]) == api.MAX_CHECK, "exactly the limit is allowed"
    assert api.MAX_CHECK < api.MAX_LIST, "a paste of MAX_LIST codes is not one polled request"
    # a lookup that ran out of the budget says so, rather than reporting a
    # good activation code as one the KVO did not recognise
    kl = FakeKL(states={"retrieve-activation-code-info": "IN_PROGRESS"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)

    def timed_out(kvo, base, tok, code, verify, timeout=None):
        return [], {"state": "IN_PROGRESS"}

    kl.lookup_code = timed_out
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "check", "codes": ["BBBB-2222"]})
    assert r["codes"][0] == {"code": "BBBB-2222", "valid": False, "state": "IN_PROGRESS",
                             "running": True, "entitlements": []}


def test_a_refused_code_is_named_by_position_and_never_echoed(tmp_path, monkeypatch):
    # a code is a secret and a near miss is most of one: the error names
    # the entry, not the value
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "_kvo_license", _never)
    r = api.licences({"kvo": "1.2.3.4", "action": "check", "codes": ["AAAA-1111", "1234 ABCD-5678"]})
    assert r["error"] == "codes: entry 2 is not an activation code"
    r = api.licences({"kvo": "1.2.3.4", "action": "release", "rows": [
        {"activationCode": "AAAA-1111", "quantity": 1}, {"activationCode": "1234 ABCD-5678", "quantity": 1}]})
    assert r["error"] == "rows: entry 2 is not an activation code"
    r = api.licences({"kvo": "1.2.3.4", "action": "release", "rows": [{"activationCode": "AAAA-1111", "quantity": 0}]})
    assert r["error"] == "rows: entry 1 needs the quantity to release (a positive integer)"
    r = api.licences({"kvo": "1.2.3.4", "action": "release", "codes": ["AAAA-1111,2", "1234 ABCD"]})
    assert r["error"] == "codes: entry 2 is not an activation code (CODE or CODE,QTY)"
    # a code never starts with a hyphen: on the argv after --kvo-codes it
    # would be a flag to the script, and --events one it acts on
    r = api.run({"plan": GOOD, "kvo_codes": ["AAAA-1111", "--events"]}, jobs={}, start=_never)
    assert r["errors"] == ["kvo_codes: codes: entry 2 is not an activation code (CODE or CODE,QTY)"]
    assert not os.listdir(str(tmp_path))
    for bad in ("-AAA-1111", "--events", "abc", "-", "AAAA 1111", "A" * 65):
        assert not api._shape(api.CODE, bad) and not api._shape(api.CODE_QTY, bad), bad
    for good in ("A-AA-1111", "1234", "1234-ABCD-5678-EFGH-9012", "A" * 64):
        assert api._shape(api.CODE, good) and api._shape(api.CODE_QTY, good + ",5"), good
    assert api.MAX_LIST == 50
    over = api._check_codes(["A123"] * 51)[1]
    assert "at most 50" in over and "51" in over, over
    # and it says what to do next, as the MAX_CHECK and MAX_OPS refusals
    # do: a refusal that only names a limit leaves the operator to guess
    assert "batches" in over, over
    not_a_list = api._check_codes("A123-4567")[1]
    assert "list" in not_a_list and "CODE,QTY" in not_a_list, not_a_list
    assert "at most 50" in api._release_rows({"rows": [{"activationCode": "A123", "quantity": 1}] * 51})[1]
    assert "vpcs" in api.discover_workloads("us-east-1", "k=v", ",".join(["vpc-0a0a0a0a"] * 51))["error"]


def test_kvo_license_imports_cleanly_from_the_scripts_dir():
    kl = api._kvo_license()
    for name in ("_req", "token", "lookup_code", "poll_op", "accept_eula"):
        assert callable(getattr(kl, name)), name


def test_kvo_license_token_form_encodes_the_credentials(monkeypatch):
    # scripts/kvo_license.py (shared with the CLI) built the token form by
    # string formatting, so a password holding &, =, %, + or a non-ASCII
    # character reached Keycloak split or mangled and the right password
    # was refused. urlencode lets every byte through.
    kl = api._kvo_license()
    seen = {}

    class Resp(object):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token": "TOK"}'

    def fake_urlopen(req, context=None, timeout=None):
        seen["url"], seen["data"], seen["ctype"] = req.full_url, req.data, req.get_header("Content-type")
        return Resp()

    monkeypatch.setattr(kl.urllib.request, "urlopen", fake_urlopen)
    pw = "p&ss=w+rd%25 caf\u00e9"
    assert kl.token("10.1.2.3", "ad min", pw, False) == "TOK"
    assert seen["url"] == "https://10.1.2.3/auth/realms/keysight/protocol/openid-connect/token"
    assert seen["ctype"] == "application/x-www-form-urlencoded"
    form = urllib.parse.parse_qs(seen["data"].decode("utf-8"), strict_parsing=True)
    assert form == {"grant_type": ["password"], "client_id": ["vision-orchestrator"],
                    "username": ["ad min"], "password": [pw]}


# ------------------------------------------------------------------- SSE
def test_sse_resumes_from_last_event_id():
    job = O.Job("j", "deploy", {}); [job.emit(E.log("l%d" % i)) for i in range(5)]
    ids = [e["id"] for e in job.buffer]
    later = server.events_after(job, last_id=ids[2])
    assert [e["id"] for e in later] == ids[3:]


def test_ids_are_stamped_by_emit_in_buffer_order():
    # A frame has no id until a job emits it: two producer threads used to
    # mint ids in events.py before the append, so the buffer could hold a
    # larger id before a smaller one and a resume from the smaller one
    # skipped the larger.
    assert "id" not in E.log("x") and "id" not in E.from_script({"seq": 1, "type": "phase", "name": "a"})
    job = O.Job("j", "deploy", {})
    stop = threading.Event()

    def producer(tag):
        while not stop.is_set():
            job.emit(E.log(tag))

    threads = [threading.Thread(target=producer, args=(t,)) for t in ("tail", "stdout")]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 10
    while len(job.buffer) < 2000 and time.monotonic() < deadline:
        pass
    stop.set()
    for t in threads:
        t.join(5)
    assert len(job.buffer) >= 2000, "the producers never got going"
    ids = [e["id"] for e in job.buffer]
    assert ids == list(range(1, len(ids) + 1)), "ids are the buffer positions, both producers interleaved"
    assert [e["id"] for e in server.events_after(job, 1990)] == ids[1990:]
    assert server.events_after(job, 10 ** 9) == []
    assert server.last_event_id("17") == 17
    for junk in (None, "", "abc", "-3", "1.5", "1e3"):
        assert server.last_event_id(junk) == 0, junk


# ---------------------------------------------------------------- server
@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(server, "KEEPALIVE_SECS", 0.2)
    httpd = server.serve("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    saved = dict(server.JOBS)
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.JOBS.clear()
        server.JOBS.update(saved)


def _call(port, method, path, body=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": "127.0.0.1:%d" % port}
    data = raw
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body)
    h.update(headers or {})
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    if (r.getheader("Content-Type") or "").startswith("application/json"):
        return r.status, json.loads(payload.decode("utf-8"))
    return r.status, payload


def test_post_routes_reject_a_foreign_host_and_origin(live, monkeypatch):
    monkeypatch.setattr(api.subprocess, "run", _never)
    good = {"plan": GOOD}
    st, r = _call(live, "POST", "/api/plan", good, headers={"Host": "evil.example:%d" % live})
    assert st == 403 and "Host" in r["error"]
    st, r = _call(live, "POST", "/api/plan", good, headers={"Host": "127.0.0.1.evil.example"})
    assert st == 403
    st, r = _call(live, "POST", "/api/plan", good, headers={"Host": "localhost:%d" % live})
    assert st == 200 and 'CLOUDLENS_STACK_NAME="demo"' in r["profile_text"]
    st, r = _call(live, "POST", "/api/plan", good)
    assert st == 200
    # a browser page on another origin names itself: refused; same-origin passes
    st, r = _call(live, "POST", "/api/plan", good, headers={"Origin": "http://evil.example"})
    assert st == 403 and "Origin" in r["error"]
    st, r = _call(live, "POST", "/api/plan", good, headers={"Origin": "http://localhost:%d" % live})
    assert st == 200
    # the origin's port is the console's own: another local server's page
    # is another origin, and no port is 80 or 443, never the console's
    st, r = _call(live, "POST", "/api/plan", good, headers={"Origin": "http://localhost:%d" % (live + 1)})
    assert st == 403 and "Origin" in r["error"]
    st, r = _call(live, "POST", "/api/plan", good, headers={"Origin": "http://127.0.0.1"})
    assert st == 403
    assert server.origin_ok("http://localhost:8760", 8760) and server.origin_ok(None, 8760)
    assert not server.origin_ok("http://localhost:99999", 8760) and not server.origin_ok("https://127.0.0.1:8760", 8761)
    assert not server.origin_ok("http://localhost:8760x", 8760) and not server.origin_ok("null", 8760)
    # the guard covers the older POST routes too
    st, r = _call(live, "POST", "/stop/nope", headers={"Host": "evil.example"})
    assert st == 403
    # and every GET that runs a command or opens a stream (reproduced: a
    # rebinding page could GET /api/doctor and /api/discover/* by its own
    # name); the page itself (/, /flows, /web/) is fetched by name and
    # runs nothing, so it stays open
    st, r = _call(live, "GET", "/api/discover/vpcs?region=us-east-1", headers={"Host": "evil.example:%d" % live})
    assert st == 403 and "Host" in r["error"]
    st, r = _call(live, "GET", "/api/doctor?region=us-east-1", headers={"Host": "evil.example"})
    assert st == 403
    st, r = _call(live, "GET", "/events/nope", headers={"Host": "evil.example:%d" % live})
    assert st == 403
    for path in ("/", "/flows"):
        st, r = _call(live, "GET", path, headers={"Host": "evil.example"})
        assert st == 200, path


def test_api_gets_refuse_a_cross_site_fetch(live, monkeypatch):
    # Reproduced: a page on any origin could fire GET /api/doctor or
    # /api/discover/* (a GET needs no preflight), and the command ran
    # whether or not the page could read the answer. A browser names the
    # request's provenance in Sec-Fetch-Site: the console's own page says
    # same-origin, a typed URL says none, and curl says nothing at all.
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"Vpcs": []}', stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    path = "/api/discover/vpcs?region=us-east-1"
    for site in ("cross-site", "same-site", "Cross-Site"):
        st, r = _call(live, "GET", path, headers={"Sec-Fetch-Site": site})
        assert st == 403 and "Sec-Fetch-Site" in r["error"], site
    st, r = _call(live, "GET", "/events/nope", headers={"Sec-Fetch-Site": "cross-site"})
    assert st == 403
    assert not calls, "a refused fetch never reached the CLI"
    for site in ("same-origin", "none", None):
        st, r = _call(live, "GET", path, headers={"Sec-Fetch-Site": site} if site else None)
        assert st == 200 and r == [], site
    assert len(calls) == 3
    # the page itself is open to any fetch: it runs nothing
    st, _ = _call(live, "GET", "/", headers={"Sec-Fetch-Site": "cross-site"})
    assert st == 200
    assert server.fetch_site_ok(None) and server.fetch_site_ok(" same-origin ") and not server.fetch_site_ok("")


def test_a_route_that_raises_answers_500_naming_only_the_exception_type(live, monkeypatch, capsys):
    # Reproduced: an exception in a route closed the connection with no
    # response (the client saw a dropped socket) while the handler printed
    # the traceback. Now every dispatch is caught: 500 JSON with the type
    # alone, since the message can carry a path, an argv or a KVO's words,
    # and the traceback on the console's own stderr.
    def boom(arg):
        raise RuntimeError("secret-bearing message: /Users/x/deploy-profile-demo.env 1234-ABCD-5678")

    monkeypatch.setattr(api, "plan", boom)
    st, r = _call(live, "POST", "/api/plan", {"plan": GOOD})
    assert st == 500 and r == {"error": "internal error: RuntimeError"}
    assert "RuntimeError: secret-bearing message" in capsys.readouterr().err, "the traceback went to stderr"

    # a subprocess that cannot be run is an answer too, not a dropped connection
    def denied(argv, **kw):
        raise PermissionError(13, "Permission denied", "aws")

    monkeypatch.setattr(api.subprocess, "run", denied)
    st, r = _call(live, "GET", "/api/discover/vpcs?region=us-east-1")
    assert st == 502 and r["error"] == "the aws CLI could not be run: Permission denied"
    # the same net under a GET route and under the SSE route's setup
    monkeypatch.setattr(api, "discover_vpcs", boom)
    st, r = _call(live, "GET", "/api/discover/vpcs?region=us-east-1")
    assert st == 500 and r == {"error": "internal error: RuntimeError"}
    monkeypatch.setattr(server, "job_id_ok", boom)
    st, r = _call(live, "GET", "/events/whatever")
    assert st == 500 and r == {"error": "internal error: RuntimeError"}
    assert capsys.readouterr().err.count("Traceback") == 2


def test_the_page_routes_that_raise_answer_500_too(live, monkeypatch, capsys):
    # Reproduced: the catch-all covered /api/ and /events/ only. A raise in
    # _file (the page, its css and js) left the try, and the browser saw a
    # dropped connection with no status line. Every route is under it now.
    def boom(self, rel, ctype):
        raise RuntimeError("cannot read /Users/x/web/" + rel)

    monkeypatch.setattr(server.Handler, "_file", boom)
    for path in ("/", "/web/app.css", "/web/app.js"):
        st, r = _call(live, "GET", path)
        assert st == 500 and r == {"error": "internal error: RuntimeError"}, path
    err = capsys.readouterr().err
    assert err.count("Traceback") == 3 and "cannot read" in err, "the message stays on the console's stderr"


def test_the_scripts_are_served_with_the_charset_their_glyphs_need(live):
    # The scripts carry their glyphs as themselves: the middot between the
    # fields of the Watch chip, the check and the cross of its phase marks.
    # Served as application/javascript with no charset, an external script
    # is decoded in the document's encoding or the browser's default, and
    # those UTF-8 bytes read as mojibake. index.html has said charset=utf-8
    # all along; the scripts say it now too, which also covers whatever
    # glyph lands next. watch.js is the one read here because it is the one
    # with the most of them; the header is the same for every /web/ script.
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=5)
    c.request("GET", "/web/watch.js", headers={"Host": "127.0.0.1:%d" % live})
    r = c.getresponse()
    status, ctype, body = r.status, r.getheader("Content-Type"), r.read()
    c.close()
    assert status == 200
    assert ctype == "application/javascript; charset=utf-8", ctype
    assert "\u00b7" in body.decode("utf-8"), "the middot is in the file as itself"


def test_a_client_that_hung_up_is_dropped_without_a_word(live, monkeypatch, capsys):
    # A closed tab or an aborted fetch surfaces as BrokenPipeError (or
    # ConnectionResetError) from the write. That is not an operator event:
    # before, it escaped the handler and socketserver printed a traceback
    # for every one; now the connection is dropped and nothing is said.
    real_send, gone = server.Handler._send, {"on": True}

    def send(self, *a, **kw):
        if gone["on"]:
            raise BrokenPipeError(32, "Broken pipe")
        return real_send(self, *a, **kw)

    monkeypatch.setattr(server.Handler, "_send", send)
    for method, path, body in (("GET", "/flows", None), ("GET", "/events/nope", None),
                               ("POST", "/api/plan", {"plan": GOOD})):
        c = http.client.HTTPConnection("127.0.0.1", live, timeout=5)
        h = {"Host": "127.0.0.1:%d" % live}
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        with pytest.raises(ConnectionResetError):    # RemoteDisconnected: no status line, the socket closed
            c.getresponse()
        c.close()
    out = capsys.readouterr()
    assert out.err == "" and out.out == "", "a client that left is nothing to report"
    # the handler returned and the server is still there for the next request
    gone["on"] = False
    st, r = _call(live, "GET", "/flows")
    assert st == 200 and r["order"] == ["stack", "sensors", "kvo", "mirror"]


def test_post_bodies_are_json_objects_under_the_cap(live):
    st, r = _call(live, "POST", "/api/plan", raw=b"{not json", headers={"Content-Type": "application/json"})
    assert st == 400 and "JSON" in r["error"]
    st, r = _call(live, "POST", "/api/plan", raw=b"[1,2]", headers={"Content-Type": "application/json"})
    assert st == 400 and "object" in r["error"]
    st, r = _call(live, "POST", "/api/plan", raw=b'{"plan":{}}', headers={"Content-Type": "text/plain"})
    assert st == 415
    big = b'{"plan": "' + b"x" * (server.MAX_BODY + 10) + b'"}'
    st, r = _call(live, "POST", "/api/plan", raw=big, headers={"Content-Type": "application/json"})
    assert st == 413
    st, r = _call(live, "POST", "/api/plan", {"plan": {"CLOUDLENS_STACK_NAME": "bad!"}})
    assert st == 400 and r["errors"]
    st, r = _call(live, "POST", "/api/nope", {})
    assert st == 404


def test_api_get_routes_validate_before_running_anything(live, monkeypatch):
    def never(*a, **k):
        raise AssertionError("a refused input reached a subprocess")

    monkeypatch.setattr(api.subprocess, "run", never)
    for path in ("/api/doctor?region=bad", "/api/discover/vpcs?region=bad", "/api/discover/subnets?region=us-east-1",
                 "/api/discover/workloads?region=us-east-1&tag=novalue", "/api/discover/eks"):
        st, r = _call(live, "GET", path)
        assert st == 400 and r["error"], path
    st, r = _call(live, "GET", "/api/licences")
    assert st == 405 and "POST" in r["error"]
    st, r = _call(live, "GET", "/api/nope")
    assert st == 404
    st, r = _call(live, "POST", "/api/answer/nope", {"prompt_id": "p1", "text": "x"})
    assert st == 404
    st, r = _call(live, "GET", "/events/nope")
    assert st == 404
    # a job id the routes could never have minted is a 400 before JOBS is
    # consulted: %0A is the one way a newline reaches a request path
    for path in ("/events/a%0Ab", "/api/stop/a%0Ab", "/stop/a%00b", "/events/a.b"):
        st, r = _call(live, "GET" if path.startswith("/events/") else "POST", path)
        assert st == 400 and "job id" in r["error"], path
    st, r = _call(live, "POST", "/api/answer/a%0Db", {"prompt_id": "p1", "text": "x"})
    assert st == 400 and "job id" in r["error"]
    for bad in ("abc\n", "abc\r", "abc\x00", "", "a/b", "a" * 65, None, 5):
        assert not server.job_id_ok(bad), repr(bad)
    assert server.job_id_ok("sse1") and server.job_id_ok("0123abcd0123") and server.job_id_ok("a-b_c")


def test_licences_route_over_http_with_kvo_license_stubbed(live, monkeypatch):
    kl = FakeKL(licences=[{"activationCode": "AAAA-1111", "product": "KVO-DEVICE", "quantity": 5}],
                ents={"BBBB-2222": [("CloudLens-Credit", 10, 20)]})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    creds = {"kvo": "10.1.2.3", "user": "admin", "password": "hunter2"}
    st, r = _call(live, "POST", "/api/licences", dict(creds, action="list"))
    assert st == 200 and r["count"] == 1 and r["licences"][0]["activationCode"] == "AAAA-1111", r
    assert ("token", "10.1.2.3", "admin", "hunter2", False) in kl.calls
    assert "hunter2" not in json.dumps(r)
    # the action from the path, the body's own inputs validated first
    st, r = _call(live, "POST", "/api/licences/check", dict(creds, codes=["BBBB-2222"]))
    assert st == 200 and r["codes"][0]["valid"] is True and r["codes"][0]["entitlements"][0]["available"] == 10
    n = len(kl.calls)
    st, r = _call(live, "POST", "/api/licences/check", creds)
    assert st == 400 and "codes" in r["error"]
    st, r = _call(live, "POST", "/api/licences", dict(creds, action="nope"))
    assert st == 400 and "action" in r["error"]
    st, r = _call(live, "POST", "/api/licences", dict(creds, action="list", kvo="10.1.2.3\n"))
    assert st == 400 and "kvo" in r["error"]
    assert len(kl.calls) == n, "a refused body never reaches the KVO"
    # a login the KVO refuses is a 401 without the KVO's words or the
    # password; a KVO that cannot be reached is a 502 with its words
    st, r = _call(live, "POST", "/api/licences", dict(creds, action="list", password="wrong"))
    assert st == 401 and r["error"] == "KVO rejected the username or password"
    st, r = _call(live, "POST", "/api/licences", dict(creds, action="list", password="down"))
    assert st == 502 and "login failed" in r["error"] and "down" not in r["error"]
    st, r = _call(live, "GET", "/api/licences/list")
    assert st == 405


def test_discover_route_over_http_with_the_cli_stubbed(live, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        assert argv[:3] == ["aws", "ec2", "describe-vpcs"] and not kw.get("shell")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"Vpcs": [
            {"VpcId": "vpc-0a0a0a0a", "CidrBlock": "10.0.0.0/16", "Tags": [{"Key": "Name", "Value": "prod"}]}]}),
            stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    st, r = _call(live, "GET", "/api/discover/vpcs?region=eu-west-2")
    assert st == 200 and r == [{"id": "vpc-0a0a0a0a", "cidr": "10.0.0.0/16", "name": "prod"}], r
    assert calls[0][-4:] == ["--region", "eu-west-2", "--output", "json"]
    # the query string decodes %0A to a newline: refused before the CLI runs
    st, r = _call(live, "GET", "/api/discover/vpcs?region=eu-west-2%0A")
    assert st == 400 and "region" in r["error"] and len(calls) == 1


def test_events_route_replays_after_last_event_id(live):
    job = O.Job("sse1", "engine-deploy", {})
    for i in range(4):
        job.emit(E.log("line %d" % i))
    job.emit(E.done("engine exited 0"))
    server.JOBS["sse1"] = job
    ids = [e["id"] for e in job.buffer]

    def frames(headers=None):
        st, raw = _call(live, "GET", "/events/sse1", headers=headers)
        assert st == 200
        text = raw.decode("utf-8")
        return [int(m) for m in re.findall(r"^id: (\d+)$", text, re.M)], text

    got, text = frames()
    assert got == ids and text.startswith("retry: ")
    got, text = frames({"Last-Event-ID": str(ids[1])})
    assert got == ids[2:], "only what came after the id the browser last saw"
    assert '"text": "line 0"' not in text and '"text":"line 0"' not in text
    got, _ = frames({"Last-Event-ID": "junk"})
    assert got == ids, "junk is a fresh start, not an error"
    got, _ = frames({"Last-Event-ID": str(ids[-1])})
    assert got == []


def test_every_sse_reader_of_one_job_sees_every_event(live):
    # Reproduced before the fix: two tabs on one job took turns on the
    # job's single queue and saw ids 1,3,5 and 2,4,6. Both readers connect
    # to an empty job and wait, so the events below are live, not
    # replayed; each has to see all of them, in order, and the stream has
    # to end for both once the done is out.
    job = O.Job("fan", "engine-deploy", {})
    server.JOBS["fan"] = job
    got = {}

    def reader(name):
        st, raw = _call(live, "GET", "/events/fan")
        got[name] = (st, [int(m) for m in re.findall(r"^id: (\d+)$", raw.decode("utf-8"), re.M)])

    threads = [threading.Thread(target=reader, args=(n,)) for n in ("a", "b", "c")]
    for t in threads:
        t.start()
    time.sleep(0.3)
    for i in range(6):
        job.emit(E.log("line %d" % i))
        time.sleep(0.01)
    job.emit(E.done("engine exited 0"))
    for t in threads:
        t.join(10)
    assert got == {n: (200, list(range(1, 8))) for n in ("a", "b", "c")}, got
    # a reader that arrives after the end gets the whole buffer and the end at once
    st, raw = _call(live, "GET", "/events/fan")
    assert [int(m) for m in re.findall(r"^id: (\d+)$", raw.decode("utf-8"), re.M)] == list(range(1, 8))
    assert not hasattr(job, "q"), "one buffer, no queue beside it"


def test_run_and_answer_routes_are_wired(live, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    started = []
    monkeypatch.setattr(api, "_start_engine", lambda job, cmd, cwd, env: started.append((job, cmd, env)))
    # a real-length secret: anything under orchestrator.MIN_REDACTION is
    # refused before the launch, because the stream could not blank it
    st, r = _call(live, "POST", "/api/run", {"plan": GOOD, "secrets": {"CLOUDLENS_VC_PASSWORD": "pw-not-real"}})
    assert st == 200 and r["job_id"] in server.JOBS, r
    job, cmd, env = started[0]
    assert env == {"CLOUDLENS_VC_PASSWORD": "pw-not-real"} and "--profile" in cmd
    assert r["profile_file"] == "deploy-profile-demo.env"
    # the stack is held from the registration on (the stubbed starter never
    # reaches a Popen): a second run and a teardown are 409 over the route too
    st, r2 = _call(live, "POST", "/api/run", {"plan": GOOD})
    assert st == 409 and r2["error"] == "stack demo already has a run in progress (job %s)" % job.id
    st, r2 = _call(live, "POST", "/api/teardown", {"stack": "demo", "region": "us-east-1", "confirm_name": "demo"})
    assert st == 409
    job.emit(E.done("engine exited 0"))     # the verdict frees it
    # no prompt is waiting: the answer is refused with the job's own words
    st, r = _call(live, "POST", "/api/answer/" + r["job_id"], {"prompt_id": "p1", "text": "x"})
    assert st == 409 and "No prompt" in r["error"]
    # teardown through the route, stubbed the same way
    monkeypatch.setattr(api, "_start_teardown", lambda job, cmd, cwd, env: started.append((job, cmd, env)))
    st, r = _call(live, "POST", "/api/teardown", {"stack": "demo", "region": "us-east-1", "confirm_name": "no"})
    assert st == 400 and r["error"].startswith("type the stack name")
    st, r = _call(live, "POST", "/api/teardown", {"stack": "demo", "region": "us-east-1", "confirm_name": "demo"})
    assert st == 200 and started[-1][1][-1] == "--yes"
    st, r = _call(live, "POST", "/api/stop/" + r["job_id"])
    assert st == 200 and r["ok"]


# ------------------------------------------------------------- operate
def test_phase_order_is_read_from_the_script_never_copied():
    """The phase list has exactly one home: deploy-stack.sh's own
    PHASE_ORDER. A copy in Python (or in the page) is a list that goes
    stale the first time a phase is added there, and /api/run would then
    refuse a phase the script knows, or accept one it does not."""
    src = open(DEPLOY).read()
    m = re.search(r'^PHASE_ORDER="([^"]+)"$', src, re.M)
    assert m, "deploy-stack.sh declares PHASE_ORDER"
    assert api.phase_order() == m.group(1).split()
    assert "stack" in api.phase_order() and "license" in api.phase_order()


def test_phase_order_of_a_script_that_does_not_say_is_empty(tmp_path, monkeypatch):
    script = tmp_path / "no-phases.sh"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    monkeypatch.setattr(api, "DEPLOY", str(script))
    api.phase_order.cache_clear() if hasattr(api.phase_order, "cache_clear") else None
    assert api.phase_order() == []
    monkeypatch.setattr(api, "DEPLOY", str(tmp_path / "nowhere.sh"))
    assert api.phase_order() == []


def _stack_instances():
    def i(name, kind, ip, state="running", key="lab"):
        return {"InstanceId": "i-" + kind.ljust(8, "0"), "InstanceType": "t3.large", "KeyName": key,
                "State": {"Name": state}, "VpcId": "vpc-0a0a0a0a", "SubnetId": "subnet-01010101",
                "Placement": {"AvailabilityZone": "us-east-1a"},
                "PrivateIpAddress": "10.0.1.5", "PublicIpAddress": ip,
                "Tags": [{"Key": "Name", "Value": name}]}
    return {"Reservations": [{"Instances": [
        i("demo-vcontroller", "vc", "3.1.1.1"),
        i("demo-kvo", "kvo", "3.1.1.2"),
        i("demo-vpb", "vpb", "3.1.1.3"),
        i("demo-old", "old", "3.1.1.9", state="terminated")]}]}


def test_status_carries_a_state_per_field_and_never_invents_a_count(tmp_path, monkeypatch):
    """Every field on the Operate screen says either what it read or why it
    could not read it, with the command that would. Nothing is a guess: a
    field with no answer is `unavailable`, never a zero."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(tmp_path / "not-here.json"))
    monkeypatch.setattr(api.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    # the doctor's own first candidate is this variable: an operator who has
    # it set would otherwise lend this test a real key
    monkeypatch.delenv("CLOUDLENS_KEY_PEM", raising=False)
    calls = []

    def fake_aws(args, region, **kw):
        calls.append(list(args))
        if args[:2] == ["ec2", "describe-instances"]:
            return _stack_instances()
        if args[:2] == ["ec2", "describe-traffic-mirror-sessions"]:
            return {"TrafficMirrorSessions": [{"TrafficMirrorSessionId": "tms-1"},
                                              {"TrafficMirrorSessionId": "tms-2"}]}
        raise AssertionError(args)

    monkeypatch.setattr(api, "_aws", fake_aws)
    # no .pem on this machine: no ssh is tried, by either door
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)
    r = api.status("demo", "us-east-1")
    assert r["stack"] == "demo" and r["region"] == "us-east-1"
    assert r["phases"] == api.phase_order(), "the script's own list, served so nothing copies it"
    assert r["profile"] == {"file": "deploy-profile-demo.env", "present": False}
    # instances: the stack's own Name tags, the terminated one dropped
    rows = r["instances"]["value"]["rows"]
    assert [x["name"] for x in rows] == ["demo-vcontroller", "demo-kvo", "demo-vpb"]
    assert [x["role"] for x in rows] == ["vcontroller", "kvo", "vpb"]
    assert r["instances"]["value"]["count"] == 3
    flt = [c for c in calls if c[:2] == ["ec2", "describe-instances"]][0]
    assert json.loads(flt[flt.index("--filters") + 1]) == [{"Name": "tag:Name", "Values": ["demo-*"]}]
    # sensors: no creds file, so the honest answer is where to look instead
    assert "value" not in r["sensors"]
    assert "credentials" in r["sensors"]["unavailable"]
    assert "3.1.1.1" in r["sensors"]["command"], "the vController it would have asked"
    # mirror: a region-wide count, and it says so
    assert r["mirror"]["value"]["sessions"] == 2 and "region" in r["mirror"]["value"]["scope"]
    # vPB: no key, so the exact command the operator can run
    assert "value" not in r["vpb"]
    assert r["vpb"]["command"] == (
        'ssh -i ~/.ssh/lab.pem -p 9022 admin@3.1.1.3 \'sudo vpb -c "show traffic-rule-packet-counters"\'')
    assert ".pem" in r["vpb"]["unavailable"]


def test_the_profile_reader_takes_export_and_single_quotes_as_the_loader_does(tmp_path):
    """Two divergences from deploy-stack.sh's loader, in opposite
    directions, in the guard that stops a replay running in a region the
    console is not watching.

    `export CLOUDLENS_REGION="us-west-2"` is a line the loader reads (it
    strips the `export ` prefix, one space, before matching) and the
    regex here did not match at all. _profile_says then said nothing about
    the region, the caller only refuses on a value that DISAGREES, and the
    guard failed OPEN: the run went to the profile's region while the
    console held and reported the typed one, and _in_flight, which keys on
    the typed region, would have let the same profile be replayed twice at
    once.

    `CLOUDLENS_REGION='us-west-2'` is the same value in the other kind of
    quotes. The loader strips one matching pair of EITHER kind; the regex
    stripped only double quotes, so the value read back as "'us-west-2'"
    and a request that named us-west-2 was refused over quotes the script
    never sees."""
    p = tmp_path / "profile.env"
    p.write_text('export CLOUDLENS_REGION="us-west-2"\n'
                 "CLOUDLENS_STACK_NAME='demo'\n"
                 'export CLOUDLENS_TAPPING=sensors\n'
                 'CLOUDLENS_INFRA="new\n')
    says = api._profile_says(str(p), ("CLOUDLENS_REGION", "CLOUDLENS_STACK_NAME", "CLOUDLENS_TAPPING",
                                      "CLOUDLENS_INFRA"))
    assert says["CLOUDLENS_REGION"] == "us-west-2", "export is stripped, as ${_pl#export } strips it"
    assert says["CLOUDLENS_STACK_NAME"] == "demo", "one matching pair of quotes, either kind"
    assert says["CLOUDLENS_TAPPING"] == "sensors"
    # an unbalanced quote is part of the value there, so it is part of it
    # here: the loader strips a PAIR
    assert says["CLOUDLENS_INFRA"] == '"new'
    # what the loader itself ignores is still ignored: it strips exactly
    # "export " with one space, and anything else fails its key rule
    for line in ("export\tCLOUDLENS_REGION=eu-west-1", "export  CLOUDLENS_REGION=eu-west-1",
                 "EXPORT CLOUDLENS_REGION=eu-west-1", "# CLOUDLENS_REGION=eu-west-1"):
        one = tmp_path / "one.env"
        one.write_text(line + "\n")
        assert api._profile_says(str(one), ("CLOUDLENS_REGION",))["CLOUDLENS_REGION"] is None, line
    # and the guard that reads it refuses on the value the script would use
    stack_says = tmp_path / "deploy-profile-demo.env"
    stack_says.write_text('export CLOUDLENS_STACK_NAME="demo"\nexport CLOUDLENS_REGION="us-west-2"\n')
    assert api._profile_says(str(stack_says), ("CLOUDLENS_REGION",)) == {"CLOUDLENS_REGION": "us-west-2"}


def test_the_profile_reader_strips_a_bom_as_the_loader_does(tmp_path, monkeypatch):
    """A UTF-8 BOM on the first line was the third way the same guard
    failed OPEN, and the same way round.

    Windows editors write one, and deploy-stack.sh strips it before it
    parses anything (`sed '1s/^\\xEF\\xBB\\xBF//'`, under a comment that
    says which editors). This reader opened the file as plain utf-8, and
    str.strip() does not remove U+FEFF, so a profile whose FIRST line was
    CLOUDLENS_REGION= or CLOUDLENS_STACK_NAME= arrived here as
    "\ufeffCLOUDLENS_REGION=..." and matched nothing at all. The key then
    read as one the profile does not set, the caller refuses only on a
    value that DISAGREES, and the replay ran in the profile's region
    while the console held, and reported, the typed one. The key that
    comes first is the one that is lost, so both are tested first."""
    bom = b"\xef\xbb\xbf"
    want = {"CLOUDLENS_REGION": "us-west-2", "CLOUDLENS_STACK_NAME": "demo"}
    for first, second in (("CLOUDLENS_REGION", "CLOUDLENS_STACK_NAME"),
                          ("CLOUDLENS_STACK_NAME", "CLOUDLENS_REGION")):
        p = tmp_path / ("bom-%s.env" % first.lower())
        p.write_bytes(bom + ('%s="%s"\n%s="%s"\n' % (first, want[first], second, want[second])).encode())
        assert api._profile_says(str(p), tuple(want)) == want, first
    # line for line with the loader: it strips exactly this
    with open(api.DEPLOY, encoding="utf-8", errors="replace") as fh:
        assert r"1s/^\xEF\xBB\xBF//" in fh.read(), "deploy-stack.sh strips the BOM before parsing"

    # and the guard itself, end to end, agreeing and disagreeing
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    started = []
    start = lambda job, cmd, cwd, env: started.append(cmd)
    path = tmp_path / "deploy-profile-demo.env"
    path.write_bytes(bom + b'CLOUDLENS_REGION="us-west-2"\nCLOUDLENS_STACK_NAME="demo"\n')
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert r["http"] == 400 and "us-west-2" in r["error"] and "us-east-1" in r["error"], r
    assert not started, "a BOM is not permission to run in another region"
    path.write_bytes(bom + b'CLOUDLENS_STACK_NAME="other"\nCLOUDLENS_REGION="us-east-1"\n')
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert r["http"] == 400 and "CLOUDLENS_STACK_NAME" in r["error"] and "other" in r["error"], r
    assert not started
    path.write_bytes(bom + b'CLOUDLENS_REGION="us-east-1"\nCLOUDLENS_STACK_NAME="demo"\n')
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert not r.get("error") and not r.get("errors"), r
    assert started and started[-1] == ["bash", api.DEPLOY, "--profile", str(path), "--resume"]


def test_status_names_the_kvo_by_role_over_every_instance_not_only_the_rows(tmp_path, monkeypatch):
    """`rows` is the first MAX_ROWS instances; `by_role` is computed over
    all of them, and travels with the answer.

    teardown.js asked "does this stack have a KVO, and at what address" of
    the rows. A stack with more than 50 live instances whose KVO sorted
    past the cut answered no KVO, and no KVO is the ONE value that draws
    no licence banner at all: the teardown armed silently over an
    appliance still holding its counts. The side that has seen every row
    is this one."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(tmp_path / "not-here.json"))
    monkeypatch.setattr(api.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    monkeypatch.delenv("CLOUDLENS_KEY_PEM", raising=False)
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)

    def crowd(args, region, **kw):
        if args[:2] == ["ec2", "describe-instances"]:
            rows = [{"InstanceId": "i-w%03d" % n, "InstanceType": "t3.small", "State": {"Name": "running"},
                     "PrivateIpAddress": "10.0.0.%d" % (n % 250),
                     "Tags": [{"Key": "Name", "Value": "demo-workload-%03d" % n}]}
                    for n in range(api.MAX_ROWS + 10)]
            # the KVO past the cut, which is the whole case
            rows.append({"InstanceId": "i-kvo", "InstanceType": "t3.xlarge", "State": {"Name": "running"},
                         "PublicIpAddress": "3.1.1.9",
                         "Tags": [{"Key": "Name", "Value": "demo-kvo"}]})
            return {"Reservations": [{"Instances": rows}]}
        return {"TrafficMirrorSessions": []}

    monkeypatch.setattr(api, "_aws", crowd)
    r = api.status("demo", "us-east-1")
    cell = r["instances"]["value"]
    assert cell["count"] == api.MAX_ROWS + 11 and len(cell["rows"]) == api.MAX_ROWS
    assert cell["truncated"] is True
    assert not [x for x in cell["rows"] if x["role"] == "kvo"], (
        "the case: the KVO is outside the rows the answer carries")
    assert cell["by_role"]["kvo"]["id"] == "i-kvo" and cell["by_role"]["kvo"]["public_ip"] == "3.1.1.9"
    assert "hunter" not in json.dumps(cell)


def test_status_says_what_the_cli_said_when_it_could_not_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(tmp_path / "not-here.json"))

    def broken(args, region, **kw):
        raise api.AwsError("An error occurred (UnauthorizedOperation)")

    monkeypatch.setattr(api, "_aws", broken)
    r = api.status("demo", "us-east-1")
    assert "UnauthorizedOperation" in r["instances"]["unavailable"]
    assert r["instances"]["command"].startswith("aws ec2 describe-instances")
    assert "UnauthorizedOperation" in r["mirror"]["unavailable"]
    # with no instance list there is no vController and no vPB to ask about
    assert "unavailable" in r["sensors"] and "unavailable" in r["vpb"]
    assert r["phases"] == api.phase_order(), "the phase list does not need AWS"


def test_status_validates_before_it_reads_anything(monkeypatch):
    monkeypatch.setattr(api, "_aws", _never)
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)
    assert "stack" in api.status("bad name", "us-east-1")["error"]
    assert "stack" in api.status("", "us-east-1")["error"]
    assert "region" in api.status("demo", "nowhere")["error"]
    assert "stack" in api.status("demo\n", "us-east-1")["error"]


def test_status_reads_the_sensor_count_through_the_creds_file(tmp_path, monkeypatch):
    """The one credential this GET may use is the file the CLI already
    wrote (mode 600), never a query parameter: a password in a URL is in
    the browser's history, the referrer and every log on the way."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"url": "https://3.1.1.1/cloudlens/login", "username": "admin",
                                 "password": "s3cr3t", "project": "autopilot"}))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(creds))
    monkeypatch.setattr(api, "_aws", lambda args, region, **kw:
                        _stack_instances() if args[1] == "describe-instances" else {"TrafficMirrorSessions": []})
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)
    seen = []

    def fake_vc(method, url, token=None, body=None, timeout=None):
        seen.append((method, url, token, body))
        if url.endswith("/identity/login"):
            return 200, {"ActiveSessionCredentials": {"JwtToken": "JWT"}, "Accounts": {"OwningAccount": {"id": "a1"}}}
        return 200, [{"name": "autopilot", "id": "p1", "agentCount": 3}]

    monkeypatch.setattr(api, "_vc_call", fake_vc)
    r = api.status("demo", "us-east-1")
    assert r["sensors"]["value"] == {"sensors": 3, "project": "autopilot", "vcontroller": "3.1.1.1"}
    assert seen[0][0] == "POST" and seen[0][1] == "https://3.1.1.1/cloudlens/api/v1/identity/login"
    assert seen[0][3] == {"Email": "admin", "Password": "s3cr3t"}, "the identity API's own field names"
    assert seen[1][:3] == ("GET", "https://3.1.1.1/cloudlens/api/v1/mgmt/accounts/a1/projects", "JWT")
    assert "s3cr3t" not in json.dumps(r), "the file's password reaches the vController and nothing else"

    # a payload with no count is said so, never counted as zero
    monkeypatch.setattr(api, "_vc_call", lambda m, u, token=None, body=None, timeout=None:
                        (200, {"ActiveSessionCredentials": {"JwtToken": "JWT"}, "Accounts": {"OwningAccount": {"id": "a1"}}})
                        if u.endswith("/identity/login") else (200, [{"name": "autopilot"}]))
    r = api.status("demo", "us-east-1")
    assert "value" not in r["sensors"] and "no sensor count" in r["sensors"]["unavailable"]

    # a login the vController refuses says the code and never the password
    monkeypatch.setattr(api, "_vc_call", lambda m, u, token=None, body=None, timeout=None: (401, {"error": "nope"}))
    r = api.status("demo", "us-east-1")
    assert "401" in r["sensors"]["unavailable"] and "s3cr3t" not in json.dumps(r)

    # a vController that did not answer at all: its own words, with the
    # file's password scrubbed out of them, and still no count
    monkeypatch.setattr(api, "_vc_call", lambda m, u, token=None, body=None, timeout=None:
                        (0, "URLError: <urlopen error [Errno 61] Connection refused> s3cr3t"))
    r = api.status("demo", "us-east-1")
    assert "could not be reached" in r["sensors"]["unavailable"] and "Connection refused" in r["sensors"]["unavailable"]
    assert "s3cr3t" not in json.dumps(r) and "***" in r["sensors"]["unavailable"]

    # a creds file for another vController is not this stack's
    monkeypatch.setattr(api, "_vc_call", lambda m, u, token=None, body=None, timeout=None: (200, {}))
    creds.write_text(json.dumps({"url": "https://9.9.9.9/cloudlens/login", "username": "admin", "password": "s3cr3t"}))
    r = api.status("demo", "us-east-1")
    assert "another vController" in r["sensors"]["unavailable"] and "9.9.9.9" in r["sensors"]["unavailable"]


def test_a_creds_file_whose_host_only_looks_like_this_stacks_is_refused(tmp_path, monkeypatch):
    """The host is compared EXACTLY, never as a substring.

    `vc_ip not in creds["url"]` passed a file for https://3.1.1.10/... on a
    stack whose vController is 3.1.1.1, so that file's password would have
    been POSTed to https://3.1.1.1/... and the OTHER box's sensor count
    reported as this stack's. Consecutive elastic IPs make that exact pair
    ordinary. A url this cannot resolve to a host is refused the same way:
    nothing is guessed at and no password leaves the machine."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    creds = tmp_path / "creds.json"
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(creds))
    monkeypatch.setattr(api, "_aws", lambda args, region, **kw:
                        _stack_instances() if args[1] == "describe-instances" else {"TrafficMirrorSessions": []})
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)

    def no_call(*a, **k):
        raise AssertionError("the saved password was sent to a vController that is not this stack's")

    monkeypatch.setattr(api, "_vc_call", no_call)
    creds.write_text(json.dumps({"url": "https://3.1.1.10/cloudlens/login", "username": "admin",
                                 "password": "s3cr3t", "project": "autopilot"}))
    r = api.status("demo", "us-east-1")      # this stack's vController is 3.1.1.1
    cell = r["sensors"]
    assert "value" not in cell, cell
    assert "another vController" in cell["unavailable"], cell
    assert "3.1.1.10" in cell["unavailable"] and "3.1.1.1" in cell["unavailable"]
    assert cell["command"].startswith("open https://3.1.1.1/cloudlens/login"), cell["command"]
    assert "s3cr3t" not in json.dumps(r), "a file that is not this stack's leaks nothing of itself"

    # a url with no host to compare is refused too, not guessed at: no
    # scheme, nothing at all, no authority, not a url, a broken literal
    for url in ("3.1.1.1/cloudlens/login", "   ", "https://", "not a url", "https://[oops/x"):
        creds.write_text(json.dumps({"url": url, "username": "admin", "password": "s3cr3t"}))
        cell = api.status("demo", "us-east-1")["sensors"]
        assert "value" not in cell, url
        assert cell["unavailable"] and cell["command"], url
        assert "s3cr3t" not in json.dumps(cell), url


def test_status_runs_the_vpb_counters_over_ssh_when_the_key_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(tmp_path / "not-here.json"))
    pem = tmp_path / ".ssh" / "lab.pem"
    pem.parent.mkdir()
    pem.write_text("-----BEGIN-----\n")
    monkeypatch.setattr(api.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    monkeypatch.delenv("CLOUDLENS_KEY_PEM", raising=False)
    monkeypatch.setattr(api, "_aws", lambda args, region, **kw:
                        _stack_instances() if args[1] == "describe-instances" else {"TrafficMirrorSessions": []})
    ran = []
    monkeypatch.setattr(api.subprocess, "Popen", FakeSsh(ran, out="rule-1  packets 145037\n"))
    r = api.status("demo", "us-east-1")
    assert r["vpb"]["value"]["text"] == "rule-1  packets 145037"
    argv, kw = ran[0]
    assert argv[0] == "ssh" and "-i" in argv and str(pem) in argv
    assert "admin@3.1.1.3" in argv and "9022" in argv
    assert argv[-1] == 'sudo vpb -c "show traffic-rule-packet-counters"'
    assert "-o" in argv and "BatchMode=yes" in argv, "no password prompt on a console's GET"
    # the doctor's own shape: its own session, so a timeout can end the
    # whole group and not the leader alone
    assert kw.get("start_new_session") is True and kw.get("stdin") == subprocess.DEVNULL
    # an ssh that fails says so with its own last line, and never a count
    monkeypatch.setattr(api.subprocess, "Popen", FakeSsh(
        ran, rc=255, err="ssh: connect to host 3.1.1.3 port 9022: Operation timed out\n"))
    r = api.status("demo", "us-east-1")
    assert "value" not in r["vpb"] and "timed out" in r["vpb"]["unavailable"]
    assert r["vpb"]["command"].startswith("ssh -i ")
    # an ssh that says nothing is ended as a GROUP, the way the doctor ends
    # one: a leader-only kill leaves whatever it started holding this pipe
    killed = []
    monkeypatch.setattr(api, "_kill_group", lambda proc: killed.append(proc.pid))
    monkeypatch.setattr(api.subprocess, "Popen", FakeSsh(ran, hang=True))
    r = api.status("demo", "us-east-1")
    assert killed == [4343], "the timeout killed the leader alone"
    assert "value" not in r["vpb"] and "did not answer within" in r["vpb"]["unavailable"]


# -------------------------------------------------------- verify-empty
def test_verify_empty_counts_what_a_teardown_should_have_left(monkeypatch):
    canned = {
        ("ec2", "describe-instances"): {"Reservations": [{"Instances": [
            {"State": {"Name": "running"}}, {"State": {"Name": "terminated"}}]}]},
        ("ec2", "describe-vpcs"): {"Vpcs": [{"VpcId": "vpc-1", "IsDefault": True},
                                            {"VpcId": "vpc-2", "IsDefault": False}]},
        ("ec2", "describe-volumes"): {"Volumes": [{"VolumeId": "vol-1", "State": "available", "Size": 100},
                                                  {"VolumeId": "vol-2", "State": "in-use", "Size": 8}]},
        ("ec2", "describe-network-interfaces"): {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}]},
        ("ec2", "describe-traffic-mirror-sessions"): {"TrafficMirrorSessions": []},
        ("cloudformation", "list-stacks"): {"StackSummaries": [{"StackName": "demo", "StackStatus": "CREATE_COMPLETE"}]},
    }

    def fake_aws(args, region, **kw):
        return canned[tuple(args[:2])]

    monkeypatch.setattr(api, "_aws", fake_aws)
    r = api.verify_empty("us-east-1")
    assert r["region"] == "us-east-1"
    assert r["instances"]["value"] == 1, "terminated instances are not left behind"
    assert r["vpcs"]["value"] == 1, "the default VPC is not something a teardown removes"
    assert r["volumes"]["value"] == 2 and r["volumes"]["detail"]["available"] == 1
    assert r["enis"]["value"] == 1 and r["mirror_sessions"]["value"] == 0
    assert r["stacks"]["value"] == 1
    assert r["empty"] is False
    # everything at zero is empty; a probe that could not answer makes it unknown
    for key in canned:
        canned[key] = {"Reservations": [], "Vpcs": [{"IsDefault": True}], "Volumes": [], "NetworkInterfaces": [],
                       "TrafficMirrorSessions": [], "StackSummaries": []}
    assert api.verify_empty("us-east-1")["empty"] is True

    def half(args, region, **kw):
        if args[:2] == ["ec2", "describe-volumes"]:
            raise api.AwsError("AccessDenied")
        return canned[tuple(args[:2])]

    monkeypatch.setattr(api, "_aws", half)
    r = api.verify_empty("us-east-1")
    assert r["empty"] is None, "a region cannot be called empty on a probe that did not answer"
    assert "AccessDenied" in r["volumes"]["unavailable"]
    monkeypatch.setattr(api, "_aws", _never)
    assert "region" in api.verify_empty("nowhere")["error"]


# ----------------------------------------------- resume and re-run a phase
def _profile_for(tmp_path, stack="demo"):
    p = tmp_path / ("deploy-profile-%s.env" % stack)
    p.write_text('CLOUDLENS_STACK_NAME="%s"\nCLOUDLENS_REGION="us-east-1"\n' % stack)
    return p


def test_a_resume_replays_the_profile_the_wizard_wrote(tmp_path, monkeypatch):
    """Resume is the CLI's own resume on the profile file that already
    exists: no plan is posted, nothing is written, and the stack cannot be
    named into a file outside the repo."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    path = _profile_for(tmp_path)
    started = []
    jobs = {}
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs=jobs,
                start=lambda job, cmd, cwd, env: started.append((job, cmd, cwd, env)))
    assert not r.get("errors") and not r.get("error"), r
    job, cmd, cwd, env = started[-1]
    assert cmd == ["bash", api.DEPLOY, "--profile", str(path), "--resume"]
    assert "--only" not in cmd and cwd == str(tmp_path) and env is None
    assert job.flow_id == "engine-deploy" and jobs[job.id] is job
    assert r == {"job_id": job.id, "profile_file": "deploy-profile-demo.env", "stack": "demo",
                 "region": "us-east-1", "only": None}
    assert job.buffer[0]["type"] == E.NARRATE and "--resume" in job.buffer[0]["text"]


def test_a_re_run_names_one_phase_the_script_knows(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    _profile_for(tmp_path)
    started = []
    jobs = {}
    start = lambda job, cmd, cwd, env: started.append((job, cmd, cwd, env))
    r = api.run({"stack": "demo", "region": "us-east-1", "only": "license"}, jobs=jobs, start=start)
    assert r["only"] == "license"
    assert started[-1][1][-2:] == ["--only", "license"]
    assert "--resume" in started[-1][1], "the engine makes the script interactive: the replay is not re-asked"
    started[-1][0].emit(E.done("engine exited 0"))
    # a phase the script does not have is refused, and the answer names the list
    n = len(started)
    r = api.run({"stack": "demo", "region": "us-east-1", "only": "wibble"}, jobs=jobs, start=start)
    assert "wibble" not in r["error"] or "phase" in r["error"]
    assert "license" in r["error"], "the refusal names the phases the script does have"
    for bad in ("", "license extra", "license\n", "--events", 5, None):
        r = api.run({"stack": "demo", "region": "us-east-1", "only": bad}, jobs=jobs, start=start)
        assert r.get("error") or r.get("errors"), bad
    assert len(started) == n, "a refused phase starts nothing"


def test_a_re_run_is_refused_for_every_phase_when_the_script_names_none(tmp_path, monkeypatch):
    """phase_order() is [] for a script with no PHASE_ORDER line, and for
    one that is not there at all. The vocabulary belongs to the script, so
    with no vocabulary there is no phase a re-run may name: EVERY --only is
    refused, the refusal says the script names none rather than printing an
    empty list, and nothing starts. A resume names no phase, so it is still
    allowed: that is the script's own resume, not this console's list."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    _profile_for(tmp_path)
    script = tmp_path / "no-phases.sh"
    script.write_text("#!/usr/bin/env bash\necho hi\n")
    started = []
    start = lambda job, cmd, cwd, env: started.append(cmd)
    for deploy in (str(script), str(tmp_path / "nowhere.sh")):
        monkeypatch.setattr(api, "DEPLOY", deploy)
        assert api.phase_order() == []
        for phase in ("license", "stack", "sensors"):
            r = api.run({"stack": "demo", "region": "us-east-1", "only": phase}, jobs={}, start=start)
            assert "the script names none" in r["error"], (deploy, phase, r)
        assert not started, "a refused phase starts nothing"
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert not r.get("error") and not r.get("errors"), r
    assert started and "--only" not in started[-1], started


def test_a_replay_without_a_profile_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    started = []
    start = lambda *a: started.append(a)
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert "deploy-profile-demo.env" in r["error"] and "Deploy" in r["error"]
    assert r["http"] == 400
    assert not os.listdir(str(tmp_path)), "a refused replay writes no profile"
    assert not started
    # and the stack name still has to be one the script would accept
    assert api.run({"stack": "../etc", "region": "us-east-1"}, jobs={}, start=start).get("error")
    assert api.run({"stack": "demo", "region": "nowhere"}, jobs={}, start=start).get("error")
    assert not started


def test_a_replay_shares_the_one_engine_per_stack_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    _profile_for(tmp_path)
    jobs = {}
    started = []
    start = lambda job, cmd, cwd, env: started.append(job)
    first = api.run({"stack": "demo", "region": "us-east-1"}, jobs=jobs, start=start)
    r = api.run({"stack": "demo", "region": "us-east-1", "only": "license"}, jobs=jobs, start=start)
    assert r["http"] == 409 and first["job_id"] in r["error"]
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "demo"}, jobs=jobs, start=start)
    assert r["http"] == 409
    r = api.run({"plan": GOOD}, jobs=jobs, start=start)
    assert r["http"] == 409
    started[0].emit(E.done("engine exited 0"))
    assert not api.run({"stack": "demo", "region": "us-east-1"}, jobs=jobs, start=start).get("error")


def test_a_replay_takes_the_same_secrets_and_codes_as_a_launch(tmp_path, monkeypatch):
    """A re-run of the licence phase needs the codes, and a resume needs the
    passwords the run would otherwise ask for. They travel exactly as they
    do on a launch: codes on the argv, secrets in the environment, both
    registered with the job so the stream redacts them."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    _profile_for(tmp_path)
    started = []
    jobs = {}
    r = api.run({"stack": "demo", "region": "us-east-1", "only": "license",
                 "secrets": {"CLOUDLENS_KVO_ADMIN_PASS": "hunter2"},
                 "kvo_codes": ["AAAA-BBBB-CCCC-DDDD,5"]}, jobs=jobs,
                start=lambda job, cmd, cwd, env: started.append((job, cmd, env)))
    job, cmd, env = started[-1]
    assert env == {"CLOUDLENS_KVO_ADMIN_PASS": "hunter2"}
    assert cmd[-4:] == ["--kvo-codes", "AAAA-BBBB-CCCC-DDDD,5", "--only", "license"]
    assert sorted(job.redactions) == ["AAAA-BBBB-CCCC-DDDD", "hunter2"]
    everywhere = json.dumps(job.inputs) + json.dumps(job.buffer) + json.dumps(r)
    for leak in ("hunter2", "AAAA-BBBB"):
        assert leak not in everywhere, leak
    n = len(started)
    r = api.run({"stack": "demo", "region": "us-east-1", "secrets": {"PATH": "/x"}}, jobs=jobs, start=_never)
    assert r["errors"] and len(started) == n


def test_the_status_and_verify_routes_are_guarded_gets(live, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "VC_CREDS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(api, "_aws", lambda args, region, **kw:
                        _stack_instances() if args[1] == "describe-instances" else {"TrafficMirrorSessions": []})
    monkeypatch.setattr(api.subprocess, "run", _never)
    monkeypatch.setattr(api.subprocess, "Popen", _never)
    st, r = _call(live, "GET", "/api/status?stack=demo&region=us-east-1")
    assert st == 200 and r["instances"]["value"]["count"] == 3
    st, r = _call(live, "GET", "/api/status?stack=bad!&region=us-east-1")
    assert st == 400 and "stack" in r["error"]
    st, r = _call(live, "GET", "/api/status?region=us-east-1")
    assert st == 400
    # the guards every other /api/ GET has
    st, r = _call(live, "GET", "/api/status?stack=demo&region=us-east-1", headers={"Host": "evil.example"})
    assert st == 403 and "Host" in r["error"]
    st, r = _call(live, "GET", "/api/status?stack=demo&region=us-east-1", headers={"Sec-Fetch-Site": "cross-site"})
    assert st == 403
    monkeypatch.setattr(api, "_aws", lambda args, region, **kw: {
        "Reservations": [], "Vpcs": [], "Volumes": [], "NetworkInterfaces": [],
        "TrafficMirrorSessions": [], "StackSummaries": []})
    st, r = _call(live, "GET", "/api/verify-empty?region=us-east-1")
    assert st == 200 and r["empty"] is True
    st, r = _call(live, "GET", "/api/verify-empty?region=nowhere")
    assert st == 400 and "region" in r["error"]
    st, r = _call(live, "GET", "/api/verify-empty?region=us-east-1", headers={"Sec-Fetch-Site": "same-site"})
    assert st == 403


def test_the_replay_route_is_wired(live, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    _profile_for(tmp_path, "demo2")
    started = []
    monkeypatch.setattr(api, "_start_engine", lambda job, cmd, cwd, env: started.append(cmd))
    st, r = _call(live, "POST", "/api/run", {"stack": "demo2", "region": "us-east-1", "only": "sensors"})
    assert st == 200 and r["only"] == "sensors", r
    assert started[-1][-2:] == ["--only", "sensors"]
    server.JOBS[r["job_id"]].emit(E.done("engine exited 0"))
    st, r = _call(live, "POST", "/api/run", {"stack": "nosuch", "region": "us-east-1"})
    assert st == 400 and "deploy-profile-nosuch.env" in r["error"]


def test_a_replay_is_refused_when_the_profile_is_for_another_stack_or_region(tmp_path, monkeypatch):
    """--profile is the whole argv of a replay: no --region, no
    --stack-name, so the FILE decides where the run happens.

    The console asked for a stack and a region, read the status for them,
    and then started a run that took its region from the profile instead.
    A profile written for us-west-2 replayed under a typed us-east-1 ran
    in us-west-2 while the Operate screen, the job's inputs and the
    operator all said us-east-1. Worse, _in_flight keys on the TYPED
    region, so the same profile could be replayed twice at once by typing
    two different regions, which is the one thing the one-engine-per-stack
    guard exists to stop.

    Both keys are read out of the profile and both are refused when they
    disagree, naming the file's value and the request's.
    """
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    started = []
    start = lambda job, cmd, cwd, env: started.append(cmd)
    path = tmp_path / "deploy-profile-demo.env"

    # the region disagrees
    path.write_text('CLOUDLENS_STACK_NAME="demo"\nCLOUDLENS_REGION="us-west-2"\n')
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert r["http"] == 400, r
    assert "CLOUDLENS_REGION" in r["error"] and "us-west-2" in r["error"] and "us-east-1" in r["error"], r
    assert "deploy-profile-demo.env" in r["error"]
    assert not started, "a profile for another region starts nothing"

    # the stack name disagrees: the file is deploy-profile-demo.env and
    # names another stack, which is a hand-edited or copied file
    path.write_text('CLOUDLENS_STACK_NAME="other"\nCLOUDLENS_REGION="us-east-1"\n')
    r = api.run({"stack": "demo", "region": "us-east-1", "only": "license"}, jobs={}, start=start)
    assert r["http"] == 400 and "CLOUDLENS_STACK_NAME" in r["error"], r
    assert "other" in r["error"] and "demo" in r["error"], r
    assert not started

    # and the agreeing case still runs, quotes stripped the way the
    # script's own loader strips them, comments and blank lines skipped
    path.write_text("# written by the console\n\nCLOUDLENS_STACK_NAME=\"demo\"\n"
                    'CLOUDLENS_REGION="us-east-1"\nCLOUDLENS_DEPLOY_KVO="true"\n')
    r = api.run({"stack": "demo", "region": "us-east-1"}, jobs={}, start=start)
    assert not r.get("error") and not r.get("errors"), r
    assert started and started[-1] == ["bash", api.DEPLOY, "--profile", str(path), "--resume"]

    # a profile that says nothing about either key is not a disagreement:
    # the script applies its own defaults and this console does not invent
    # a refusal it cannot justify
    started[:] = []
    path.write_text('CLOUDLENS_DEPLOY_KVO="true"\n')
    r = api.run({"stack": "demo", "region": "eu-west-1"}, jobs={}, start=start)
    assert not r.get("error") and not r.get("errors"), r
    assert started


def test_the_profile_reader_reads_what_the_scripts_loader_reads(tmp_path):
    """One pair of outer quotes stripped and nothing unescaped, which is
    what deploy-stack.sh's loader does and what profile.render() writes
    for. The last assignment wins, as it would in a shell."""
    p = tmp_path / "profile.env"
    p.write_text('# a comment\n\nCLOUDLENS_REGION="us-east-1"\nCLOUDLENS_STACK_NAME=demo\n'
                 'CLOUDLENS_REGION="eu-west-1"\nNOT_A_PROFILE_KEY="x"\n')
    says = api._profile_says(str(p), ("CLOUDLENS_REGION", "CLOUDLENS_STACK_NAME", "CLOUDLENS_ABSENT"))
    assert says == {"CLOUDLENS_REGION": "eu-west-1", "CLOUDLENS_STACK_NAME": "demo",
                    "CLOUDLENS_ABSENT": None}
    # a file that cannot be read says nothing about any key, which is not
    # the same as saying the key is absent
    assert api._profile_says(str(tmp_path / "nowhere.env"), ("CLOUDLENS_REGION",)) == {"CLOUDLENS_REGION": None}


class _TimedKL(FakeKL):
    """FakeKL that records the timeout each poll was given, and lets the
    poll itself take time on a clock the test holds."""
    def __init__(self, clock, per_poll=0.0, **kw):
        FakeKL.__init__(self, **kw)
        self.clock, self.per_poll, self.timeouts = clock, per_poll, []

    def poll_op(self, kvo, tok, first, verify, want_result=True, timeout=120, label=None):
        self.timeouts.append(timeout)
        self.clock[0] += self.per_poll
        return FakeKL.poll_op(self, kvo, tok, first, verify, want_result, timeout, label)


def test_one_licensing_call_is_bounded_in_rows_and_in_time(monkeypatch):
    """A licensing request POSTs one operation per row and polls each to
    its end, one after another, inside the one request the browser is
    holding open.

    kvo_license.py's poll_op defaults to 120 seconds PER poll, and nothing
    here passed a timeout at all, so a 50-row body (MAX_LIST) was a single
    synchronous request that could run for an hour and a half while the
    page showed one static word. The budget is for the WHOLE request and
    is shared out across the rows, so the last row cannot start a fresh
    120 seconds; and the row count is capped, refused before anything
    reaches the KVO.
    """
    clock = [1000.0]
    monkeypatch.setattr(api, "_now", lambda: clock[0])
    kl = _TimedKL(clock, per_poll=100.0,
                  licences=[{"activationCode": "AAAA-1111", "quantity": 5}])
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    rows = [{"activationCode": "AAAA-%04d" % n, "quantity": 1} for n in range(3)]
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release", "rows": rows})
    assert len(r["results"]) == 3
    # 180 for the first, what is left for the second, and never below 1:
    # one budget, spent, and not three fresh ones
    assert kl.timeouts == [api.OP_BUDGET, api.OP_BUDGET - 100, 1], kl.timeouts
    assert sum(1 for t in kl.timeouts if t > api.OP_BUDGET) == 0

    # more rows than one call may take: refused, naming the limit, before
    # the KVO is touched at all
    n = len(kl.calls)
    many = [{"activationCode": "AAAA-%04d" % i, "quantity": 1} for i in range(api.MAX_OPS + 1)]
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release", "rows": many})
    assert r.get("error") and str(api.MAX_OPS) in r["error"] and str(len(many)) in r["error"], r
    codes = ["AAAA-%04d,1" % i for i in range(api.MAX_OPS + 1)]
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "activate", "codes": codes})
    assert r.get("error") and str(api.MAX_OPS) in r["error"], r
    assert len(kl.calls) == n, "a body over the limit never reaches the KVO, not even to log in"
    # exactly the limit is allowed
    ok = [{"activationCode": "AAAA-%04d" % i, "quantity": 1} for i in range(api.MAX_OPS)]
    r = api.licences({"kvo": "10.1.2.3", "password": "pw", "action": "release", "rows": ok})
    assert len(r["results"]) == api.MAX_OPS
