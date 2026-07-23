"""Runs a deployment and turns REAL progress into events.

Two producers, merged into one per-job queue the SSE route drains:
  1. the deploy SUBPROCESS  - line-by-line stdout of the repo scripts
  2. the boto3 POLLER        - real CloudFormation stack events / instance state

Honesty rule: every `state` event comes from a real AWS transition or a real log
line. When we are only waiting, we emit a `stat(waiting=True)` - never a fake
`state`. A `--replay` fixture (real captured events) drives the whole UI with no
AWS calls, for offline demo/dev/test; it is real data, just replayed.
"""
from __future__ import annotations
import os
import json
import queue
import time
import threading
import subprocess

from . import events as E
from . import flows as F

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POLL_SECS = 4


class Job:
    def __init__(self, job_id, flow_id, inputs):
        self.id = job_id
        self.flow_id = flow_id
        self.inputs = inputs
        self.q = queue.Queue()
        self.buffer = []          # for SSE Last-Event-ID replay
        # done/done_at are the JOB's lifecycle, and only finish() sets them.
        # An E.ERROR event is NOT the end of a job: _poll_cfn emits one per
        # failed resource and keeps polling, so a stack that loses a resource
        # in the first minute can still be running twenty minutes later. When
        # emit() inferred the lifecycle from the event type, that job counted
        # as finished, became prunable, and /stop/<id> then answered ok:true
        # while the deploy carried on - the operator loses the cancel and is
        # told it worked. done_at is None for as long as the job may still act.
        self.done = False
        self.done_at = None
        # Bookkeeping, not lifecycle: whether any terminal-shaped event has
        # gone out, so a flow does not append a second summary after one.
        self.saw_terminal_event = False
        self.stopped = False
        self._proc = None
        self._t0 = time.time()

    def emit(self, ev):
        self.buffer.append(ev)
        self.q.put(ev)
        if ev["type"] in (E.DONE, E.ERROR):
            self.saw_terminal_event = True

    def finish(self):
        """The job will not act again: no more events, no process to cancel.

        The single place done/done_at are set. Called from run_job's finally,
        which covers the ordinary returns, the handled ValueError, the
        catch-all, and an unexpected BaseException alike - a job that escaped
        all four would otherwise stay un-prunable for the life of the process.
        """
        self.done = True
        if self.done_at is None:
            self.done_at = time.time()

    def elapsed(self):
        return int(time.time() - self._t0)

    def stop(self):
        self.stopped = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ---------------------------------------------------------------- preflight
def preflight(region=None):
    """Return (account, arn, region) from the shell's real AWS identity, or raise
    a ValueError with a fix the UI can show. boto3 is imported lazily so replay
    mode needs neither boto3 nor credentials."""
    try:
        import boto3  # noqa
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise ValueError("boto3 is not installed. Run: pip install boto3")
    try:
        sess = boto3.session.Session(region_name=region)
        ident = sess.client("sts").get_caller_identity()
        reg = region or sess.region_name or "us-east-1"
        return ident["Account"], ident["Arn"], reg
    except (BotoCoreError, ClientError, Exception) as exc:  # noqa
        raise ValueError(
            "No usable AWS credentials ({}). Run `aws sso login` (or set your "
            "profile / env vars), then reload.".format(type(exc).__name__)
        )


# ---------------------------------------------------------------- replay
def _run_replay(job, fixture_path):
    with open(fixture_path) as fh:
        frames = json.load(fh)
    for fr in frames:
        if job.stopped:
            return
        time.sleep(min(fr.get("_delay", 0.6), 2.5))
        ev = dict(fr["event"])
        typ = ev.pop("type")
        ev.pop("id", None)
        # rebuild through events.py so ids stay freshly sequenced for SSE replay
        job.emit(_rebuild(typ, ev))


def _rebuild(typ, data):
    # reconstruct a typed event through events.py so ids are freshly sequenced
    if typ == E.LOG:
        return E.log(data.get("text", ""), data.get("stream", "out"))
    if typ == E.STATE:
        return E.state(data["node"], data["status"], data.get("label"))
    if typ == E.NARRATE:
        return E.narrate(data.get("text", ""), data.get("tone", "info"))
    if typ == E.STAT:
        return E.stat(**{k: v for k, v in data.items() if k not in ("id", "type")})
    if typ == E.DONE:
        return E.done(data.get("summary", ""), data.get("outputs"))
    if typ == E.ERROR:
        return E.error(data.get("text", ""), data.get("node"), data.get("fix"))
    if typ == "hello":
        return E.hello(data.get("account", ""), data.get("arn", ""), data.get("region", ""))
    return E.log(json.dumps(data))


# ---------------------------------------------------------------- subprocess
def _stream_subprocess(job, cmd, cwd, on_line):
    job._proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    for line in job._proc.stdout:
        if job.stopped:
            break
        line = line.rstrip("\n")
        if line:
            job.emit(E.log(line))
            on_line(line)
    job._proc.wait()
    return job._proc.returncode


