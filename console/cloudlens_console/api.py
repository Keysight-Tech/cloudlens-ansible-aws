"""The console's API: every route a face on an existing command.

Nothing here knows AWS on its own. Discovery shells out to the aws CLI with
--output json (an argv list, never a shell); the doctor is deploy-stack.sh
--doctor read back through its own events file; a run is deploy-stack.sh
--profile on a file this module wrote from the one allowlist the script
enforces (profile.py); a teardown is teardown-stack.sh with the flags it
documents; licensing is scripts/kvo_license.py's own functions against the
KVO REST API.

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
import subprocess
import tempfile
import threading
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
MAX_ANSWER = 64 * 1024  # bytes of one prompt answer
MAX_VALUE = 4096        # characters of one plan value or secret

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
# an activation code, optionally with the quantity the script's --kvo-codes takes
CODE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
CODE_QTY = re.compile(r"^([A-Za-z0-9-]{4,64})(?:,([0-9]{1,6}))?$")
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

LICENCE_ACTIONS = ("list", "check", "activate", "release")


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
    if ids is None or len(ids) > 50 or any(not _shape(VPC, v) for v in ids):
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
def doctor(region):
    """deploy-stack.sh --doctor --region R --events F, synchronously, read
    back from F: the check events as {item, status, fix}, ok when none
    failed. No --prompt-pipe: the doctor asks nothing, and a pipe with no
    writer would block it. Same rules as the engine: no stdin, no
    controlling terminal (the script re-attaches /dev/tty when it can)."""
    bad = _check_region(region)
    if bad:
        return bad
    work = tempfile.mkdtemp(prefix="cloudlens-console-doctor-")
    events = os.path.join(work, "events.jsonl")
    argv = ["bash", DEPLOY, "--doctor", "--region", region, "--events", events]
    try:
        try:
            proc = subprocess.run(argv, cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="replace", timeout=DOCTOR_TIMEOUT, start_new_session=True)
        except subprocess.TimeoutExpired:
            return _err("the doctor timed out after %ds (it probes GitHub, the template bucket and AWS)"
                        % DOCTOR_TIMEOUT, 504)
        except OSError as exc:
            return _err("could not run deploy-stack.sh: %s" % exc, 500)
        _, evs = E.iter_script_events(events)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    checks = [{"item": e.get("item", ""), "status": e.get("status", ""), "fix": e.get("fix", "") or ""}
              for e in evs if e["type"] == E.CHECK]
    if not checks:
        tail = [l for l in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if l.strip()][-3:]
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


def _check_secrets(secrets):
    """(env, errors): the secrets as the engine's extra environment. A name
    has to be one the script reads a secret from (SECRET_ENV) and can never
    be a profile key; a value is a string the environment can hold. An empty
    value is dropped: the script reads ${NAME:-} and treats it as unset."""
    if not isinstance(secrets, dict):
        return {}, ["secrets must be an object of {NAME: value}"]
    env, errors = {}, {}
    allowed = set(P.allowed_keys())
    for name, value in secrets.items():
        if name in allowed:
            errors[name] = "%s is a profile key, not a secret: put it in the plan" % name
        elif name not in SECRET_ENV:
            errors[name] = "%s is not a secret deploy-stack.sh reads (%s)" % (name, ", ".join(sorted(SECRET_ENV)))
        elif not _is_str(value) or "\x00" in value:
            errors[name] = "%s: a secret is a string of at most %d characters" % (name, MAX_VALUE)
        elif value:
            env[name] = value
    return env, [errors[k] for k in sorted(errors)]


def _check_codes(codes, with_qty=True):
    """(codes, error): activation codes as strings, optionally CODE,QTY."""
    if codes is None:
        return [], None
    if not isinstance(codes, list) or len(codes) > 50:
        return [], "codes must be a list of activation codes"
    rule = CODE_QTY if with_qty else CODE
    out = []
    for c in codes:
        # strip() forgives the whitespace a pasted code carries; what is
        # left has to be the whole code, control bytes included in "not"
        if not isinstance(c, str) or not _shape(rule, c.strip()):
            return [], "codes: %r is not an activation code%s" % (c, " (CODE or CODE,QTY)" if with_qty else "")
        out.append(c.strip())
    return out, None


def run(body, jobs=None, start=None):
    """Write deploy-profile-<stack>.env from the validated plan and start
    deploy-stack.sh --profile on it. The plan goes through plan(); the
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
    {job_id, profile_file, stack, region} or {errors}."""
    if not isinstance(body, dict):
        return {"errors": ["body must be an object: {plan, secrets?, kvo_codes?}"]}
    p = plan(body.get("plan"))
    if p.get("errors"):
        return {"errors": p["errors"]}
    env, errors = _check_secrets(body.get("secrets") or {})
    codes, bad = _check_codes(body.get("kvo_codes"))
    if bad:
        errors.append("kvo_codes: " + bad)
    if errors:
        return {"errors": errors}
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
    for c in codes:
        job.redactions.append(CODE_QTY.fullmatch(c).group(1))   # the code, never the quantity
    job.redactions.extend(env.values())
    (jobs if jobs is not None else _jobs())[job_id] = job
    shown = "bash deploy/deploy-stack.sh --profile %s" % p["profile_file"]
    if codes:
        shown += " --kvo-codes ... (%d code%s)" % (len(codes), "" if len(codes) == 1 else "s")
    if env:
        shown += "   [%s in the environment]" % ", ".join(sorted(env))
    job.emit(E.narrate("engine: " + shown, "note"))
    (start or _start_engine)(job, cmd, REPO, env or None)
    return {"job_id": job_id, "profile_file": path, "stack": p["stack"], "region": p["region"]}


def answer(job, body):
    """{prompt_id, text} -> job.answer. The job's own refusals (no prompt
    waiting, the wrong prompt, an answer already in flight, an engine that
    went away) come back as 409 with its words."""
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
    that deletes nothing, so it needs no typed name."""
    if not isinstance(body, dict):
        return _err("body must be {stack, region, confirm_name, orphans_only?, licences_released?}")
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
    cmd = ["bash", TEARDOWN, "--stack-name", stack, "--region", region, "--yes"]
    if audit:
        cmd.append("--orphans")
    elif body.get("licences_released") is True:
        cmd.append("--accept-licence-loss")
    job_id = uuid.uuid4().hex[:12]
    job = O.Job(job_id, "engine-teardown", {"stack": stack, "region": region, "audit": audit})
    (jobs if jobs is not None else _jobs())[job_id] = job
    job.emit(E.narrate("engine: bash deploy/teardown-stack.sh " + " ".join(cmd[3:]), "note"))
    (start or _start_teardown)(job, cmd, REPO, None)
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


