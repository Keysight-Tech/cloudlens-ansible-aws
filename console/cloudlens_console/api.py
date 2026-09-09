"""The console's API: every route a face on an existing command.

Nothing here knows AWS on its own. Discovery shells out to the aws CLI with
--output json (an argv list, never a shell); the doctor is deploy-stack.sh
--doctor read back through its own events file; a run is deploy-stack.sh
--profile on a file this module wrote from the one allowlist the script
enforces (profile.py), and a re-run or a resume is that same script on the
file that is already there; a teardown is teardown-stack.sh with the flags
it documents; licensing is scripts/kvo_license.py's own functions against
the KVO REST API. status() adds the two reads that have no CLI: the
vController's own REST API (with the credentials file the deploy wrote,
never anything from a URL) and ssh to the vPB with the EC2 key pair. Every
field it returns carries either a value or the reason there is none, and
no field is ever a fabricated zero.

Every function validates its inputs before anything runs and answers a bad
one as {"error": str} or {"errors": [str]}, which server.py sends as 400 (or
the "http" code the dict names). Secrets reach the engine as environment
(SECRET_ENV) or argv (activation codes: the script's only way to take them)
and go nowhere else: never the profile file, never a job's inputs or its
events, never a log line, never a response.
"""
from __future__ import annotations
import importlib.util
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import events as E
from . import orchestrator as O
from . import profile as P

REPO = O.REPO_ROOT
DEPLOY = os.path.join(REPO, "deploy", "deploy-stack.sh")
TEARDOWN = os.path.join(REPO, "deploy", "teardown-stack.sh")
KVO_LICENSE = os.path.join(REPO, "scripts", "kvo_license.py")

DOCTOR_TIMEOUT = 120    # seconds for deploy-stack.sh --doctor (it probes the network)
AWS_TIMEOUT = 60        # seconds per aws CLI call
MAX_ROWS = 50           # workload rows returned; the count is always the whole
MAX_LIST = 50           # entries in one list a body carries: codes, rows, VPC ids
MAX_ANSWER = 64 * 1024  # bytes of one prompt answer
MAX_VALUE = 4096        # characters of one plan value or secret
MAX_USER = 128          # characters of a KVO user name
MAX_PASSWORD = 1024     # characters of a KVO password
# One licensing request POSTs an operation per row and polls each to its end,
# so its cost is rows times the poll. kvo_license.py's own default is 120
# seconds PER poll, which made a 50-row body a request that could hold a
# connection for an hour and a half while the page showed one static word.
# MAX_OPS bounds the rows; OP_BUDGET bounds the whole request, shared out
# across them, so the last row cannot start a fresh 120 seconds.
MAX_OPS = 10            # activation codes in one polled licensing call
# check is a lookup per code, polled the same way, and it is where a
# customer's whole paste lands: it spends nothing, so it may take more rows
# than activate, but not 50 of them, and it shares the same OP_BUDGET so a
# long list only makes each row's share smaller.
MAX_CHECK = 25          # activation codes looked up in one check call
OP_BUDGET = 180         # seconds for all of one licensing request's polling

# ---------------------------------------------------------------- rules
# Every rule below is applied through _shape(): a control-character check
# first, then re.fullmatch. Not re.match: Python's `$` also matches before
# a trailing newline, bash's `=~ ^...$` does not, so "abc\n" passed every
# anchored rule here and reached an argv as --stack-name "abc\n" while the
# script would have refused it (test_api runs the samples through bash).
#
# deploy-stack.sh valid_stack_name(), verbatim (test_api holds them equal).
STACK = re.compile(r"^[a-zA-Z][-a-zA-Z0-9]*$")
STACK_MAX = 128         # CloudFormation's own limit on a stack name
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ZONE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d[a-z]$")
VPC = re.compile(r"^vpc-[0-9a-f]{8,17}$")
SUBNET = re.compile(r"^subnet-[0-9a-f]{8,17}$")
SG = re.compile(r"^sg-[0-9a-f]{8,17}$")
EKS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
# an activation code, optionally with the quantity the script's --kvo-codes
# takes. The first character is never a hyphen: the code rides an argv
# after --kvo-codes, and "--events" would be a code by shape and a flag
# to the script.
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,63}$")
CODE_QTY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{3,63})(?:,([0-9]{1,6}))?$")
# the KVO address: an IP or a hostname, never a URL, a port or a path
HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
# the script's prompt ids are p1, p2, ...; the rule leaves room for a
# renamed scheme but never for a byte the FIFO line could not carry
PROMPT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
TAG_KEY_MAX, TAG_VALUE_MAX = 128, 256   # AWS's own limits


def _shape(rule, value, cap=None):
    """True when `value` is a string under `cap` characters, holds no
    control character, and `rule` matches ALL of it. The control check
    runs before the rule for every typed value, whatever its rule allows;
    fullmatch, never match, for the newline reason above."""
    if not isinstance(value, str) or (cap is not None and len(value) > cap):
        return False
    if CONTROL.search(value):
        return False
    return rule.fullmatch(value) is not None


def stack_ok(name):
    """deploy-stack.sh's valid_stack_name() plus CloudFormation's length
    cap: the one check plan() and teardown() share, held equal to bash by
    test_stack_rule_is_the_scripts_own."""
    return _shape(STACK, name, STACK_MAX)


# Typed profile keys: what each must look like when it is not empty. Shape
# only, as profile.py says; the vocabulary of the rest is the forms' job.
_TYPED = {
    "CLOUDLENS_REGION": (REGION, "an AWS region like us-east-1"),
    "CLOUDLENS_COLLECTOR_ZONE": (ZONE, "an availability zone like us-east-1a"),
    "CLOUDLENS_EXISTING_VPC_ID": (VPC, "a VPC id (vpc-...)"),
    "CLOUDLENS_EKS_CLUSTER": (EKS, "an EKS cluster name"),
}
for _k in ("CLOUDLENS_EXISTING_SUBNET_ID", "CLOUDLENS_EXISTING_DATA_SUBNET_ID", "CLOUDLENS_EXISTING_TOOL_SUBNET_ID",
           "CLOUDLENS_COLLECTOR_MGMT_SUBNET", "CLOUDLENS_COLLECTOR_INGRESS_SUBNET", "CLOUDLENS_COLLECTOR_EGRESS_SUBNET"):
    _TYPED[_k] = (SUBNET, "a subnet id (subnet-...)")
for _k in ("CLOUDLENS_EXISTING_SG_ID", "CLOUDLENS_COLLECTOR_MGMT_SG", "CLOUDLENS_COLLECTOR_INGRESS_SG",
           "CLOUDLENS_COLLECTOR_EGRESS_SG"):
    _TYPED[_k] = (SG, "a security group id (sg-...)")

# The environment names deploy-stack.sh reads a secret from, and where. A
# secret goes to the engine as environment only: never the profile
# (profile-keys.txt is the allowlist and test_profile.py's NEVER list holds
# these out of it), never the events file, never a log line. Any other name
# offered as a secret is refused, so nothing can smuggle a variable the
# script does not read (or one it reads as something else) into its shell.
SECRET_ENV = {
    # vc_password_now() reads it for the login hint; phase 9 passes it to
    # vcontroller_project_key.py --new-password; phase 12 reads it as
    # VC_ADMIN_PASS for the KVO adoption of the vController.
    "CLOUDLENS_VC_PASSWORD": "the vController admin password",
    # KVO_ADMIN_PASS="${CLOUDLENS_KVO_ADMIN_PASS:-admin}" at the top of the
    # script; the KVO Keycloak token request and the login hint use it.
    "CLOUDLENS_KVO_ADMIN_PASS": "the KVO admin password",
    # MIRROR_ACCESS_KEY / MIRROR_SECRET_KEY: the AWS key pair KVO mirrors
    # with, handed to scripts/kvo_aws_mirror.py --aws-access-key /
    # --aws-secret-key in the mirror phase (else asked via ask_secret).
    "CLOUDLENS_MIRROR_ACCESS_KEY": "the AWS access key id KVO mirrors with",
    "CLOUDLENS_MIRROR_SECRET_KEY": "the AWS secret access key KVO mirrors with",
}
# Activation codes have NO environment name: the script takes them only as
# --kvo-codes CODE[,QTY] (repeatable), so run() puts them on the argv.
# kvo_license.py needs a TTY to prompt for them, and the engine has none.
# An argv is readable by `ps` to any local user for the life of the run,
# which on the single-user laptop this console serves is the same exposure
# as typing `--kvo-codes` at the shell by hand; the script and the licence
# tool print parts of them, so run() registers each code (and every secret)
# with the job and the engine redacts them from the stream (Job.redact).


class AwsError(Exception):
    """The aws CLI could not answer: not installed, not signed in, or refused."""


# ---------------------------------------------------------------- helpers
def _err(message, http=None):
    r = {"error": message}
    if http:
        r["http"] = http
    return r


def _is_str(v, cap=MAX_VALUE):
    return isinstance(v, str) and len(v) <= cap


def _tag_part(v, cap):
    return _is_str(v, cap) and bool(v) and not CONTROL.search(v)


def _name_tag(tags):
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "") or ""
    return ""


def _check_region(region):
    if not _shape(REGION, region):
        return _err("region must be an AWS region like us-east-1")
    return None