# ---------------------------------------------------------------- cfn poller
def _poll_cfn(job, flow, stack_name, region, stop_evt):
    import boto3
    cf = boto3.session.Session(region_name=region).client("cloudformation")
    rmap = flow["source"]["resource_map"]
    narr = flow["source"]["narrate"]
    seen = set()
    lit = set()
    while not stop_evt.is_set() and not job.stopped:
        try:
            evs = cf.describe_stack_events(StackName=stack_name)["StackEvents"]
        except Exception:
            job.emit(E.stat(waiting=True, note="waiting for the stack to appear"))
            time.sleep(POLL_SECS)
            continue
        for se in reversed(evs):  # oldest first
            eid = se["EventId"]
            if eid in seen:
                continue
            seen.add(eid)
            logical = se.get("LogicalResourceId", "")
            status = se.get("ResourceStatus", "")
            node = next((n for frag, n in rmap.items() if frag in logical), None)
            if node:
                if status.endswith("CREATE_IN_PROGRESS") and node not in lit:
                    # "not in lit" or a finished node goes backwards. The map
                    # matches on a fragment, so several resources share a node:
                    # VpcGatewayAttachment contains "Vpc", and its CREATE_IN_
                    # PROGRESS lands after the VPC's own CREATE_COMPLETE, which
                    # left the VPC showing "creating" for the whole deploy.
                    job.emit(E.state(node, E.BUSY, "creating"))
                elif status.endswith("CREATE_COMPLETE") and node not in lit:
                    lit.add(node)
                    job.emit(E.state(node, E.LIVE, "live"))
                elif "ROLLBACK" in status or "FAILED" in status:
                    reason = se.get("ResourceStatusReason", status)
                    job.emit(E.error(reason, node=node,
                                     fix="Check the stack events; fix the input and redeploy."))
            for (frag, st), (text, tone) in narr.items():
                if frag in logical and st in status:
                    job.emit(E.narrate(text, tone))
            if logical == stack_name and status == "CREATE_COMPLETE":
                stop_evt.set()
        # waiting=False explicitly: reaching here means describe_stack_events
        # answered, so any earlier "waiting for the stack to appear" is stale
        # and the page needs telling, not just an absent field.
        job.emit(E.stat(elapsed=job.elapsed(), created=len(lit), waiting=False))
        time.sleep(POLL_SECS)


# ---------------------------------------------------------------- run
def run_job(job, replay=None):
    flow = F.FLOWS[job.flow_id]
    region = job.inputs.get("region", "us-east-1")
    try:
        if replay:
            job.emit(E.hello("000000000000", "arn:aws:iam::demo:replay", region))
            job.emit(E.narrate("Replay mode - real captured events from a live deploy, no AWS calls.", "note"))
            _run_replay(job, replay)
            # A stopped replay is NOT a completed one. _run_replay returns the
            # moment job.stopped is set, so a run the operator cut short left
            # no terminal event behind and used to be closed with
            # done("Replay complete.") - success reported for a run that was
            # cancelled. Both real deploy paths already say "Stopped by
            # operator."; this is the same sentence, from the same authority.
            #
            # Both branches are guarded on saw_terminal_event, not just the
            # second: a stop that arrives after the fixture's own done frame
            # must not append a cancellation to a run that had already ended.
            if not job.saw_terminal_event:
                if job.stopped:
                    job.emit(E.error("Stopped by operator.", fix="Reload to start over."))
                else:
                    job.emit(E.done("Replay complete."))
            return
        account, arn, reg = preflight(region)
        job.emit(E.hello(account, arn, reg))
        job.emit(E.narrate("Live - deploying into account {} as {}.".format(account, arn.split('/')[-1]), "info"))
        src = flow["source"]
        if src["kind"] == "cfn":
            _run_cfn_flow(job, flow, reg)
        else:
            _run_script_flow(job, flow)
    except ValueError as exc:
        job.emit(E.error(str(exc), fix="Fix the item above, then reload the page."))
    except Exception as exc:  # noqa
        job.emit(E.error("Unexpected: {}".format(exc)))
    finally:
        # Every exit from run_job is the end of the job, and only these are:
        # the flow helpers below emit their own closing event and return here.
        # In a finally so a raise cannot leave a job that nothing will ever
        # finish, which the SSE tail and the server's TTL sweep both key on.
        job.finish()