def _op_ok(state):
    """kvo_license.py's own reading of an operation's final state."""
    s = str(state or "").upper()
    return bool(state) and "FAIL" not in s and "ERROR" not in s


def _op(KL, kvo, base, tok, verify, name, payload):
    """POST one licensing operation and poll it to its end."""
    _, resp = KL._req("POST", "%s/api/v2/licensing/operations/%s" % (base, name), tok, payload, verify)
    op = KL.poll_op(kvo, tok, resp, verify)
    state = op.get("state") if isinstance(op, dict) else op
    result = op.get("result") if isinstance(op, dict) else None
    return state, result


def _list(KL, base, tok, verify):
    _, rows = KL._req("GET", base + "/api/v2/licensing/licenses", tok, verify=verify)
    return rows if isinstance(rows, list) else []


def _release_rows(body):
    """(rows, error): [(code, quantity)] from rows [{activationCode|code,
    quantity}] (the shape GET licenses returns) or codes ["CODE,QTY"]."""
    rows = body.get("rows")
    if rows is None and body.get("codes") is not None:
        codes, bad = _check_codes(body.get("codes"))
        if bad:
            return [], bad
        rows = []
        for c in codes:
            code, qty = CODE_QTY.fullmatch(c).groups()
            rows.append({"code": code, "quantity": int(qty) if qty else 0})
    if not isinstance(rows, list) or not rows or len(rows) > 50:
        return [], "rows must list what to release: [{activationCode, quantity}] as GET licenses shows them"
    out = []
    for r in rows:
        code = r.get("activationCode", r.get("code")) if isinstance(r, dict) else None
        qty = r.get("quantity") if isinstance(r, dict) else None
        if not _shape(CODE, code):
            return [], "rows: %r is not an activation code" % (code,)
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            return [], "rows: %s needs the quantity to release (a positive integer)" % code
        out.append((code, qty))
    return out, None