# ------------------------------------------------------------------- plan
def plan(p):
    """Validate a plan ({KEY: value}) and render it through profile.py.

    Unknown keys are refused by name (profile.render would drop them
    silently, which is right for the script and wrong for a form: the
    operator typed something the deploy will ignore). Values are strings; a
    typed key must look like what it names; what render() cannot write
    faithfully (a double quote, a line break) is reported, not raised. The
    stack name is the script's own rule, because the profile file name and
    the CloudFormation stack come from it. Returns {profile_text, resolved,
    stack, region, profile_file} or {errors: [...]}.

    `resolved` is one row per profile key, in the file's order: the value
    the plan gives, or None with a note that the script will ask or apply
    its own default. No default is invented here: the script owns them.
    """
    if not isinstance(p, dict):
        return {"errors": ["plan must be an object of CLOUDLENS_* keys"]}
    keys = P.allowed_keys()
    errors = []
    for k in sorted(p):
        if k not in keys:
            errors.append("%s: not a profile key (deploy/profile-keys.txt is the list)" % k)
    for k in keys:
        if k not in p or p[k] is None:
            continue
        v = p[k]
        if not _is_str(v):
            errors.append("%s: a value is a string of at most %d characters" % (k, MAX_VALUE))
            continue
        if not v:
            continue        # written empty, as the interview does for a field it did not use
        if CONTROL.search(v):
            errors.append("%s: control characters are not allowed" % k)
        elif k in _TYPED and not _shape(_TYPED[k][0], v):
            errors.append("%s: must be %s" % (k, _TYPED[k][1]))
        elif k == "CLOUDLENS_DISCOVERY_TAG_KEY" and not _tag_part(v, TAG_KEY_MAX):
            errors.append("%s: a tag key is 1 to %d characters" % (k, TAG_KEY_MAX))
        elif k == "CLOUDLENS_DISCOVERY_TAG_VALUE" and not _is_str(v, TAG_VALUE_MAX):
            errors.append("%s: a tag value is at most %d characters" % (k, TAG_VALUE_MAX))
    stack = p.get("CLOUDLENS_STACK_NAME")
    if not isinstance(stack, str) or not stack:
        errors.append("CLOUDLENS_STACK_NAME: required (the profile file and the stack are named after it)")
    elif not stack_ok(stack):
        errors.append("CLOUDLENS_STACK_NAME: must start with a letter and use only letters, digits and "
                      "hyphens, at most %d characters (the script's own rule)" % STACK_MAX)
    region = p.get("CLOUDLENS_REGION")
    if not isinstance(region, str) or not region:
        errors.append("CLOUDLENS_REGION: required")
    if errors:
        return {"errors": errors}
    try:
        text = P.render(p)
    except ValueError as exc:
        return {"errors": [str(exc)]}
    resolved = []
    for k in keys:
        if k in p and p[k] is not None:
            resolved.append({"key": k, "value": p[k]})
        else:
            resolved.append({"key": k, "value": None,
                             "note": "not in the plan: the script asks, or applies its own default"})
    return {"profile_text": text, "resolved": resolved, "stack": stack, "region": region,
            "profile_file": "deploy-profile-%s.env" % stack}


# --------------------------------------------------------------- discover
def _aws(args, region, timeout=AWS_TIMEOUT):
    """One aws CLI call as parsed JSON. An argv list (never a shell), the
    region and --output json appended here so no caller can forget them;
    AWS_PAGER cleared so the CLI never waits on a pager. Any failure is an
    AwsError with the CLI's own last line, which is where it says why."""
    argv = ["aws"] + list(args) + ["--region", region, "--output", "json"]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=timeout, env=dict(os.environ, AWS_PAGER=""))
    except FileNotFoundError:
        raise AwsError("the aws CLI is not installed (or not on PATH); the doctor says how to fix that")
    except OSError as exc:
        # a CLI on PATH that cannot run (mode, a broken interpreter line,
        # a mount that refuses to exec) is an answer too, not a traceback
        raise AwsError("the aws CLI could not be run: %s" % (exc.strerror or exc))
    except subprocess.TimeoutExpired:
        raise AwsError("aws %s timed out after %ds" % (" ".join(args[:2]), timeout))
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
        raise AwsError(lines[-1].strip() if lines else "aws %s exited %d" % (" ".join(args[:2]), proc.returncode))
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        raise AwsError("aws %s returned something that is not JSON" % " ".join(args[:2]))


def _filters(*pairs):
    """describe-* --filters as one JSON argument: the shorthand splits values
    on commas, and a tag value may hold one."""
    return json.dumps([{"Name": n, "Values": list(v)} for n, v in pairs], separators=(",", ":"))


def discover_vpcs(region):
    """[{id, cidr, name}] from ec2 describe-vpcs."""
    bad = _check_region(region)
    if bad:
        return bad
    try:
        data = _aws(["ec2", "describe-vpcs"], region)
    except AwsError as exc:
        return _err(str(exc), 502)
    return [{"id": v.get("VpcId", ""), "cidr": v.get("CidrBlock", ""), "name": _name_tag(v.get("Tags"))}
            for v in data.get("Vpcs", [])]


def discover_subnets(region, vpc):
    """[{id, az, cidr, public, igw_route, name}] for one VPC. Two readings
    of "public", both returned so the wizard can show either and agree
    with the interview:
      public     MapPublicIpOnLaunch, exactly what deploy-stack.sh's
                 pick_subnet() prints as public/private (its
                 "public=auto-assigns public IPs" column)
      igw_route  the route-table truth: the subnet's own association,
                 else the VPC's main table, has an active route to an
                 internet gateway. A NAT gateway is not one. A subnet
                 can be either without the other."""
    bad = _check_region(region)
    if bad:
        return bad
    if not _shape(VPC, vpc):
        return _err("vpc must be a VPC id (vpc-...)")
    try:
        subnets = _aws(["ec2", "describe-subnets", "--filters", _filters(("vpc-id", [vpc]))], region)
        tables = _aws(["ec2", "describe-route-tables", "--filters", _filters(("vpc-id", [vpc]))], region)
    except AwsError as exc:
        return _err(str(exc), 502)
    main_public, by_subnet = False, {}
    for rt in tables.get("RouteTables", []):
        igw = any((r.get("GatewayId") or "").startswith("igw-") and r.get("State", "active") == "active"
                  for r in rt.get("Routes", []))
        for a in rt.get("Associations", []):
            if a.get("Main"):
                main_public = igw
            elif a.get("SubnetId"):
                by_subnet[a["SubnetId"]] = igw
    out = []
    for s in subnets.get("Subnets", []):
        sid = s.get("SubnetId", "")
        out.append({"id": sid, "az": s.get("AvailabilityZone", ""), "cidr": s.get("CidrBlock", ""),
                    "public": s.get("MapPublicIpOnLaunch") is True,
                    "igw_route": by_subnet.get(sid, main_public), "name": _name_tag(s.get("Tags"))})
    return out


def _parse_tag(tag):
    """'K=V' -> (K, V) under AWS's limits, or None."""
    if not isinstance(tag, str) or "=" not in tag:
        return None
    k, v = tag.split("=", 1)
    if not _tag_part(k, TAG_KEY_MAX) or not _is_str(v, TAG_VALUE_MAX) or CONTROL.search(v):
        return None
    return k, v


def discover_workloads(region, tag, vpcs=""):
    """The running instances carrying tag K=V (and, when given, in the listed
    VPCs): the same filter scripts/workload_selection.py applies, so the
    count is what the sensor and mirror paths would select. `count` is the
    whole; `rows` is the first MAX_ROWS."""
    bad = _check_region(region)
    if bad:
        return bad
    kv = _parse_tag(tag)
    if not kv:
        return _err("tag must be KEY=VALUE (a key of 1 to %d characters, a value of at most %d, no control "
                    "characters)" % (TAG_KEY_MAX, TAG_VALUE_MAX))
    ids = [v.strip() for v in (vpcs or "").split(",") if v.strip()] if isinstance(vpcs, str) else None
    if ids is None or len(ids) > MAX_LIST or any(not _shape(VPC, v) for v in ids):
        return _err("vpcs must be a comma-separated list of VPC ids (vpc-...)")
    pairs = [("tag:" + kv[0], [kv[1]]), ("instance-state-name", ["running"])]
    if ids:
        pairs.append(("vpc-id", ids))
    try:
        data = _aws(["ec2", "describe-instances", "--filters", _filters(*pairs)], region)
    except AwsError as exc:
        return _err(str(exc), 502)
    rows = []
    for res in data.get("Reservations", []):
        for i in res.get("Instances", []):
            rows.append({
                "id": i.get("InstanceId", ""), "name": _name_tag(i.get("Tags")),
                "vpc": i.get("VpcId", ""), "subnet": i.get("SubnetId", ""),
                "az": (i.get("Placement") or {}).get("AvailabilityZone", ""),
                "state": (i.get("State") or {}).get("Name", ""), "type": i.get("InstanceType", ""),
                "platform": i.get("PlatformDetails") or i.get("Platform") or "",
                "private_ip": i.get("PrivateIpAddress", "") or "", "public_ip": i.get("PublicIpAddress", "") or "",
            })
    return {"count": len(rows), "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS,
            "filter": {"tag": kv[0] + "=" + kv[1], "vpcs": ids, "state": "running"}}


def discover_eks(region):
    """[{name}] from eks list-clusters."""
    bad = _check_region(region)
    if bad:
        return bad
    try:
        data = _aws(["eks", "list-clusters"], region)
    except AwsError as exc:
        return _err(str(exc), 502)
    return [{"name": n} for n in data.get("clusters", []) if isinstance(n, str)]


