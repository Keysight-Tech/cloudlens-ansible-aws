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
             environment only, and registers the job
  teardown   the typed-name gate, the exact flags teardown-stack.sh parses,
             the licence-loss flag only when the body says licences were
             released, --orphans for the read-only audit
  licences   kvo_license.py's own functions; the password reaches the KVO
             and nothing else
  server     Last-Event-ID replay from the job buffer, ids stamped by emit in
             buffer order, the Host guard on POST, body caps and JSON errors

Run:  cd console && python3 -m pytest tests/test_api.py -q
"""
import http.client
import json
import os
import re
import stat
import subprocess
import threading
import time

import pytest

from cloudlens_console import api, events as E, orchestrator as O, profile as P, server

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEPLOY = os.path.join(REPO, "deploy", "deploy-stack.sh")
TEARDOWN = os.path.join(REPO, "deploy", "teardown-stack.sh")
GOOD = {"CLOUDLENS_STACK_NAME": "demo", "CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_TAPPING": "sensors"}


# ------------------------------------------------------------------ plan
def test_plan_rejects_bad_stack_name_and_renders_profile(monkeypatch):
    from cloudlens_console import api
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
        {"stack": "abc" + t, "region": "us-east-1", "confirm_name": "abc" + t}, start=_never)),
    ("confirm_name", lambda t: "error" in api.teardown(
        {"stack": "abc", "region": "us-east-1", "confirm_name": "abc" + t}, start=_never)),
    ("region (discover)", lambda t: "error" in api.discover_vpcs("us-east-1" + t)),
    ("region (doctor)", lambda t: "error" in api.doctor("us-east-1" + t)),
    ("region (teardown)", lambda t: "error" in api.teardown(
        {"stack": "abc", "region": "us-east-1" + t, "confirm_name": "abc"}, start=_never)),
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
    monkeypatch.setattr(api, "_aws", _never)
    monkeypatch.setattr(api, "_kvo_license", _never)
    assert rejects(tail), "%s accepted %r" % (name, "abc" + tail)
    assert rejects(tail * 2)


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
def test_doctor_runs_the_script_and_reads_its_check_events(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        ev = argv[argv.index("--events") + 1]
        with open(ev, "a") as fh:
            fh.write('{"seq":1,"ts":"t","type":"check","item":"AWS CLI 2.15","status":"pass"}\n')
            fh.write('{"seq":2,"ts":"t","type":"check","item":"No creds","status":"fail","fix":"aws sso login"}\n')
            fh.write('{"seq":3,"ts":"t","type":"check","item":"Bucket","status":"warn","fix":"allow https"}\n')
            fh.write('{"seq":4,"ts":"t","type":"done","status":"failed","mode":"doctor"}\n')
        return subprocess.CompletedProcess(argv, 1, stdout="[FAIL] No creds\n", stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    r = api.doctor("eu-west-2")
    argv = seen["argv"]
    assert argv[:2] == ["bash", api.DEPLOY] and "--doctor" in argv
    assert argv[argv.index("--region") + 1] == "eu-west-2"
    assert "--prompt-pipe" not in argv, "the doctor asks nothing; a pipe would block it"
    assert seen["kw"]["timeout"] == api.DOCTOR_TIMEOUT and seen["kw"]["stdin"] is subprocess.DEVNULL
    assert seen["kw"]["start_new_session"], "no controlling terminal: the script would re-attach /dev/tty"
    assert r["checks"] == [
        {"item": "AWS CLI 2.15", "status": "pass", "fix": ""},
        {"item": "No creds", "status": "fail", "fix": "aws sso login"},
        {"item": "Bucket", "status": "warn", "fix": "allow https"}]
    assert r["ok"] is False and r["exit"] == 1
    assert not os.path.exists(argv[argv.index("--events") + 1]), "the events file is cleaned up"

    def fake_ok(argv, **kw):
        with open(argv[argv.index("--events") + 1], "a") as fh:
            fh.write('{"seq":1,"ts":"t","type":"check","item":"x","status":"pass"}\n')
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_ok)
    assert api.doctor("us-east-1")["ok"] is True

    def hangs(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(api.subprocess, "run", hangs)
    r = api.doctor("us-east-1")
    assert r["http"] == 504 and "timed out" in r["error"]

    def silent(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bash: syntax error")

    monkeypatch.setattr(api.subprocess, "run", silent)
    r = api.doctor("us-east-1")
    assert r["http"] == 502 and "syntax error" in r["error"]
    assert "region" in api.doctor("nope")["error"]


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
    assert started["cwd"] == str(tmp_path) and r["profile_file"] == path
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


def test_run_refuses_secrets_it_does_not_know_and_bad_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    calls = []
    start = lambda *a, **k: calls.append(a)
    # a profile key is never a secret (it would bypass the file), and an
    # unknown name is never passed through to the engine's environment
    for bad in ({"CLOUDLENS_REGION": "us-east-1"}, {"PATH": "/x"}, {"AWS_SECRET_ACCESS_KEY": "x"},
                {"CLOUDLENS_MIRROR_SECRET_KEY": 5}, {"CLOUDLENS_MIRROR_SECRET_KEY": "a\x00b"}):
        r = api.run({"plan": GOOD, "secrets": bad}, jobs={}, start=start)
        assert r.get("errors"), bad
    r = api.run({"plan": GOOD, "secrets": "nope"}, jobs={}, start=start)
    assert r["errors"]
    r = api.run({"plan": GOOD, "kvo_codes": ["has space"]}, jobs={}, start=start)
    assert any("kvo_codes" in e for e in r["errors"]), r
    r = api.run({"plan": dict(GOOD, CLOUDLENS_STACK_NAME="../etc")}, jobs={}, start=start)
    assert r["errors"]
    assert api.run("x", jobs={}, start=start)["errors"]
    assert not calls and not os.listdir(str(tmp_path)), "nothing written, nothing started"


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


# -------------------------------------------------------------- teardown
def test_teardown_requires_typed_name():
    from cloudlens_console import api
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "nope"}, start=lambda *a, **k: "j")
    assert r["error"].startswith("type the stack name")


def _teardown_flags():
    """The flags teardown-stack.sh's own case statement accepts."""
    src = open(TEARDOWN).read()
    case = re.search(r'while \[\[ \$# -gt 0 \]\]; do\n  case "\$1" in\n(.*?)\n  esac', src, re.S)
    assert case, "teardown-stack.sh arg parser not found"
    return set(re.findall(r"(?<![\w-])(--?[a-z][a-z-]*)\)", case.group(1))) | \
        set(f for line in case.group(1).splitlines() for f in re.findall(r"(--[a-z-]+)", line.split(")")[0]))


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
    assert "--accept-licence-loss" not in cmd, "the licence-loss flag needs a release first"
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "demo", "licences_released": True},
                     start=start, jobs=jobs)
    assert started[-1][1][-2:] == ["--yes", "--accept-licence-loss"]
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