def licences(body, action=None):
    """{action, kvo, user?, password?, verify?, accept_eula?, codes?|rows?}
    against one KVO, through kvo_license.py's own functions:
      list      GET /api/v2/licensing/licenses -> {licences, count}
      check     retrieve-activation-code-info per code -> {codes: [...]}
      activate  activate per code, the available quantity unless CODE,QTY
                says how many; a code with nothing available is skipped, as
                kvo_license.py does -> {results, activated, licences}
      release   operations/deactivate per row, polled -> {results, released
                (every row succeeded), clear (the KVO holds no licence now),
                licences}
    The password goes into the KVO's token request and nowhere else: it is
    never stored, never in a response, and scrubbed from any error text."""
    if not isinstance(body, dict):
        return _err("body must be {action, kvo, user, password, ...}")
    action = action or body.get("action")
    if action not in LICENCE_ACTIONS:
        return _err("action must be one of %s" % ", ".join(LICENCE_ACTIONS))
    kvo = body.get("kvo")
    if not _shape(HOST, kvo):
        return _err("kvo must be the KVO address: an IP or a hostname, no scheme, port or path")
    user, password = body.get("user", "admin"), body.get("password", "admin")
    if not _tag_part(user, 128) or not _is_str(password, 1024) or CONTROL.search(password):
        return _err("user and password must be plain strings")
    verify = body.get("verify") is True
    # validate the action's own inputs before anything reaches the KVO
    codes, rows = [], []
    if action in ("check", "activate"):
        codes, bad = _check_codes(body.get("codes"), with_qty=(action == "activate"))
        if bad or not codes:
            return _err(bad or "codes must list at least one activation code")
    elif action == "release":
        rows, bad = _release_rows(body)
        if bad:
            return _err(bad)
    KL = _kvo_license()
    base = "https://%s" % kvo
    if body.get("accept_eula") is True:
        KL.accept_eula(kvo, verify)
    try:
        tok = KL.token(kvo, user, password, verify)
    except Exception as exc:  # noqa: urllib raises several; none carries the password
        return _err("KVO login failed: %s" % _scrub(exc, password), 502)
    try:
        if action == "list":
            rows = _list(KL, base, tok, verify)
            return {"licences": rows, "count": len(rows)}
        if action == "check":
            out = []
            for code in codes:
                ents, info = KL.lookup_code(kvo, base, tok, code, verify)
                out.append({"code": code, "valid": bool(ents),
                            "state": info.get("state") if isinstance(info, dict) else info,
                            "entitlements": [{"product": p, "available": a, "total": t} for p, a, t in ents]})
            return {"codes": out}
        if action == "activate":
            results = []
            for c in codes:
                code, qty = CODE_QTY.fullmatch(c).groups()
                qty = int(qty) if qty else None
                ents, info = KL.lookup_code(kvo, base, tok, code, verify)
                if not ents:
                    results.append({"code": code, "state": "invalid", "ok": False, "picks": [],
                                    "detail": info.get("state") if isinstance(info, dict) else info})
                    continue
                picks = [(p, qty if qty is not None else a) for p, a, _ in ents if (qty if qty is not None else a)]
                if not picks:
                    results.append({"code": code, "state": "nothing-available", "ok": False, "picks": [],
                                    "entitlements": [{"product": p, "available": a, "total": t} for p, a, t in ents]})
                    continue
                if len(picks) == 1:
                    payload = [{"activationCode": code, "quantity": picks[0][1]}]
                else:
                    payload = [{"activationCode": code, "product": p, "quantity": q} for p, q in picks]
                state, result = _op(KL, kvo, base, tok, verify, "activate", payload)
                results.append({"code": code, "state": state, "ok": _op_ok(state),
                                "picks": [{"product": p, "quantity": q} for p, q in picks], "result": result})
            return {"results": results, "activated": sum(1 for r in results if r["ok"]),
                    "licences": _list(KL, base, tok, verify)}
        results = []
        for code, qty in rows:
            state, result = _op(KL, kvo, base, tok, verify, "deactivate",
                                [{"activationCode": code, "quantity": qty}])
            results.append({"code": code, "quantity": qty, "state": state, "ok": _op_ok(state), "result": result})
        remaining = _list(KL, base, tok, verify)
        return {"results": results, "released": all(r["ok"] for r in results), "clear": not remaining,
                "licences": remaining}
    except Exception as exc:  # noqa: a KVO that stopped answering mid-way
        return _err("KVO licensing call failed: %s" % _scrub(exc, password), 502)


def _scrub(exc, password):
    text = "%s: %s" % (type(exc).__name__, exc)
    return text.replace(password, "***") if password else text