# ----------------------------------------------------------------- doctor
def _kill_group(proc):
    """KILL the whole process group a Popen started with start_new_session
    leads (its pid is the pgid), then reap it. ESRCH is the group already
    gone; EPERM on macOS is every member a zombie awaiting the reap. The
    leader alone was never enough: the doctor's probes (curl, the aws CLI)
    are children holding its stdout, and a kill that left them alive left
    the pipe open, so the read after the kill waited on them instead."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.communicate()


def doctor(region):
    """deploy-stack.sh --doctor --region R --events F, synchronously, read
    back from F: the check events as {item, status, fix}, ok when none
    failed. No --prompt-pipe: the doctor asks nothing, and a pipe with no
    writer would block it. Same rules as the engine: no stdin, no
    controlling terminal (the script re-attaches /dev/tty when it can),
    its own session, and a timeout that ends the whole group."""
    bad = _check_region(region)
    if bad:
        return bad
    work = tempfile.mkdtemp(prefix="cloudlens-console-doctor-")
    events = os.path.join(work, "events.jsonl")
    argv = ["bash", DEPLOY, "--doctor", "--region", region, "--events", events]
    try:
        try:
            proc = subprocess.Popen(argv, cwd=REPO, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
        except OSError as exc:
            return _err("could not run deploy-stack.sh: %s" % exc, 500)
        try:
            out, err = proc.communicate(timeout=DOCTOR_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return _err("the doctor timed out after %gs (it probes GitHub, the template bucket and AWS)"
                        % DOCTOR_TIMEOUT, 504)
        _, evs = E.iter_script_events(events)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    checks = [{"item": e.get("item", ""), "status": e.get("status", ""), "fix": e.get("fix", "") or ""}
              for e in evs if e["type"] == E.CHECK]
    if not checks:
        tail = [l for l in ((out or "") + (err or "")).splitlines() if l.strip()][-3:]
        return _err("the doctor produced no checks (exit %d): %s" % (proc.returncode, " | ".join(tail)), 502)
    return {"checks": checks, "ok": not any(c["status"] == "fail" for c in checks), "exit": proc.returncode}


# -------------------------------------------------------------------- run
def _jobs():
    from . import server   # late: server imports this module
    return server.JOBS


def _start_engine(job, cmd, cwd, env):
    """The real starter: run_engine on a daemon thread, the two flags
    appended by the engine itself."""
    t = threading.Thread(target=O.run_engine, args=(job, cmd), kwargs={"cwd": cwd, "env": env}, daemon=True)
    t.start()
    return t


def _start_teardown(job, cmd, cwd, env):
    """teardown-stack.sh has no events channel and rejects flags it does not
    know, so it runs unwired: stdout is the stream, exit is the verdict."""
    t = threading.Thread(target=O.run_engine, args=(job, cmd),
                         kwargs={"cwd": cwd, "env": env, "wired": False}, daemon=True)
    t.start()
    return t


def _launch(start, job, cmd, env):
    """Hand a registered job to its starter. The job holds its stack from
    registration to its terminal event (_in_flight), and run_engine emits
    that event on every path; a starter that raises before run_engine
    runs (a thread the OS refused) would leave the job registered and
    never done, the stack held until the console restarts. The failure
    becomes the job's terminal event first; the exception then goes on to
    the caller as it did."""
    try:
        start(job, cmd, REPO, env)
    except Exception as exc:
        job.emit(E.error("could not start the engine: " + type(exc).__name__,
                         fix="Restart the console and try again."))
        raise


# Held while run() and teardown() decide that a stack is free and register
# the job that takes it (run() writes the profile in between). The decision
# and the registration have to be one step: two requests for one stack that
# both looked before either had registered both found it free.
_ENGINE_LOCK = threading.Lock()


def _check_secrets(secrets):
    """(env, errors): the secrets as the engine's extra environment. A name
    has to be one the script reads a secret from (SECRET_ENV) and can never
    be a profile key; a value is a string the environment can hold, with
    no control character: the script hands a password on to a command
    line and a form body, and a newline or a tab inside it ends the
    argument or the field early. An empty value is dropped: the script
    reads ${NAME:-} and treats it as unset."""
    if not isinstance(secrets, dict):
        return {}, ["secrets must be an object of {NAME: value}"]
    env, errors = {}, {}
    allowed = set(P.allowed_keys())
    for name, value in secrets.items():
        if name in allowed:
            errors[name] = "%s is a profile key, not a secret: put it in the plan" % name
        elif name not in SECRET_ENV:
            errors[name] = "%s is not a secret deploy-stack.sh reads (%s)" % (name, ", ".join(sorted(SECRET_ENV)))
        elif not _is_str(value) or CONTROL.search(value):
            errors[name] = "%s: a secret is a string of at most %d characters with no control character" % (
                name, MAX_VALUE)
        elif value:
            env[name] = value
    return env, [errors[k] for k in sorted(errors)]


def _check_codes(codes, with_qty=True):
    """(codes, error): activation codes as strings, optionally CODE,QTY. A
    refused entry is named by its position, never echoed: a code is a
    secret, and a near miss is most of one."""
    if codes is None:
        return [], None
    if not isinstance(codes, list):
        return [], ('codes must be a list of activation codes, ["CODE"] or ["CODE,QTY"] as the Licensing '
                    "screen sends them, and at most %d of them in one body" % MAX_LIST)
    if len(codes) > MAX_LIST:
        # as the MAX_CHECK and MAX_OPS refusals do: say what to do next.
        # A list this long has to be split whatever it is for, because
        # every action that takes one is capped tighter than this.
        return [], ("codes takes at most %d activation codes in one body, and this asks for %d. Split the "
                    "paste and send it in batches: check takes at most %d in one call and activate at most "
                    "%d, so a list this long has to be broken up either way."
                    % (MAX_LIST, len(codes), MAX_CHECK, MAX_OPS))
    rule = CODE_QTY if with_qty else CODE
    out = []
    for n, c in enumerate(codes, 1):
        # strip() forgives the whitespace a pasted code carries; what is
        # left has to be the whole code, control bytes included in "not"
        if not isinstance(c, str) or not _shape(rule, c.strip()):
            return [], "codes: entry %d is not an activation code%s" % (n, " (CODE or CODE,QTY)" if with_qty else "")
        out.append(c.strip())
    return out, None


def _unredactable(env, codes):
    """The secrets and activation codes this run could not blank from its
    own output, as errors ready for the caller's list.

    Everything registered with a job is replaced wherever it appears in
    that run's stream, so a very short one rewrites words that are not the
    secret: orchestrator.MIN_REDACTION carries the run this cost. The
    floor is enforced there too (Job.add_redaction raises), but a raise
    inside the launch would be a 500 for a typo. Checked here, beside the
    other validation and before the engine lock, it is a 400 that names
    the field and says what to do.

    Neither the secret nor the code is echoed: a refusal that quotes a
    near-miss is most of a secret."""
    bad = []
    for name in sorted(env):
        if len(env[name]) < O.MIN_REDACTION:
            bad.append("%s: a secret of %d characters is refused. The console blanks every secret from "
                       "the run's own output, and a string that short also appears inside the words the "
                       "console's frames are made of, so blanking it would corrupt them. Send the real "
                       "value (at least %d characters)." % (name, len(env[name]), O.MIN_REDACTION))
    for n, c in enumerate(codes, 1):
        if len(CODE_QTY.fullmatch(c).group(1)) < O.MIN_REDACTION:
            bad.append("kvo_codes: entry %d is an activation code of under %d characters. The console blanks "
                       "every code from the run's own output, and one that short cannot be blanked without "
                       "corrupting the console's own frames." % (n, O.MIN_REDACTION))
    return bad


def _register(job, env, codes):
    """Register a launch's secrets and activation codes with the job, so
    the stream blanks them. The quantity on a CODE,QTY is not part of the
    secret and is not registered. _unredactable() has already refused
    anything add_redaction would raise on."""
    for c in codes:
        job.add_redaction(CODE_QTY.fullmatch(c).group(1))   # the code, never the quantity
    for value in env.values():
        job.add_redaction(value)


def _in_flight(jobs, stack, region):
    """The id of a registered job that holds this stack in this region,
    else None. Two engines on one stack would write one profile file and
    race CloudFormation on one stack name, and a teardown of a stack
    mid-deploy is the same race from the other side, so run() and
    teardown() refuse the second with a 409 before they write anything.

    An engine job (engine-deploy, engine-teardown) holds the stack from
    the moment it is registered until its terminal event. run_engine emits
    that event on every path (stopped before the launch, a Popen that
    failed, or the verdict after the exit), so `not done` is exact.
    running() alone was not: it begins at the engine thread's Popen, 5-20
    ms after run() had registered the job, and a second request in that
    window found the stack free. running() still counts on its own, for
    any job whose process group is open. The callers hold _ENGINE_LOCK
    across this check and their own registration."""
    for job_id, job in list(jobs.items()):
        if job.inputs.get("stack") != stack or job.inputs.get("region") != region:
            continue
        if job.running() or (job.flow_id.startswith("engine-") and not job.done):
            return job_id
    return None


# a phase name as deploy-stack.sh writes them in PHASE_ORDER: short, lower
# case, stable. The rule bounds the shape; phase_order() decides the
# vocabulary, because only the script knows it.
PHASE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# KEY="value" as deploy-stack.sh's profile loader reads a line back, line for
# line with the loader (its `_pl="${_pl#export }"` and the two lines under
# "one matching pair of quotes around the whole value, either kind"):
#
#   an optional leading `export ` (exactly one space, as ${_pl#export }
#   strips it). Without this the guard failed OPEN: a profile written with
#   `export CLOUDLENS_REGION="us-west-2"` is loaded by the script and read
#   as nothing here, so the region check passed and the replay ran in the
#   profile's region while the console held, and reported, the typed one.
#
#   one matching pair of outer quotes, EITHER kind. A single-quoted value
#   is unquoted by the loader and was not here, so the same profile read
#   as CLOUDLENS_REGION='us-west-2' against a typed us-west-2 was refused
#   over quotes the script never sees.
#
# The value is taken whole (the loader's BASH_REMATCH[2] is the rest of the
# line) and unquoted by _unquote, never by the regex: a non-greedy "?(.*?)"?$
# also strips a lone leading quote, which the loader keeps.
_PROFILE_LINE = re.compile(r'^(?:export )?(CLOUDLENS_[A-Z0-9_]+)=(.*)$')


def _unquote(value):
    """One matching pair of outer quotes, either kind, stripped: the
    loader's own rule. Nothing else is unescaped, and an unbalanced quote
    is part of the value, as it is there."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _profile_says(path, keys):
    """{key: value or None} for `keys`, read out of a deploy profile.

    The last assignment wins, as a shell loader's would. A file that cannot
    be opened says nothing about any key, which is not the same as saying
    the key is absent: the caller only refuses on a value that DISAGREES,
    so an unreadable profile is left to fail where it is used."""
    out = dict.fromkeys(keys)
    try:
        # utf-8-sig, not utf-8: a BOM belongs to the encoding, not to the
        # first key. The script's loader strips it before it parses
        # anything (`sed '1s/^\xEF\xBB\xBF//'`, whose comment names the
        # Windows editors that add one) and str.strip() below does not, so
        # a profile whose first line was CLOUDLENS_REGION= or
        # CLOUDLENS_STACK_NAME= read as "\ufeffCLOUDLENS_REGION=..." here,
        # matched nothing, and said NOTHING about the key. The caller only
        # refuses on a value that disagrees, so the guard failed OPEN in
        # the same direction the `export ` prefix used to: the replay ran
        # in the profile's region while the console held, and reported,
        # the typed one.
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = _PROFILE_LINE.match(line)
                if m and m.group(1) in out:
                    out[m.group(1)] = _unquote(m.group(2))
    except OSError:
        pass
    return out