# -------------------------------------------------------------- licences
class FakeKL(object):
    """kvo_license.py's surface as the API uses it, recording every call."""
    def __init__(self, licences=None, ents=None, states=None):
        self.calls = []
        self.licences = licences if licences is not None else []
        self.ents = ents or {}
        self.states = states or {}

    def accept_eula(self, kvo, verify):
        self.calls.append(("eula", kvo))
        return True

    def token(self, kvo, user, pw, verify):
        self.calls.append(("token", kvo, user, pw, verify))
        if pw == "wrong":
            raise Exception("HTTP Error 401: Unauthorized")
        return "TOK"

    def _req(self, method, url, token=None, body=None, verify=False, timeout=30):
        self.calls.append(("req", method, url, token, body))
        if url.endswith("/licensing/licenses"):
            return 200, list(self.licences)
        return 202, {"url": "/op/" + url.rsplit("/", 1)[-1]}

    def lookup_code(self, kvo, base, tok, code, verify):
        self.calls.append(("lookup", code))
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
        {"code": "BBBB-2222", "valid": True, "state": "SUCCESS",
         "entitlements": [{"product": "CloudLens-Credit", "available": 10, "total": 20}]},
        {"code": "ZZZZ-9999", "valid": False, "state": "FAILED", "entitlements": []}]
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
                             "result": {"op": "deactivate"}}]
    # the action may also come from the route path
    assert api.licences(creds, action="list")["count"] == 0


def test_licences_refuses_bad_input_and_reports_auth_without_the_password(monkeypatch):
    kl = FakeKL()
    monkeypatch.setattr(api, "_kvo_license", lambda: kl)
    creds = {"kvo": "kvo.example.net", "password": "wrong"}
    r = api.licences(dict(creds, action="list"))
    assert r["http"] == 502 and "login failed" in r["error"] and "wrong" not in r["error"]
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
    assert len(kl.calls) == n, "a refused body never reaches the KVO"
    # the failed deactivate is reported as not released
    kl2 = FakeKL(licences=[{"activationCode": "AAAA-1111", "quantity": 5}], states={"deactivate": "FAILED"})
    monkeypatch.setattr(api, "_kvo_license", lambda: kl2)
    r = api.licences({"kvo": "1.2.3.4", "action": "release", "rows": [{"code": "AAAA-1111", "quantity": 5}]})
    assert r["released"] is False and r["clear"] is False and r["results"][0]["ok"] is False


def test_kvo_license_imports_cleanly_from_the_scripts_dir():
    kl = api._kvo_license()
    for name in ("_req", "token", "lookup_code", "poll_op", "accept_eula"):
        assert callable(getattr(kl, name)), name


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
    while len(job.buffer) < 2000:
        pass
    stop.set()
    for t in threads:
        t.join(5)
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


def test_post_routes_reject_a_foreign_host_and_origin(live):
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
    # the guard covers the older POST routes too
    st, r = _call(live, "POST", "/stop/nope", headers={"Host": "evil.example"})
    assert st == 403
    # GET is unaffected: the page itself is fetched by name
    st, r = _call(live, "GET", "/flows", headers={"Host": "evil.example"})
    assert st == 200


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
    # a login the KVO refuses is a 502 with the KVO's words and never the password
    st, r = _call(live, "POST", "/api/licences", dict(creds, action="list", password="wrong"))
    assert st == 502 and "login failed" in r["error"] and "wrong" not in r["error"]
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


def test_run_and_answer_routes_are_wired(live, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    started = []
    monkeypatch.setattr(api, "_start_engine", lambda job, cmd, cwd, env: started.append((job, cmd, env)))
    st, r = _call(live, "POST", "/api/run", {"plan": GOOD, "secrets": {"CLOUDLENS_VC_PASSWORD": "pw"}})
    assert st == 200 and r["job_id"] in server.JOBS, r
    job, cmd, env = started[0]
    assert env == {"CLOUDLENS_VC_PASSWORD": "pw"} and "--profile" in cmd
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