def _stack_cmd(job, stack, region):
    """Build the deploy-stack.sh argv.

    Three things the script insists on that are easy to get wrong, and did
    get wrong: the flag is --stack-name (--stack is rejected outright), the
    toggles are bare booleans (--with-kvo / --no-kvo) not "--kvo yes", and
    --key-name has to be supplied. Omit the key and the script drops into an
    interactive picker, but the console gives it no TTY, so the run hangs
    with nothing on screen instead of failing. --no-sensors because sensors
    are their own flow here.
    """
    i = job.inputs
    cmd = ["bash", os.path.join(REPO_ROOT, "deploy", "deploy-stack.sh"),
           "--stack-name", stack, "--region", region, "--no-sensors"]
    cmd.append("--with-kvo" if _yes(i.get("kvo", "yes")) else "--no-kvo")
    cmd.append("--with-vpb" if _yes(i.get("vpb", "yes")) else "--no-vpb")
    key = (i.get("key") or "").strip()
    if not key:
        raise ValueError(
            "An EC2 key pair name is required: without it the deploy script "
            "stops at an interactive prompt this console cannot answer.")
    cmd += ["--key-name", key]
    return cmd


def _yes(v):
    return str(v).strip().lower() in ("yes", "y", "true", "1", "on")


def _run_cfn_flow(job, flow, region):
    stack = job.inputs.get("stack", "cloudlens-live")
    # Built before the poller starts: a bad argv is the job's fault, not
    # AWS's, and there is no reason to spin up a polling thread for a run
    # that cannot launch.
    cmd = _stack_cmd(job, stack, region)
    for nid in flow["nodes"]:
        job.emit(E.state(nid, E.GHOST))
    stop_evt = threading.Event()
    poller = threading.Thread(target=_poll_cfn, args=(job, flow, stack, region, stop_evt), daemon=True)
    poller.start()
    # Everything from here on is inside the finally: the poller is a thread we
    # started, so stopping it is our obligation on EVERY exit, not only the one
    # where the subprocess ran to completion. Popen raises whenever
    # deploy-stack.sh or bash is missing, and that used to unwind straight past
    # stop_evt.set() into run_job's catch-all: the job was reported, finished
    # and pruned, while _poll_cfn went on calling describe_stack_events every
    # POLL_SECS for the life of the process - a leaked thread making paid AWS
    # calls, refilling job.buffer with nobody to read it and nothing left in
    # JOBS to prune it a second time. job.stopped never rescues it either: the
    # operator's /stop is gone with the job entry.
    try:
        rc = _stream_subprocess(job, cmd, REPO_ROOT, on_line=lambda l: None)
        time.sleep(POLL_SECS + 1)  # let the poller catch the final CREATE_COMPLETE
    finally:
        stop_evt.set()
    if job.stopped:
        job.emit(E.error("Stopped by operator.", fix="Reload to start over."))
    elif rc == 0:
        job.emit(E.done("Stack CREATE_COMPLETE - appliances need ~15 min to initialize.",
                        outputs={"note": "Log in at the vController URL once initialized."}))
    else:
        job.emit(E.error("deploy-stack.sh exited {}".format(rc),
                         fix="Read the console output above for the failing resource."))


def _run_script_flow(job, flow):
    for nid in flow["nodes"]:
        job.emit(E.state(nid, E.GHOST))
    patterns = flow["source"]["patterns"]

    def on_line(line):
        hit = F.match(patterns, line)
        if hit:
            node, status, text, tone = hit
            job.emit(E.state(node, status))
            job.emit(E.narrate(text, tone))

    cmd = _script_cmd(job, flow)
    rc = _stream_subprocess(job, cmd, REPO_ROOT, on_line=on_line)
    if job.stopped:
        job.emit(E.error("Stopped by operator.", fix="Reload to start over."))
    elif rc == 0:
        job.emit(E.done("{} complete.".format(flow["name"])))
    else:
        job.emit(E.error("{} exited {}".format(flow["script"], rc),
                         fix="Read the console output above."))


def _script_cmd(job, flow):
    """Map a flow's inputs to the real repo command. Kept explicit per flow so the
    wrapper never guesses."""
    i = job.inputs
    S = lambda *p: os.path.join(REPO_ROOT, "scripts", *p)
    if flow["id"] == "sensors":
        return ["ansible-playbook", "-i", os.path.join(REPO_ROOT, "inventory", "aws_ec2.yaml"),
                os.path.join(REPO_ROOT, "deploy.yaml"),
                "-e", "cloudlens_ip={}".format(i.get("clms", "")),
                "-e", "project_key={}".format(i.get("key", ""))]
    if flow["id"] == "kvo":
        return ["python3", S("kvo_adopt_clms.py"),
                "--clms", i.get("clms", ""), "--kvo", i.get("kvo", ""),
                "--cloud-config", i.get("cloud", "prod-cloud"), "--accept-eula", "--insecure"]
    if flow["id"] == "mirror":
        return ["python3", S("kvo_aws_mirror.py"),
                "--kvo", i.get("kvo", ""), "--vpc-id", i.get("vpc", ""),
                "--source-tag", i.get("tag", "cloudlens=yes"),
                "--zone", i.get("az", "us-east-1a"), "--accept-eula", "--insecure"]
    return ["echo", "no command for flow {}".format(flow["id"])]