def _replay(body, jobs, start=None):
    """The Operate screen's two buttons: Resume, and Re-run one phase.
    {stack, region, only?, secrets?, kvo_codes?}.

    Both replay the profile file that already exists next to the deploy
    script (deploy-profile-<stack>.env, which run() wrote from a plan the
    operator reviewed), and NEITHER writes one. That is what keeps this
    from being a second way to launch an unreviewed deploy: with no such
    file the request is refused and the answer sends the operator to the
    Deploy screen. Both go through the same _in_flight guard under the
    same _ENGINE_LOCK as run() and teardown(), so one stack still has one
    engine.

    Both pass --resume. The engine hands the script a --prompt-pipe, which
    forces INTERACTIVE=true, so without --resume every replay stops on the
    script's own "Continue from <phase>? [Y/n]" in the Watch modal;
    --resume is the answer the operator already gave by pressing the
    button, and it deletes nothing either way (the script says so in a
    dozen places). Re-run adds --only PHASE, and the phase has to be one
    of the script's own PHASE_ORDER, read from the script by
    phase_order(); resume adds no --only and lets the script's own resume
    decide what to skip. Secrets and activation codes travel exactly as
    they do on a launch: environment and argv, both registered with the
    job so the stream redacts them.

    The profile is checked against the stack and the region the request
    named before anything starts: --profile is the whole argv, so the file
    decides where the run happens, and a profile that disagrees is refused
    with both values rather than run somewhere the operator is not
    looking."""
    stack, region = body.get("stack"), body.get("region")
    if not stack_ok(stack):
        return _err("stack must be a CloudFormation stack name (letters, digits and hyphens, starting with a letter)")
    bad = _check_region(region)
    if bad:
        return bad
    only, order = None, phase_order()
    if "only" in body:
        only = body["only"]
        if not _shape(PHASE, only) or only not in order:
            return _err("only must name one phase deploy-stack.sh runs (%s)"
                        % (", ".join(order) or "the script names none"))
    env, errors = _check_secrets(body.get("secrets") or {})
    codes, bad = _check_codes(body.get("kvo_codes"))
    if bad:
        errors.append("kvo_codes: " + bad)
    else:
        # only when every code parsed: _unredactable reads the CODE half
        errors += _unredactable(env, codes)
    if errors:
        return {"errors": errors}
    profile_file = "deploy-profile-%s.env" % stack
    path = os.path.join(REPO, profile_file)
    with _ENGINE_LOCK:
        busy = _in_flight(jobs, stack, region)
        if busy:
            return _err("stack %s already has a run in progress (job %s)" % (stack, busy), 409)
        if not os.path.isfile(path):
            return _err("no profile for stack %s: %s is not next to the deploy script, so there is nothing to "
                        "replay. Plan and launch it on the Deploy screen first: that is what writes the file, "
                        "and this screen only replays it." % (stack, profile_file), 400)
        # The profile has to be for the stack and the region this request
        # named. Nothing on the command line says either: --profile is the
        # whole argv, so the FILE decides where the run happens, while the
        # operator read the status for, and is watching, what they typed. A
        # profile whose region is us-west-2 replayed under a typed
        # us-east-1 ran in us-west-2 and reported as us-east-1; and because
        # _in_flight keys on the typed region, the same profile could be
        # replayed twice at once by typing two regions, which is the one
        # thing the one-engine-per-stack guard exists to stop.
        says = _profile_says(path, ("CLOUDLENS_STACK_NAME", "CLOUDLENS_REGION"))
        for key, asked, what in (("CLOUDLENS_STACK_NAME", stack, "stack"),
                                 ("CLOUDLENS_REGION", region, "region")):
            said = says.get(key)
            if said and said != asked:
                return _err('%s sets %s="%s" and this request names the %s %s. deploy-stack.sh takes the '
                            "%s from the profile, so the run would be against %s while this console holds "
                            "it as %s. Read the status for %s, or plan the deploy again on the Deploy "
                            "screen." % (profile_file, key, said, what, asked, what, said, asked, said), 400)
        cmd = ["bash", DEPLOY, "--profile", path, "--resume"]
        for c in codes:
            cmd += ["--kvo-codes", c]
        if only:
            cmd += ["--only", only]
        job_id = uuid.uuid4().hex[:12]
        job = O.Job(job_id, "engine-deploy",
                    {"stack": stack, "region": region, "profile": path, "only": only})
        _register(job, env, codes)
        jobs[job_id] = job
    shown = "bash deploy/deploy-stack.sh --profile %s --resume" % profile_file
    if codes:
        shown += " --kvo-codes ... (%d code%s)" % (len(codes), "" if len(codes) == 1 else "s")
    if only:
        shown += " --only %s" % only
    if env:
        shown += "   [%s in the environment]" % ", ".join(sorted(env))
    job.emit(E.narrate("engine: " + shown, "note"))
    _launch(start or _start_engine, job, cmd, env or None)
    return {"job_id": job_id, "profile_file": profile_file, "stack": stack, "region": region, "only": only}


def run(body, jobs=None, start=None):
    """Write deploy-profile-<stack>.env from the validated plan and start
    deploy-stack.sh --profile on it. A body with no plan but a stack name
    is the Operate screen's replay instead and goes to _replay(), which
    writes no profile and refuses without one. The plan goes through plan(); the
    secrets through _check_secrets() and then only into the engine's
    environment; activation codes (kvo_codes) onto the argv as --kvo-codes.
    The profile file name comes from the validated stack name alone, so it
    cannot leave the repo root; mode 600, as the script's own writer does.
    The job is registered before the start so /events can find it at once;
    its inputs carry the stack, the region and the profile path, never a
    secret. Every code and every secret value is registered with the job
    before the engine starts, so a line that prints one (kvo_license.py
    prints the first 14 characters of each code; the dry run prints them
    whole) reaches the stream as [redacted]: see Job.redact. Returns
    {job_id, profile_file (the name, relative to the repo root, as plan()
    gives it), stack, region}, {errors}, or a 409 {error} while a job
    holds the same stack in the same region (_in_flight). That check, the
    profile write and the registration happen under _ENGINE_LOCK, so two
    requests for one stack yield one job and one 409, whichever came
    first; the starter runs outside it, the registered job already holds
    the stack."""
    if not isinstance(body, dict):
        return {"errors": ["body must be an object: {plan, secrets?, kvo_codes?}"]}
    jobs = jobs if jobs is not None else _jobs()
    if body.get("plan") is None and body.get("stack") is not None:
        return _replay(body, jobs, start)
    p = plan(body.get("plan"))
    if p.get("errors"):
        return {"errors": p["errors"]}
    env, errors = _check_secrets(body.get("secrets") or {})
    codes, bad = _check_codes(body.get("kvo_codes"))
    if bad:
        errors.append("kvo_codes: " + bad)
    else:
        # only when every code parsed: _unredactable reads the CODE half
        errors += _unredactable(env, codes)
    if errors:
        return {"errors": errors}
    with _ENGINE_LOCK:
        busy = _in_flight(jobs, p["stack"], p["region"])
        if busy:
            return _err("stack %s already has a run in progress (job %s)" % (p["stack"], busy), 409)
        path = os.path.join(REPO, p["profile_file"])
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(p["profile_text"])
            os.chmod(path, 0o600)
        except OSError as exc:
            return {"errors": ["could not write %s: %s" % (path, exc.strerror or exc)]}
        cmd = ["bash", DEPLOY, "--profile", path]
        for c in codes:
            cmd += ["--kvo-codes", c]
        job_id = uuid.uuid4().hex[:12]
        job = O.Job(job_id, "engine-deploy", {"stack": p["stack"], "region": p["region"], "profile": path})
        _register(job, env, codes)
        jobs[job_id] = job
    shown = "bash deploy/deploy-stack.sh --profile %s" % p["profile_file"]
    if codes:
        shown += " --kvo-codes ... (%d code%s)" % (len(codes), "" if len(codes) == 1 else "s")
    if env:
        shown += "   [%s in the environment]" % ", ".join(sorted(env))
    job.emit(E.narrate("engine: " + shown, "note"))
    _launch(start or _start_engine, job, cmd, env or None)
    return {"job_id": job_id, "profile_file": p["profile_file"], "stack": p["stack"], "region": p["region"]}


