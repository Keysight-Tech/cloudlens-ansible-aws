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
import errno
import queue
import shutil
import signal
import time
import tempfile
import threading
import subprocess

from . import events as E
from . import flows as F

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POLL_SECS = 4
TAIL_SECS = 0.25        # how often run_engine re-reads the script's events file
STOP_GRACE_SECS = 3.0   # TERM to the process group, then KILL after this long
ANSWER_WAIT_SECS = 10.0  # how long answer() waits for the script to open the FIFO


class Job:
    def __init__(self, job_id, flow_id, inputs):
        self.id = job_id
        self.flow_id = flow_id
        self.inputs = inputs
        self.q = queue.Queue()
        self.buffer = []          # every event emitted, for SSE Last-Event-ID replay
        self.done = False
        self.stopped = False
        self.pending_prompt = None  # the script's id of the prompt waiting for an answer
        self.events_path = None     # the --events file run_engine gave the script
        self.pipe_path = None       # the --prompt-pipe FIFO run_engine created
        self._proc = None
        self._t0 = time.time()

    def emit(self, ev):
        self.buffer.append(ev)
        self.q.put(ev)
        if ev["type"] in (E.DONE, E.ERROR):
            self.done = True
        elif ev["type"] == E.PROMPT:
            # the script's own id (from_script filed it as prompt_id): what
            # answer() pairs the reply with, so a reply meant for an earlier
            # question after a reconnect cannot land on this one
            self.pending_prompt = ev.get("prompt_id")

    def elapsed(self):
        return int(time.time() - self._t0)

    def alive(self):
        """True while the engine process is still running."""
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        """Cancel the run. The signal goes to the process GROUP, not the pid:
        deploy-stack.sh asks its questions from inside x="$(ask ...)"
        subshells, and a run blocked on a prompt is really that subshell
        blocked on the FIFO with bash waiting for it. A TERM to the leader
        alone leaves the subshell there, the stdout pipe open and the run
        never ending; run_engine started the child in its own session, so
        its pid is its pgid and the whole tree is one group. A run that
        ignores TERM gets KILL after STOP_GRACE_SECS."""
        self.stopped = True
        if not self.alive():
            return
        if not self._signal_group(signal.SIGTERM):
            try:
                self._proc.terminate()
            except Exception:
                pass
        t = threading.Timer(STOP_GRACE_SECS, self._escalate)
        t.daemon = True
        t.start()

    def _escalate(self):
        if self.alive() and not self._signal_group(signal.SIGKILL):
            try:
                self._proc.kill()
            except Exception:
                pass

    def _signal_group(self, sig):
        # alive() first: a reaped leader's pgid may already belong to someone else
        if not self.alive():
            return True
        try:
            os.killpg(self._proc.pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def answer(self, prompt_id, text):
        """Answer the prompt the script is waiting on: open the FIFO for
        writing, write exactly one line, close. That is the script's contract
        (the comment above ask() in deploy-stack.sh): a write end held open
        between answers reads as an empty answer and takes the default.

        prompt_id has to be the prompt that is waiting. A page that
        reconnects can replay an old question, and its answer must not be
        taken for the current one; the API surfaces the ValueError.

        The FIFO is opened non-blocking and retried: a blocking open would
        hang this thread forever if the script died between emitting the
        prompt and reading the pipe, and the window between those two is
        real (ENXIO, no reader yet) even while it is alive."""
        if self.pending_prompt is None:
            raise ValueError("No prompt is waiting for an answer.")
        if prompt_id != self.pending_prompt:
            raise ValueError("The prompt waiting is {}, not {}.".format(self.pending_prompt, prompt_id))
        if "\n" in text or "\r" in text:
            raise ValueError("An answer is one line.")
        if not self.pipe_path:
            raise ValueError("This job has no engine to answer.")
        deadline = time.time() + ANSWER_WAIT_SECS
        while True:
            try:
                fd = os.open(self.pipe_path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise ValueError("The prompt pipe is gone ({}); the run has ended.".format(
                        exc.strerror))
                if not self.alive():
                    raise ValueError("The engine exited before it read the answer.")
                if time.time() > deadline:
                    raise ValueError("The engine never opened the prompt pipe.")
                time.sleep(0.05)
        try:
            data = (text + "\n").encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
        self.pending_prompt = None


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
    if typ in E.SCRIPT_TYPES and "script_seq" in data:
        # a frame deploy-stack.sh wrote (events v2): keep every field as it
        # is, minus a console id the fixture may still carry: from_script
        # reads "id" as the script's own and would file it as <type>_id
        d = dict(data, type=typ)
        d.pop("id", None)
        return E.from_script(d)
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
    # No stdin and no controlling terminal, on purpose. deploy-stack.sh
    # re-attaches /dev/tty whenever stdin is not a terminal and then decides
    # the run is INTERACTIVE, so a deploy launched from a console that was
    # itself started in a terminal inherited that terminal and sat on
    # "Proceed with this plan? [Y/n]" with every input already supplied.
    # start_new_session detaches the child from the terminal, so the
    # /dev/tty re-attach cannot happen and every ask() takes its default;
    # DEVNULL makes the script's raw reads see EOF instead of blocking.
    job._proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
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


# ---------------------------------------------------------------- engine
def run_engine(job, cmd, cwd=None, env=None):
    """Run a deploy-stack.sh style command with --events/--prompt-pipe wired to
    this job, tailing the events file into the stream while the process runs.
    `cmd` is the argv WITHOUT the two flags; they are appended here so every
    caller gets them right. Process exit without a done event is terminal.

    Three producers feed job.emit: the script's events file (a thread that
    re-reads it every TAIL_SECS), the merged stdout/stderr (this thread, one
    log per line) and, once the process has exited and the tail has done its
    last read, the closing verdict. The verdict is decided only after the
    tail joined: a done the script wrote in its final milliseconds must win
    over "engine exited 0", and one written before a group kill (there is
    none: the tee dies first) would have to win over the stop sentence.

    Exit without a done is terminal because the script's contract says so:
    after a group kill there is no done event, and a run whose console went
    away would otherwise block forever. So the closing event is: the stop
    sentence when the operator stopped it, an error naming the exit code
    when it failed, and a done that says only that the engine exited when
    it returned 0 without saying anything itself. Returns the exit code.
    """
    work = tempfile.mkdtemp(prefix="cloudlens-console-{}-".format(job.id))
    job.events_path = os.path.join(work, "events.jsonl")
    job.pipe_path = os.path.join(work, "prompts.fifo")
    with open(job.events_path, "a"):
        pass
    os.mkfifo(job.pipe_path, 0o600)
    argv = list(cmd) + ["--events", job.events_path, "--prompt-pipe", job.pipe_path]
    penv = dict(os.environ, PYTHONUNBUFFERED="1")
    if env:
        penv.update(env)
    exited = threading.Event()
    tail = threading.Thread(target=_tail_events, args=(job, exited), daemon=True)
    try:
        # Same rules as _stream_subprocess: no stdin (a raw read sees EOF, not
        # a hang), no controlling terminal (the script's /dev/tty re-attach
        # cannot happen, and the console's own Ctrl-C does not reach it), and
        # its own session, which is what makes stop()'s group kill possible.
        job._proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
            text=True, bufsize=1, env=penv,
        )
        tail.start()
        # read to EOF, stopped or not: after a group kill EOF is how the pipe
        # closes, and the lines before it are the last thing the run said
        for line in job._proc.stdout:
            line = line.rstrip("\n")
            if line:
                job.emit(E.log(line))
        rc = job._proc.wait()
    finally:
        exited.set()
        if tail.is_alive():
            tail.join()
        job.pending_prompt = None
        shutil.rmtree(work, ignore_errors=True)
    if not job.done:
        if job.stopped:
            job.emit(E.error("Stopped by operator.", fix="Reload to start over."))
        elif rc != 0:
            job.emit(E.error("engine exited {}".format(rc),
                             fix="Read the console output above for the failing step."))
        else:
            job.emit(E.done("engine exited 0"))
    return rc


def _tail_events(job, exited):
    """Re-read the script's events file every TAIL_SECS and emit what is new,
    until the process has exited AND one read started after that exit: a
    read that began before the exit can miss the line the script wrote on
    its way out, so `final` is sampled before the read, never after.

    last_seq is the watermark iter_script_events uses to tell a replaced
    file from an appended one. It is the LATEST int script_seq handled, not
    the largest ever seen: after a restart the new file's seqs begin at 1,
    and a watermark stuck at the old maximum would make every later read
    look like yet another replacement and re-read the file forever. Only an
    int (not a bool, not a string the script may have written) becomes the
    watermark; comparing anything else would raise in this thread."""
    offset, last_seq = 0, None
    while True:
        final = exited.is_set()
        offset, evs = E.iter_script_events(job.events_path, offset, last_seq)
        for ev in evs:
            seq = ev.get("script_seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                last_seq = seq
            job.emit(ev)
        if final:
            return
        exited.wait(TAIL_SECS)


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
            # Both branches are guarded on job.done, not just the second: a
            # stop that arrives after the fixture's own done frame must not
            # append a cancellation to a run that had already ended.
            if not job.done:
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


def _stack_cmd(job, stack, region):
    """Build the deploy-stack.sh argv.

    Three things the script insists on that are easy to get wrong, and did
    get wrong: the flag is --stack-name (--stack is rejected outright), the
    toggles are bare booleans (--with-kvo / --no-kvo) not "--kvo yes", and
    --key-name has to be supplied. Omit the key and the script falls into
    select_key_pair, which prompts through ask() with defaults, so without
    a console pipe an empty stdin takes the default and mints a key pair
    named cloudlens-key that the visitor never chose. Requiring the name
    here is what stops that. --no-sensors because sensors are their own
    flow here.
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
            "would silently create a key pair named cloudlens-key that you "
            "never chose.")
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
    rc = _stream_subprocess(job, cmd, REPO_ROOT, on_line=lambda l: None)
    time.sleep(POLL_SECS + 1)  # let the poller catch the final CREATE_COMPLETE
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
        cmd = ["python3", S("kvo_aws_mirror.py"),
               "--vpc-id", i.get("vpc", ""),
               "--source-tag", i.get("tag", "cloudlens=yes"),
               "--zone", i.get("az", "us-east-1a"), "--accept-eula", "--insecure"]
        # MIRROR has no kvo input, so this was always `--kvo ""`: an empty
        # token argparse accepted as a value and the script fell over on
        # later. Omitting the flag lets argparse say "--kvo is required".
        kvo = (i.get("kvo") or "").strip()
        if kvo:
            cmd += ["--kvo", kvo]
        # The UI asks for a tool IP and used to throw the answer away, so the
        # mirror had nowhere to forward to and the field was decoration.
        tool = (i.get("tool") or "").strip()
        if tool:
            cmd += ["--tool-remote-ip", tool]
        return cmd
    return ["echo", "no command for flow {}".format(flow["id"])]