def answer(job, body):
    """{prompt_id, text} -> job.answer. The job's own refusals (no prompt
    waiting, the wrong prompt, an answer already in flight, an engine that
    went away) come back as 409 with its words.

    The ok here is not the record of the answer: job.answer emits an
    `answered` frame into the stream, and that is what a page attaching
    later reads. A refusal emits nothing, so the question stays open in the
    stream because it is still open in the run."""
    if not isinstance(body, dict):
        return _err("body must be {prompt_id, text}")
    prompt_id, text = body.get("prompt_id"), body.get("text")
    if not _shape(PROMPT_ID, prompt_id):
        return _err("prompt_id must be the id of the prompt event")
    if not isinstance(text, str):
        return _err("text must be a string (the answer, one line)")
    if len(text.encode("utf-8")) > MAX_ANSWER:
        return _err("an answer is at most %d bytes" % MAX_ANSWER)
    if "\n" in text or "\r" in text:
        return _err("an answer is one line")
    try:
        job.answer(prompt_id, text)
    except ValueError as exc:
        return _err(str(exc), 409)
    return {"ok": True}


# --------------------------------------------------------------- teardown
def teardown(body, start=None, jobs=None):
    """teardown-stack.sh through the engine, unwired (it has no events
    channel). {stack, region, confirm_name, orphans_only?, licences_released?}.

    The destructive run needs the stack name typed back (confirm_name), the
    same gate the script itself puts on the licence loss; it runs with
    --yes because the engine has no terminal to confirm on, and with
    --accept-licence-loss only when the body says the licences were released
    (Task 10 sets that after /api/licences release): without it a stack that
    holds a KVO stops at the script's own licence warning, which is the
    right outcome. orphans_only is the script's --orphans: a read-only audit
    that deletes nothing, so it needs no typed name. A stack a job still
    holds (a deploy, another teardown) is refused with a 409: see
    _in_flight. The check and the registration happen under _ENGINE_LOCK,
    as in run()."""
    if not isinstance(body, dict):
        return _err("body must be {stack, region, confirm_name, orphans_only?, licences_released?}")
    jobs = jobs if jobs is not None else _jobs()
    stack, region = body.get("stack"), body.get("region")
    if not stack_ok(stack):
        return _err("stack must be a CloudFormation stack name (letters, digits and hyphens, starting with a letter)")
    bad = _check_region(region)
    if bad:
        return bad
    audit = bool(body.get("orphans_only"))
    # an exact, typed match: a name with a stray newline or control byte is
    # not the stack's name, whatever the stack field itself passed as
    if not audit and (not _shape(STACK, body.get("confirm_name")) or body.get("confirm_name") != stack):
        return _err("type the stack name (%s) as confirm_name to tear it down" % stack)
    with _ENGINE_LOCK:
        busy = _in_flight(jobs, stack, region)
        if busy:
            return _err("stack %s already has a run in progress (job %s)" % (stack, busy), 409)
        cmd = ["bash", TEARDOWN, "--stack-name", stack, "--region", region, "--yes"]
        if audit:
            cmd.append("--orphans")
        elif body.get("licences_released") is True:
            cmd.append("--accept-licence-loss")
        job_id = uuid.uuid4().hex[:12]
        job = O.Job(job_id, "engine-teardown", {"stack": stack, "region": region, "audit": audit})
        jobs[job_id] = job
    # the flags from --stack-name on: cmd[0] is bash, cmd[1] the script's path
    job.emit(E.narrate("engine: bash deploy/teardown-stack.sh " + " ".join(cmd[2:]), "note"))
    _launch(start or _start_teardown, job, cmd, None)
    return {"job_id": job_id, "stack": stack, "region": region, "audit": audit}


# --------------------------------------------------------------- licences
_KL = None


def _kvo_license():
    """scripts/kvo_license.py as a module, imported by path once. It has no
    package and its main() is behind __name__, so the import runs nothing."""
    global _KL
    if _KL is None:
        spec = importlib.util.spec_from_file_location("kvo_license", KVO_LICENSE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _KL = mod
    return _KL


def _now():
    """time.monotonic, behind a name a test can hold still. The licensing
    budget is the one thing here measured in wall clock, and a test that
    cannot move the clock can only assert that a number was passed."""
    return time.monotonic()


class _Kvo(object):
    """One logged-in KVO for the action functions below: kvo_license.py,
    the address, the token, the TLS choice and the deadline, so each action
    is a function of this and its own validated input.

    The deadline is for the WHOLE request, not per operation. An action
    that loops over rows polls each one, and kvo_license.py's own default
    is 120 seconds per poll, so a per-row timeout is a per-row promise and
    no promise at all about the request the browser is holding open."""
    __slots__ = ("KL", "kvo", "base", "tok", "verify", "deadline")

    def __init__(self, KL, kvo, tok, verify, budget=OP_BUDGET):
        self.KL, self.kvo, self.tok, self.verify = KL, kvo, tok, verify
        self.base = "https://%s" % kvo
        self.deadline = _now() + budget

    def left(self):
        """Seconds still in the budget, never below 1: a poll asked for 0
        seconds is a poll that reads nothing, and an operation the KVO has
        already accepted deserves at least one look."""
        return max(1, int(self.deadline - _now()))


# The states that mean an operation FINISHED and succeeded. An allow-list,
# and a short one, because only SUCCESS is knowable from here: kvo_license.py
# polls until the state leaves IN_PROGRESS and names no other word, and the
# deactivates that proved the release path answered SUCCESS. A KVO build that
# says something else for "done" belongs in this tuple; until it is in it, its
# rows are reported as unknown, which is the safe direction. Guessing the
# vocabulary is the unsafe one: a word wrongly counted as success is a licence
# count nobody released and a teardown banner that goes green on it.
_OP_DONE = ("SUCCESS",)


def _op_failed(state):
    """Whether the KVO REFUSED the operation. FAIL and ERROR are matched as
    substrings on purpose: FAILED, FAILURE and INTERNAL_ERROR are one
    answer. This is the only reading that may be shown as a refusal."""
    s = str(state or "").upper()
    return "FAIL" in s or "ERROR" in s


def _op_ok(state):
    """Whether the operation finished AND succeeded.

    This was a deny-list: anything truthy that was not IN_PROGRESS and
    carried neither FAIL nor ERROR. Two things got through it. IN_PROGRESS
    itself did at first ("FAIL" is not in it), so a poll that exhausted
    the request's budget counted as a success, which is not a rare shape:
    OP_BUDGET is shared across the rows of one call, so rows 4 and 5 of a
    five-code activate are polled with a second each. And any word the KVO
    might use for "accepted, not finished" - PENDING, QUEUED - still does,
    because poll_op stops at the FIRST state that is neither empty nor
    IN_PROGRESS, so such a word arrives here as the last word on the row.
    "5 of 5 activated" then counts operations nobody watched to their end,
    and on the release side one of them sits beside `clear` and turns the
    teardown banner green. An allow-list cannot make that mistake."""
    return str(state or "").upper() in _OP_DONE


def _op_running(state):
    """Whether the row's outcome is NOT KNOWN. Three shapes, one meaning:

      IN_PROGRESS, which kvo_license.py's poll_op returns in exactly one
      case: it ran out of its timeout with the KVO still working.

      no state at all. poll_op reads `state` out of the polled body and
      leaves it "" when that body is not a JSON object (an HTML page from
      a pending EULA, a plain-text error, nothing at all), and "" never
      breaks its loop, so an empty state means the poll ran to its
      deadline having never read one. That is a timeout, and it was
      reported as a refusal: `ok` false, `running` false, and the page
      printing the row as "refused", which tells the operator the KVO
      rejected a code it never answered about.

      a word that is neither a known success nor a failure, which is the
      other half of _op_ok's allow-list.

    None of the three is a refusal, and none of them is a success."""
    return not _op_ok(state) and not _op_failed(state)


def _state(info):
    return info.get("state") if isinstance(info, dict) else info


def _ents_rows(ents):
    return [{"product": p, "available": a, "total": t} for p, a, t in ents]


def _op(k, name, payload):
    """POST one licensing operation and poll it to its end, within what is
    left of the request's budget: (state, result)."""
    _, resp = k.KL._req("POST", "%s/api/v2/licensing/operations/%s" % (k.base, name), k.tok, payload, k.verify)
    op = k.KL.poll_op(k.kvo, k.tok, resp, k.verify, timeout=k.left())
    state = op.get("state") if isinstance(op, dict) else op
    result = op.get("result") if isinstance(op, dict) else None
    return state, result


# The envelope keys a licence list could plausibly arrive under. Enough to
# say the KVO STILL HOLDS something, and never enough to say it holds
# nothing: an envelope this code guessed at is not a reading of the KVO.
_LIST_KEYS = ("licenses", "licences", "items", "rows", "data")


def _body_shape(body):
    """What came back, said as a shape and never as the KVO's own words."""
    if body is None:
        return "no body"
    if isinstance(body, list):
        return "a list"
    if isinstance(body, dict):
        return "an object"
    return "text (an HTML page, or a plain-text error)"


def _read_list(k):
    """(rows, why): the licences the KVO says it still holds, and why they
    could not be read. `why` is None only when the KVO answered 200 with
    the JSON ARRAY this endpoint returns.

    kvo_license.py's _req does not raise on an HTTP status error: it
    returns (code, body), and the body of a 500 is a dict, of a pending
    EULA is an HTML redirect page, of a KVO that answered nothing is None,
    and a build that wrapped the array would be {"licenses": [...]}. The
    old reading, `rows if isinstance(rows, list) else []`, turned every
    one of those into an empty list, which is the same value a KVO holding
    nothing returns. `clear` was computed from it, licences.js recorded
    that as this session's evidence, and teardown.js turned that into
    --accept-licence-loss, which is the last gate before a stack whose KVO
    still holds its counts is deleted. Four shapes, all of them wrongly
    clear, none of them a read.

    A wrapped array is mined for rows, because rows that ARE there prove
    the KVO holds licences; its emptiness proves nothing, so it still
    comes back with a `why`."""
    try:
        code, body = k.KL._req("GET", k.base + "/api/v2/licensing/licenses", k.tok, verify=k.verify)
    except Exception as exc:  # noqa: urllib raises several; none carries a password here
        return [], "the KVO did not answer GET licenses (%s)" % type(exc).__name__
    if isinstance(body, list) and code == 200:
        return body, None
    rows = []
    if isinstance(body, dict):
        for key in _LIST_KEYS:
            if isinstance(body.get(key), list):
                rows = body[key]
                break
    return rows, ("GET licenses answered HTTP %s with %s, not the list of licences this KVO's API returns"
                  % (code, _body_shape(body)))


def _holdings(k):
    """What the KVO holds now, as a field that may say "I could not tell".

    {licences, count, clear, unreadable?}. `clear` is tri-state and is the
    only thing the teardown gate may stand on: True on a genuine empty
    list, False on any answer that named a licence, and None (with the
    reason beside it) when the list could not be read. The page treats a
    missing or null `clear` as not-clear, so None refuses the green
    banner, which is the whole point of not returning False there either:
    False would read as "the KVO answered", and it did not."""
    rows, why = _read_list(k)
    out = {"licences": rows, "count": len(rows),
           "clear": (False if rows else None) if why else not rows}
    if why:
        out["unreadable"] = why
    return out


def _lookup(k, code):
    return k.KL.lookup_code(k.kvo, k.base, k.tok, code, k.verify, timeout=k.left())


def _release_rows(body):
    """(rows, error): [(code, quantity)] from rows [{activationCode|code,
    quantity}] (the shape GET licenses returns) or codes ["CODE,QTY"]. As
    _check_codes: a refused row is named by its position, never echoed."""
    rows = body.get("rows")
    if rows is None and body.get("codes") is not None:
        codes, bad = _check_codes(body.get("codes"))
        if bad:
            return [], bad
        rows = []
        for c in codes:
            code, qty = CODE_QTY.fullmatch(c).groups()
            rows.append({"code": code, "quantity": int(qty) if qty else 0})
    if not isinstance(rows, list) or not rows or len(rows) > MAX_LIST:
        return [], ("rows must list what to release, at most %d: [{activationCode, quantity}] as GET licenses "
                    "shows them" % MAX_LIST)
    out = []
    for n, r in enumerate(rows, 1):
        code = r.get("activationCode", r.get("code")) if isinstance(r, dict) else None
        qty = r.get("quantity") if isinstance(r, dict) else None
        if not _shape(CODE, code):
            return [], "rows: entry %d is not an activation code" % n
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            return [], "rows: entry %d needs the quantity to release (a positive integer)" % n
        out.append((code, qty))
    return out, None


def _lic_list(k, _):
    """GET /api/v2/licensing/licenses -> {licences, count, clear,
    unreadable?}. A list that could not be read says so rather than
    reporting an empty appliance."""
    return _holdings(k)


def _lic_check(k, codes):
    """retrieve-activation-code-info per code -> {codes: [...]}.

    One POST and one poll per code, inside the request's own budget: a
    customer's paste comes through here first, and _lookup used to pass no
    timeout at all, so kvo_license.py's poll_op applied its own 120 second
    default PER CODE. A code whose poll ran out of the budget is reported
    as still running, not as a code the KVO refused: check spends nothing,
    but "the KVO recognised nothing under this code" about a good code is
    how an operator throws away an activation code."""
    out = []
    for code in codes:
        ents, info = _lookup(k, code)
        state = _state(info)
        out.append({"code": code, "valid": bool(ents), "state": state, "running": _op_running(state),
                    "entitlements": _ents_rows(ents)})
    return {"codes": out}


def _lic_activate(k, codes):
    """activate per code, the available quantity unless CODE,QTY says how
    many; a code with nothing available is skipped, as kvo_license.py does
    -> {results, activated, licences}."""
    results = []
    for c in codes:
        code, qty = CODE_QTY.fullmatch(c).groups()
        qty = int(qty) if qty else None
        ents, info = _lookup(k, code)
        if not ents:
            results.append({"code": code, "state": "invalid", "ok": False, "running": False, "picks": [],
                            "detail": _state(info)})
            continue
        picks = [(p, qty if qty is not None else a) for p, a, _ in ents if (qty if qty is not None else a)]
        if not picks:
            results.append({"code": code, "state": "nothing-available", "ok": False, "running": False,
                            "picks": [], "entitlements": _ents_rows(ents)})
            continue
        if len(picks) == 1:
            payload = [{"activationCode": code, "quantity": picks[0][1]}]
        else:
            payload = [{"activationCode": code, "product": p, "quantity": q} for p, q in picks]
        state, result = _op(k, "activate", payload)
        results.append({"code": code, "state": state, "ok": _op_ok(state), "running": _op_running(state),
                        "picks": [{"product": p, "quantity": q} for p, q in picks], "result": result})
    return dict(_holdings(k), results=results,
                activated=sum(1 for r in results if r["ok"]),
                # a row whose poll ran out of the budget is neither: the
                # console stopped watching before the KVO finished, and
                # counting it as activated is a number nobody measured
                running=sum(1 for r in results if r["running"]))


def _lic_release(k, rows):
    """operations/deactivate per row, polled -> {results, released (every
    row succeeded), clear (tri-state: whether the KVO holds no licence
    now), licences, running}.

    `released` and `clear` are two different facts and both are needed:
    the rows this call asked about can all come back SUCCESS while the
    appliance still holds licences nobody named, and a deactivate whose
    poll timed out has no outcome at all. Only a row that finished counts
    as released, and only a genuinely empty list counts as clear."""
    results = []
    for code, qty in rows:
        state, result = _op(k, "deactivate", [{"activationCode": code, "quantity": qty}])
        results.append({"code": code, "quantity": qty, "state": state, "ok": _op_ok(state),
                        "running": _op_running(state), "result": result})
    return dict(_holdings(k), results=results, released=all(r["ok"] for r in results),
                running=sum(1 for r in results if r["running"]))


_LICENCE = {"list": _lic_list, "check": _lic_check, "activate": _lic_activate, "release": _lic_release}
LICENCE_ACTIONS = tuple(_LICENCE)


def licences(body, action=None):
    """{action, kvo, user?, password?, verify?, accept_eula?, codes?|rows?}
    against one KVO, through kvo_license.py's own functions; the actions
    are the _lic_* functions above, one per name in LICENCE_ACTIONS. The
    action's own input is validated before anything reaches the KVO, then
    the EULA (when asked), then the login, then the action, each with its
    own answer: a pending EULA that could not be accepted is a 502, a
    login the KVO refuses (401 or 403 from its token endpoint) is a 401
    without the KVO's words, anything else that failed on the way in or
    mid-way is a 502 with them. The password goes into the KVO's token
    request and nowhere else: it is never stored, never in a response,
    and scrubbed from any error text."""
    if not isinstance(body, dict):
        return _err("body must be {action, kvo, user, password, ...}")
    action = action or body.get("action")
    if action not in _LICENCE:
        return _err("action must be one of %s" % ", ".join(LICENCE_ACTIONS))
    kvo = body.get("kvo")
    if not _shape(HOST, kvo):
        return _err("kvo must be the KVO address: an IP or a hostname, no scheme, port or path")
    user, password = body.get("user", "admin"), body.get("password", "admin")
    if not _tag_part(user, MAX_USER) or not _is_str(password, MAX_PASSWORD) or CONTROL.search(password):
        return _err("user and password must be plain strings")
    verify = body.get("verify") is True
    arg = None
    if action in ("check", "activate"):
        arg, bad = _check_codes(body.get("codes"), with_qty=(action == "activate"))
        if bad or not arg:
            return _err(bad or "codes must list at least one activation code")
    elif action == "release":
        arg, bad = _release_rows(body)
        if bad:
            return _err(bad)
    if action == "check" and len(arg) > MAX_CHECK:
        # a lookup per code, each POSTed and polled inside this one request
        return _err("check takes at most %d activation codes in one call, and this asks for %d. Each one is "
                    "a lookup POSTed to the KVO and polled to its end inside this single request, and they "
                    "share one polling budget, so a longer list only gives each code less time to answer. "
                    "Check them in batches." % (MAX_CHECK, len(arg)))
    if action in ("activate", "release") and len(arg) > MAX_OPS:
        # each row is an operation POSTed and polled to its end, and they run
        # one after another inside this one request
        return _err("%s takes at most %d activation code%s in one call, and this asks for %d. Each one is "
                    "a licensing operation POSTed and polled to its end, so a longer list is a single "
                    "request held open for as long as all of them take. Send them in batches."
                    % (action, MAX_OPS, "" if MAX_OPS == 1 else "s", len(arg)))
    KL = _kvo_license()
    if body.get("accept_eula") is True and not KL.accept_eula(kvo, verify):
        # a fresh KVO redirects every request, the token endpoint included,
        # to its EULA page until the EULA is accepted; a login attempted
        # now would fail with a JSON error on an HTML body and read as a
        # broken KVO rather than an unsigned agreement
        return _err("the KVO's EULA is pending and could not be accepted", 502)
    try:
        tok = KL.token(kvo, user, password, verify)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _err("KVO rejected the username or password", 401)
        return _err("KVO login failed: %s" % _scrub(exc, password), 502)
    except Exception as exc:  # noqa: urllib raises several; none carries the password
        return _err("KVO login failed: %s" % _scrub(exc, password), 502)
    try:
        return _LICENCE[action](_Kvo(KL, kvo, tok, verify), arg)
    except Exception as exc:  # noqa: a KVO that stopped answering mid-way
        return _err("KVO licensing call failed: %s" % _scrub(exc, password), 502)


def _scrub(exc, password):
    text = "%s: %s" % (type(exc).__name__, exc)
    return text.replace(password, "***") if password else text


# --------------------------------------------------------------- operate
# A cell is one field of the Operate screen: what was read, or why it could
# not be, with the command the operator can run instead. Never both, and
# never a zero standing in for "I could not tell": a fabricated count on
# this screen is how an operator concludes a stack is idle and tears it
# down. `detail` rides beside a value where a count needs a second number.
def _cell(value, **detail):
    c = {"value": value}
    c.update(detail)
    return c


def _blind(reason, command):
    return {"unavailable": reason, "command": command}


def phase_order():
    """The phases deploy-stack.sh can run, in its order, read from the
    script's own PHASE_ORDER line at call time.

    There is exactly one home for this list and it is the script. A copy
    here (or in the page) goes stale the first time a phase is added
    there, and /api/run would then refuse a phase the script knows, or
    accept one it does not. The Watch screen gets the same list a
    different way (the script's `phases` event, which is selected_phases()
    of this), so both sides read the script and neither reads the other.
    A script that does not say returns [], which refuses every --only."""
    try:
        with open(DEPLOY, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return []
    m = re.search(r'^PHASE_ORDER="([^"]+)"$', src, re.M)
    return m.group(1).split() if m else []


# The vController login the CLI already wrote, mode 600 (deploy-stack.sh's
# VC_CREDS_FILE, same default and same override). It is the ONLY credential
# a GET here may use: /api/status carries no password, because a password in
# a query string is in the browser's history, the referrer and every log on
# the way. When the file is not there the answer is where to log in, not a
# field asking for one.
VC_CREDS_FILE = (os.environ.get("CLOUDLENS_VC_CREDS_FILE")
                 or os.path.join(os.path.expanduser("~"), ".cloudlens-vcontroller-creds.json"))
VC_API = "/cloudlens/api/v1"        # vcontroller_project_key.py's API_ROOT, confirmed live on 6.14.1
VC_TIMEOUT = 15
# the vPB's management SSH, as deploy-stack.sh reports it: port 9022, the
# EC2 key pair, and the counters command its own summary prints
VPB_SSH_PORT = os.environ.get("CLOUDLENS_VPB_SSH_PORT") or "9022"
VPB_USER = "admin"
VPB_COUNTERS = 'sudo vpb -c "show traffic-rule-packet-counters"'
SSH_TIMEOUT = 25
MAX_TEXT = 4000                     # characters of command output carried back
# every CloudFormation status but DELETE_COMPLETE: a stack in any of these
# is still in the region, which is what a post-teardown check is asking
STACK_ALIVE = ["CREATE_IN_PROGRESS", "CREATE_FAILED", "CREATE_COMPLETE", "ROLLBACK_IN_PROGRESS",
               "ROLLBACK_FAILED", "ROLLBACK_COMPLETE", "DELETE_IN_PROGRESS", "DELETE_FAILED",
               "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_IN_PROGRESS",
               "UPDATE_ROLLBACK_FAILED", "UPDATE_ROLLBACK_COMPLETE", "REVIEW_IN_PROGRESS"]


def _vc_call(method, url, token=None, body=None, timeout=None):
    """One vController REST call as (status, parsed body). stdlib urllib,
    like kvo_license.py; the appliance serves a self-signed certificate, so
    the context does not verify it (the script's own probes are curl -k).
    Any transport failure is (0, str(exc)): the caller turns it into the
    field's own `unavailable`, never an exception out of a GET."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        # the scheme is literally "jwt" on this product, not Bearer
        req.add_header("Authorization", "jwt " + token)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout or VC_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw) if raw else None
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: urllib raises several; none carries the password
        return 0, "%s: %s" % (type(exc).__name__, exc)


def _dig(obj, *names):
    """The first value under any of `names`, at any depth. The login
    payload's shape has moved between releases (vcontroller_project_key.py
    says so and does the same), so the key is searched for, not assumed."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and isinstance(v, (str, int)) and not isinstance(v, bool):
                return str(v)
        for v in obj.values():
            found = _dig(v, *names)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _dig(v, *names)
            if found:
                return found
    return None


_SENSOR_KEYS = ("agentcount", "sensorcount", "agents", "sensors", "agent_count", "sensor_count")


def _url_host(url):
    """The host a credentials file's url names, or None when it names none.

    An EXACT host is the only safe comparison here. The test used to be
    `vc_ip not in creds["url"]`, and a substring is not a host: a file for
    https://3.1.1.10/cloudlens/login passed the check against a stack whose
    vController is 3.1.1.1, so that file's password would have been POSTed
    to the other box and the other box's sensor count reported as this
    stack's. Consecutive elastic IPs make that exact pair ordinary.

    A url with no scheme, one with no host, or one urlsplit cannot parse
    names no host: this returns None and the caller refuses rather than
    guessing which part of the string is an address and sending a password
    to it.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        if not parts.scheme or not parts.netloc:
            return None
        return parts.hostname or None
    except ValueError:      # a malformed authority: a bad IPv6 literal, a bad port
        return None


def _creds():
    """(creds, error): the CLI's vController credentials file as a dict."""
    path = VC_CREDS_FILE
    if not os.path.isfile(path):
        return None, ("no vController credentials file at %s: the deploy writes it (mode 600) when it "
                      "mints the project key" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "the credentials file %s could not be read: %s" % (path, type(exc).__name__)
    if not isinstance(data, dict) or not data.get("url") or not data.get("password"):
        return None, "the credentials file %s carries no url and password" % path
    return data, None


def _sensors_cell(vc_ip):
    """Sensors registered, through the vController's own REST API with the
    credentials file the CLI wrote. Every path that is not a number is an
    `unavailable` naming the reason and the UI to log into: the API's
    verified calls are the login and the project list, and if the project
    payload carries no count then this console does not know one."""
    ui = "https://%s/cloudlens/login" % vc_ip if vc_ip else "the vController UI"
    look = "open %s and log in: the project page lists the VMs whose sensors have registered" % ui
    if not vc_ip:
        return _blind("the stack's vController address is not known, so nothing can be asked", look)
    creds, bad = _creds()
    if bad:
        return _blind(bad, look)
    host = _url_host(creds.get("url"))
    if not host:
        return _blind("the credentials file's url names no vController host (%s), so there is nothing to "
                      "match against this stack's %s and its password is not sent anywhere"
                      % (str(creds.get("url", ""))[:MAX_VALUE], vc_ip), look)
    if host != vc_ip:
        return _blind("the credentials file is for another vController (%s), not this stack's %s"
                      % (creds.get("url", ""), vc_ip), look)
    base = "https://%s%s" % (vc_ip, VC_API)
    code, body = _vc_call("POST", base + "/identity/login",
                          body={"Email": creds.get("username", "admin"), "Password": creds["password"]})
    if code == 0:
        # a transport failure, not an answer: the exception's own words,
        # with the file's password scrubbed out of them for the same reason
        # licences() scrubs the KVO's
        return _blind("the vController could not be reached: %s"
                      % str(body).replace(creds["password"], "***"), look)
    if code != 200 or not isinstance(body, dict):
        return _blind("the vController did not accept the saved login (HTTP %s)" % code, look)
    token = _dig(body, "jwttoken", "token", "jwt", "access_token")
    account = _dig(body.get("Accounts"), "id") if isinstance(body.get("Accounts"), dict) else None
    account = account or _dig(body, "account_id", "accountid")
    if not token or not account:
        return _blind("the vController logged in but named no token and account to read projects with", look)
    code, rows = _vc_call("GET", "%s/mgmt/accounts/%s/projects" % (base, account), token=token)
    if code != 200:
        return _blind("the vController refused the project list (HTTP %s)" % code, look)
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    if not isinstance(rows, list):
        return _blind("the vController's project list was not a list", look)
    # the project the creds file names is looked at first; a row that is not
    # an object is not a project and is skipped, never sorted on
    want = creds.get("project")
    for row in sorted([r for r in rows if isinstance(r, dict)], key=lambda r: r.get("name") != want):
        for key in row:
            if key.lower() in _SENSOR_KEYS and isinstance(row[key], int) and not isinstance(row[key], bool):
                return _cell({"sensors": row[key], "project": row.get("name") or row.get("project_name") or "",
                              "vcontroller": vc_ip})
    return _blind("the vController answered (%d project%s) but its payload carries no sensor count"
                  % (len(rows), "" if len(rows) == 1 else "s"), look)


def _key_pem(key_name):
    """The private key for an EC2 key pair, in the places deploy-stack.sh's
    own doctor looks, in its order. None when it is on another machine."""
    if not key_name:
        return None
    home = os.path.expanduser("~")
    for cand in (os.environ.get("CLOUDLENS_KEY_PEM", ""),
                 os.path.join(home, ".ssh", key_name + ".pem"),
                 os.path.join(home, "Downloads", key_name + ".pem"),
                 os.path.join(home, key_name + ".pem"),
                 os.path.join(REPO, key_name + ".pem"),
                 os.path.join(home, "Downloads", key_name + ".cer"),
                 os.path.join(home, ".ssh", key_name)):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _vpb_cell(vpb):
    """The vPB's own packet counters over SSH, which is the only way to
    them: the box answers on port 9022 with the EC2 key pair. Without that
    key on this machine there is no count to report and no way to get one
    from here, so the field carries the exact command instead of a blank."""
    ip = (vpb or {}).get("public_ip") or (vpb or {}).get("private_ip") or ""
    key_name = (vpb or {}).get("key") or ""
    shown = "ssh -i %s -p %s %s@%s '%s'" % (
        "~/.ssh/%s.pem" % key_name if key_name else "<key>.pem", VPB_SSH_PORT, VPB_USER, ip or "<vpb-ip>",
        VPB_COUNTERS)
    if not vpb:
        return _blind("this stack has no running instance named <stack>-vpb", shown)
    if not ip:
        return _blind("the vPB has no address in this account's answer", shown)
    pem = _key_pem(key_name)
    if not pem:
        return _blind("%s.pem is not on this machine (looked in ~/.ssh, ~/Downloads, ~ and the repo, the "
                      "places the doctor looks), so there is no way to reach the vPB from here"
                      % (key_name or "the key pair's"), shown)
    shown = "ssh -i %s -p %s %s@%s '%s'" % (pem, VPB_SSH_PORT, VPB_USER, ip, VPB_COUNTERS)
    argv = ["ssh", "-i", pem, "-p", str(VPB_SSH_PORT), "-n",
            "-o", "BatchMode=yes",          # a key that does not fit must fail, never ask for a password
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8", "%s@%s" % (VPB_USER, ip), VPB_COUNTERS]
    # the same shape as doctor(): its own session, so the timeout ends the
    # WHOLE group and not the leader alone. ssh starts no grandchild today,
    # but a leader-only kill leaves anything it did start holding the pipe
    # this reads, and that is the bug doctor() already carries a test for
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace",
                                start_new_session=True)
    except OSError as exc:
        return _blind("ssh could not be run: %s" % (getattr(exc, "strerror", None) or exc), shown)
    try:
        out, err = proc.communicate(timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return _blind("the vPB did not answer within %ds" % SSH_TIMEOUT, shown)
    if proc.returncode != 0:
        lines = [l for l in ((err or "") + (out or "")).splitlines() if l.strip()]
        return _blind(lines[-1].strip() if lines else "ssh exited %d" % proc.returncode, shown)
    return _cell({"text": (out or "").strip()[:MAX_TEXT], "command": shown})


def _role(name, stack):
    """The part of a Name tag after the stack's own name: vcontroller, kvo,
    vpb, and whatever else a deploy tagged with the same prefix."""
    return name[len(stack) + 1:] if name.startswith(stack + "-") else ""


def status(stack, region):
    """One read-only look at a deployed stack: {stack, region, phases,
    profile, instances, sensors, mirror, vpb}.

    Every field but the first four is a cell that carries EITHER a value OR
    the reason it has none and the command that would get one. Nothing here
    guesses and nothing invents a count. It takes no credential: the only
    one it may use is the file the CLI already wrote (VC_CREDS_FILE), never
    a query parameter.

    `phases` is the script's own PHASE_ORDER, which is what the Operate
    screen's re-run offers; it does not need AWS, so it is answered even
    when every probe fails."""
    if not stack_ok(stack):
        return _err("stack must be a CloudFormation stack name (letters, digits and hyphens, starting with a letter)")
    bad = _check_region(region)
    if bad:
        return bad
    profile = "deploy-profile-%s.env" % stack
    out = {"stack": stack, "region": region, "phases": phase_order(),
           "profile": {"file": profile, "present": os.path.isfile(os.path.join(REPO, profile))}}
    inst_cmd = ("aws ec2 describe-instances --filters Name=tag:Name,Values=%s-* --region %s" % (stack, region))
    rows, by_role = [], {}
    try:
        data = _aws(["ec2", "describe-instances", "--filters", _filters(("tag:Name", [stack + "-*"]))], region)
        for res in data.get("Reservations", []):
            for i in res.get("Instances", []):
                state = (i.get("State") or {}).get("Name", "")
                if state == "terminated":
                    continue        # a terminated instance is not part of a stack that is up
                name = _name_tag(i.get("Tags"))
                row = {"id": i.get("InstanceId", ""), "name": name, "role": _role(name, stack), "state": state,
                       "type": i.get("InstanceType", ""), "key": i.get("KeyName", "") or "",
                       "az": (i.get("Placement") or {}).get("AvailabilityZone", ""),
                       "private_ip": i.get("PrivateIpAddress", "") or "",
                       "public_ip": i.get("PublicIpAddress", "") or ""}
                rows.append(row)
                # one instance per role: the first, except that a running
                # one beats a stopped one. teardown.js picks the kvo row by
                # the same rule (kvoRow), so the appliance this console
                # licenses against and the appliance it matches a release to
                # are the same instance; taking the first here and the last
                # there was two answers to one question.
                prev = by_role.get(row["role"])
                if prev is None or (prev["state"] != "running" and state == "running"):
                    by_role[row["role"]] = row
        # by_role is computed from EVERY instance and travels with the
        # answer, because `rows` is only the first MAX_ROWS of them.
        # teardown.js asked "does this stack have a KVO, and at what
        # address" of the truncated list: a stack of more than 50 live
        # instances whose KVO sorted past the cut answered no KVO, which
        # draws no banner at all and arms the teardown silently. The one
        # side that has seen all the rows is this one, so it says.
        out["instances"] = _cell({"count": len(rows), "rows": rows[:MAX_ROWS],
                                  "truncated": len(rows) > MAX_ROWS, "by_role": by_role})
    except AwsError as exc:
        out["instances"] = _blind(str(exc), inst_cmd)
    out["sensors"] = _sensors_cell((by_role.get("vcontroller") or {}).get("public_ip", ""))
    try:
        mirror = _aws(["ec2", "describe-traffic-mirror-sessions"], region)
        out["mirror"] = _cell({"sessions": len(mirror.get("TrafficMirrorSessions", [])),
                               "scope": "every mirror session in the region: KVO's carry no stack tag, so "
                                        "they cannot be counted per stack from here"})
    except AwsError as exc:
        out["mirror"] = _blind(str(exc), "aws ec2 describe-traffic-mirror-sessions --region " + region)
    out["vpb"] = _vpb_cell(by_role.get("vpb"))
    return out


def verify_empty(region):
    """What is still in a region: the proof to read after a teardown.

    Read-only, and region-wide, which it says: the counts are of everything
    in the region and not only one stack's, because a teardown's own sweep
    is what ties a resource to a stack and this is the check that it
    worked. `empty` is True when every count is zero, False when one is
    not, and None when any probe could not answer: a region is never
    called empty on a question that got no answer."""
    bad = _check_region(region)
    if bad:
        return bad
    out = {"region": region}

    def count(name, args, pick, command):
        try:
            data = _aws(args, region)
        except AwsError as exc:
            out[name] = _blind(str(exc), command)
            return
        value, extra = pick(data)
        out[name] = _cell(value, **extra) if extra else _cell(value)

    def instances(d):
        n = sum(1 for r in d.get("Reservations", []) for i in r.get("Instances", [])
                if (i.get("State") or {}).get("Name") != "terminated")
        return n, None

    def vpcs(d):
        return sum(1 for v in d.get("Vpcs", []) if not v.get("IsDefault")), None

    def volumes(d):
        vols = d.get("Volumes", [])
        return len(vols), {"detail": {"available": sum(1 for v in vols if v.get("State") == "available"),
                                      "gb": sum(int(v.get("Size") or 0) for v in vols)}}

    count("instances", ["ec2", "describe-instances"], instances,
          "aws ec2 describe-instances --region " + region)
    count("vpcs", ["ec2", "describe-vpcs"], vpcs, "aws ec2 describe-vpcs --region " + region)
    count("volumes", ["ec2", "describe-volumes"], volumes, "aws ec2 describe-volumes --region " + region)
    count("enis", ["ec2", "describe-network-interfaces"],
          lambda d: (len(d.get("NetworkInterfaces", [])), None),
          "aws ec2 describe-network-interfaces --region " + region)
    count("mirror_sessions", ["ec2", "describe-traffic-mirror-sessions"],
          lambda d: (len(d.get("TrafficMirrorSessions", [])), None),
          "aws ec2 describe-traffic-mirror-sessions --region " + region)
    count("stacks", ["cloudformation", "list-stacks", "--stack-status-filter"] + STACK_ALIVE,
          lambda d: (len(d.get("StackSummaries", [])), None),
          "aws cloudformation list-stacks --region " + region)
    cells = [out[k] for k in ("instances", "vpcs", "volumes", "enis", "mirror_sessions", "stacks")]
    if any("unavailable" in c for c in cells):
        out["empty"] = None
    else:
        out["empty"] = all(c["value"] == 0 for c in cells)
    return out
