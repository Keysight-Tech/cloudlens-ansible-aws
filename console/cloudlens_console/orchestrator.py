"""Runs a deployment and turns REAL progress into events.

Two producers, merged into one per-job buffer every SSE reader drains:
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
import fcntl
import itertools
import shutil
import signal
import sys
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
COMPLETED = ("ok", "dry-run")  # the script's done statuses that mean the run finished
REDACTED = "[redacted]"
# what an `answered` frame shows for a secret. Eight asterisks, the same mask
# web/watch.js puts on the card the moment it sends one, so the stream's word
# and the page's agree and a replay does not change what the operator saw.
MASKED = "********"
# scripts/kvo_license.py prints `code[:14]...` per activation code, so a
# registered string longer than this is redacted by its first 14 characters
# as well as whole
REDACT_PREFIX = 14
# The shortest string that may be registered as a redaction.
#
# redact() is a blind substring replace over every string it is given, and
# _redact_event runs it over every string field of every frame the script
# writes. A short needle therefore rewrites words that are not the secret.
# Reproduced: a two-character typo at a secret prompt registered "on", and
# from the next frame on {"type":"done"} went out as
# {"type":"d[redacted]e"}. emit()'s terminal test reads ev["type"], so it
# never fired: the job never went done, _verdict's finally overwrote a
# successful deploy's verdict with an error, and the frame reached the
# browser as an SSE event with no listener for it.
#
# Six characters is the floor. Every secret the console actually carries is
# longer (a vController or KVO password, an AWS secret access key, an
# activation code), and no field name, status word or type in the event
# contract is six characters of a real secret by accident. Anything shorter
# is REFUSED, never dropped: a secret the stream cannot blank safely is one
# the operator has to be told about, because dropping it silently would
# leave it printed in the log.
MIN_REDACTION = 6


class Job:
    def __init__(self, job_id, flow_id, inputs):
        self.id = job_id
        self.flow_id = flow_id
        self.inputs = inputs
        self.buffer = []          # every event emitted, for SSE Last-Event-ID replay
        self.redactions = []      # strings redact() blanks from the stream: codes, secrets
        self._ids = itertools.count(1)  # the event ids, minted by emit under the lock
        self.done = False
        self.stopped = False
        self.pending_prompt = None  # the script's id of the prompt waiting for an answer
        self.pending_kind = None    # that prompt's kind: "secret" answers are never shown
        self.events_path = None     # the --events file run_engine gave the script
        self.pipe_path = None       # the --prompt-pipe FIFO run_engine created
        self._proc = None
        self._pgid = None           # the engine's process group: its pid, own session
        self._group_open = False    # True from Popen until the runner has returned
        self._script_done = None    # the done the script wrote; the runner emits it last
        self._lock = threading.Lock()   # guards the buffer, pending_prompt and _answering
        self._cond = threading.Condition(self._lock)  # emit wakes every SSE reader waiting here
        self._answering = None      # the prompt id an answer() in progress has claimed
        self._t0 = time.time()

    def emit(self, ev):
        """Stamp the next id on the frame and append it, both under the lock:
        the id IS the buffer position, whichever of the producers (the
        events tail, the stdout loop, the verdict) got here first. Minting
        in events.py, before the append, let two threads append out of id
        order, and a resume from the smaller id then skipped the larger;
        the SSE route's max() watermark was the stopgap for that.

        The buffer is the one place an event lives: every SSE reader takes
        what it holds past its own watermark and is woken here (notify_all
        under the same lock, `done` set before it), so any number of tabs on
        one job each see every event. The queue that used to sit beside
        the buffer handed each live event to one reader only."""
        with self._cond:
            ev["id"] = next(self._ids)
            self.buffer.append(ev)
            if ev["type"] == E.PROMPT:
                # the script's own id (from_script filed it as prompt_id):
                # what answer() pairs the reply with, so a reply meant for an
                # earlier question after a reconnect cannot land on this one.
                # The kind rides along because answer() has to know, at the
                # moment it succeeds, whether what it just wrote may be shown
                self.pending_prompt = ev.get("prompt_id")
                self.pending_kind = ev.get("kind")
            if ev["type"] in (E.DONE, E.ERROR):
                self.done = True
            self._cond.notify_all()

    def events_since(self, last_id):
        """The buffered events with an id above last_id, in id order: what a
        reconnecting browser missed. Snapshot under the lock, so an emit in
        flight is either wholly in or wholly out."""
        with self._lock:
            return [ev for ev in self.buffer if ev["id"] > last_id]

    def wait_for_events(self, last_id, timeout):
        """Block until the buffer holds an event with an id above last_id or
        the job is done, or `timeout` seconds pass: True in the first two
        cases, False on the timeout (the SSE route's cue for a keepalive).
        Ids are buffer positions, so "an id above last_id" is a buffer
        longer than last_id; both conditions are read under the lock emit
        sets them under, so a wake is never missed between the check and
        the wait."""
        with self._cond:
            return self._cond.wait_for(lambda: len(self.buffer) > last_id or self.done, timeout)

    def add_redaction(self, value):
        """Register `value` as a string redact() blanks from the stream.

        The one door in: nothing appends to self.redactions directly, so
        the MIN_REDACTION floor cannot be walked around. A value under it
        raises rather than being ignored - see MIN_REDACTION for the run
        that was reported as failed because "on" was registered. An empty
        value is a no-op (there is nothing to blank) and a non-string is
        not a needle."""
        if not isinstance(value, str) or not value:
            return
        if len(value) < MIN_REDACTION:
            raise ValueError(
                "A secret of {} characters cannot be blanked from this run's output: "
                "redaction replaces it wherever it appears, and a string that short "
                "also appears inside the words the console's own frames are made of. "
                "Secrets of at least {} characters are accepted.".format(
                    len(value), MIN_REDACTION))
        self.redactions.append(value)

    def redact(self, text):
        """`text` with every registered string, and the first REDACT_PREFIX
        characters of each one longer than that, replaced by REDACTED.
        The engine's stdout loop and its verdict texts go through here, so
        a script line that prints an activation code (kvo_license.py prints
        14 of its characters; the dry run prints it whole) or a password
        never lands in the buffer or the browser. Longest first, so a whole
        code is blanked before its own prefix could split it; anything that
        is not a string, or a job with nothing registered, passes through
        untouched. Registering a short common word (a default password of
        "admin") blanks that word wherever the run prints it: the operator
        chose to send it as a secret."""
        if not self.redactions or not isinstance(text, str):
            return text
        needles = set()
        for s in self.redactions:
            if isinstance(s, str) and s:
                needles.add(s)
                if len(s) > REDACT_PREFIX:
                    needles.add(s[:REDACT_PREFIX])
        for s in sorted(needles, key=len, reverse=True):
            text = text.replace(s, REDACTED)
        return text

    def elapsed(self):
        return int(time.time() - self._t0)

    def alive(self):
        """True while the engine's LEADER is still running. Not the test for
        whether the run is over: that is running()."""
        return self._proc is not None and self._proc.poll() is None

    def running(self):
        """True from the engine's Popen until its runner has returned: the
        whole window in which its process group can have members. This, not
        the leader's poll(), is what stop() and the console's shutdown gate
        on. A leader that exited while a subshell of its still holds the
        stdout pipe is a run that has not ended, and its group is still
        there to be signalled."""
        return self._group_open

    def stop(self):
        """Cancel the run. The signal goes to the process GROUP, not the pid:
        deploy-stack.sh asks its questions from inside x="$(ask ...)"
        subshells, and a run blocked on a prompt is really that subshell
        blocked on the FIFO with bash waiting for it. A TERM to the leader
        alone leaves the subshell there, the stdout pipe open and the run
        never ending; the runner started the child in its own session, so
        its pid is its pgid and the whole tree is one group. A run that
        ignores TERM gets KILL after STOP_GRACE_SECS. The runner reports the
        stop: once the group is gone and the pipe has closed it ends the
        stream with the stop sentence, after whatever the script said last;
        unless the script's done says the run had already completed, in
        which case that done is the verdict (see run_engine)."""
        self.stopped = True
        if not self.running():
            return
        self._signal_group(signal.SIGTERM)
        t = threading.Timer(STOP_GRACE_SECS, self._escalate)
        t.daemon = True
        t.start()

    def _escalate(self):
        if self.running():
            self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig):
        """killpg the engine's group; True when the signal went out.

        ESRCH is the group already gone. EPERM on macOS is the zombie
        window: for some milliseconds after the last member died the kernel
        will not credential-check what is left to reap, so retry briefly,
        then let it go, it is dying. There is deliberately NO fallback to
        the leader's pid: a TERM to the leader alone is exactly the signal
        that does not stop a run blocked in a subshell. Nor is the pgid
        guarded on the leader's poll(): POSIX keeps a process group, and its
        id out of reuse, while any member lives, so a reaped leader whose
        subshell is still there still names this run's group."""
        if self._pgid is None:
            return False
        for _ in range(5):
            try:
                os.killpg(self._pgid, sig)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                time.sleep(0.02)
        return False

    def answer(self, prompt_id, text):
        """Answer the prompt the script is waiting on: open the FIFO for
        writing, write exactly one line, close. That is the script's contract
        (the comment above ask() in deploy-stack.sh): a write end held open
        between answers reads as an empty answer and takes the default.

        prompt_id has to be the prompt that is waiting. A page that
        reconnects can replay an old question, and its answer must not be
        taken for the current one; the API surfaces the ValueError. The id
        is claimed under the lock before the write and released after it, so
        two answers to the same prompt (a double click, two tabs) let exactly
        one through; and the prompt is cleared afterwards only if it is still
        the one answered, because the script may already have asked the next
        question by then. The claim is released the same way, only while it
        is still this prompt's: by then another answer may hold the claim
        for that next question, and clearing it unconditionally would let a
        third answer through on it while that one is still writing.

        The FIFO is opened non-blocking and retried: a blocking open would
        hang this thread forever if the script died between emitting the
        prompt and reading the pipe, and the window between those two is
        real (ENXIO, no reader yet) even while it is alive. The write itself
        blocks: O_NONBLOCK is cleared after the open, so a long answer waits
        for the reader instead of failing part-way with EAGAIN.

        A write that succeeded emits an `answered` frame. Nothing else in the
        stream ever said a question had been settled, so a page attaching to
        a run that had already answered some replayed them as still waiting,
        opened its modal on the newest one and could only be refused by the
        checks above. The frame carries the prompt's id and what may be shown
        for it: asterisks when the script asked for a secret, otherwise the
        text through redact(). The value the operator typed for a secret goes
        to the FIFO and nowhere else - not this buffer, not the SSE stream,
        not the log - and it is registered with the job's redactions on the
        way, so a later line of the engine's own output that happens to carry
        it is blanked too. The frame is emitted AFTER the write, never
        before: an answer the engine never took is not an answer, and emit()
        takes the same lock, so it happens outside the block that releases
        the claim."""
        with self._lock:
            if self.pending_prompt is None:
                raise ValueError("No prompt is waiting for an answer.")
            if prompt_id != self.pending_prompt:
                raise ValueError("The prompt waiting is {}, not {}.".format(self.pending_prompt, prompt_id))
            if "\n" in text or "\r" in text:
                raise ValueError("An answer is one line.")
            if not self.pipe_path:
                raise ValueError("This job has no engine to answer.")
            if self._answering == prompt_id:
                raise ValueError("Prompt {} is already being answered.".format(prompt_id))
            # read under the lock the claim is taken under: by the time the
            # write returns the script may have asked the next question, and
            # pending_kind would then be that one's
            secret = self.pending_kind == "secret"
            # A secret typed here is registered exactly like the ones that
            # came in with the launch (api.run registers those), because the
            # engine can print it back: MIRROR_SECRET_KEY and
            # SENSOR_PROJECT_KEY go to kvo_aws_mirror.py on its argv, and an
            # argparse usage error or a traceback there puts the whole argv
            # on stdout, which reaches the buffer through job.redact(line).
            # Registered BEFORE the write, not after: the script can echo the
            # value the moment it reads it, and an answer the engine never
            # took was still typed as a secret.
            #
            # A secret too short to blank is refused here, BEFORE the claim
            # is taken and before anything is written to the pipe: the
            # operator gets the refusal on the prompt card, the question
            # stays open, and they can type the real value. A two-character
            # typo used to be registered and go on to rewrite the type field
            # of every later frame (see MIN_REDACTION).
            if secret and text:
                self.add_redaction(text)
            self._answering = prompt_id
            pipe = self.pipe_path
        try:
            self._write_answer(pipe, text)
        except BaseException:
            with self._lock:
                if self._answering == prompt_id:
                    self._answering = None
            raise
        with self._lock:
            if self._answering == prompt_id:
                self._answering = None
            if self.pending_prompt == prompt_id:
                self.pending_prompt = None
                self.pending_kind = None
        self.emit(E.answered(prompt_id, MASKED if secret else self.redact(text)))

    def _write_answer(self, pipe, text):
        deadline = time.monotonic() + ANSWER_WAIT_SECS
        while True:
            try:
                fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise ValueError("The prompt pipe is gone ({}); the run has ended.".format(
                        exc.strerror))
                if not self.alive():
                    raise ValueError("The engine exited before it read the answer.")
                if time.monotonic() > deadline:
                    raise ValueError("The engine never opened the prompt pipe.")
                time.sleep(0.05)
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            data = (text + "\n").encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
        except OSError:
            # EPIPE: the reader closed (the run died) between the open and
            # the write. Python ignores SIGPIPE, so it arrives as this error.
            raise ValueError("The engine went away before reading the answer.")
        finally:
            os.close(fd)


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
        # rebuild through events.py; emit stamps this job's own id on it
        job.emit(_rebuild(typ, ev))


def _rebuild(typ, data):
    # reconstruct a typed event through events.py, with no id: the job's
    # emit stamps a fresh one, so a replay resumes like any other stream
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
        text=True, bufsize=1, errors="replace",
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    # own session, so the pid is the pgid; stop() signals the group for as
    # long as the run is open (Job.running), whatever the leader is doing
    job._pgid = job._proc.pid
    job._group_open = True
    try:
        for line in job._proc.stdout:
            if job.stopped:
                break
            line = job.redact(line.rstrip("\n"))
            if line:
                job.emit(E.log(line))
                on_line(line)
        job._proc.wait()
    finally:
        job._group_open = False
    return job._proc.returncode


# ---------------------------------------------------------------- engine
def run_engine(job, cmd, cwd=None, env=None, wired=True):
    """Run a deploy-stack.sh style command with --events/--prompt-pipe wired to
    this job, tailing the events file into the stream while the process runs.
    `cmd` is the argv WITHOUT the two flags; they are appended here so every
    caller gets them right. Returns the exit code, or None when no process
    ran (stopped before the launch, or a command that could not start).

    wired=False runs a script that has no events channel yet
    (teardown-stack.sh rejects any flag it does not know): nothing is
    appended, no FIFO is made so answer() says the job has no engine to
    answer, its stdout is the stream and process exit is the verdict. The
    same session, group and stop rules apply.

    Three producers feed job.emit: the script's events file (a thread that
    re-reads it every TAIL_SECS), the merged stdout/stderr (this thread, one
    log per line), and the closing verdict, which this thread emits after
    the process exited and the tail thread joined. The runner OWNS the
    terminal event: the tail never emits a done, it stashes the script's
    done on the job, so however the two producers interleaved the last
    event in the buffer is the verdict. The verdict, in order:

      stopped      the operator stopped it. When the script's done says the
                   run had already completed (status in COMPLETED: ok or
                   dry-run) that done is the verdict, verbatim: a finished
                   deploy is never reported as stopped. Any other done it
                   wrote is relayed as a note ("engine: interrupted in key
                   (code 130)"), then the stop sentence, always last.
      script done  the script's own done, verbatim. It is the script's last
                   word, and beats a tail that failed after reading it: the
                   warn narrate already recorded that failure.
      tail failed  the events file could not be read even after the exit,
                   and no done was read before that: an error naming the
                   exception.
      exit != 0    an error naming the exit code.
      exit 0       a done that says only that the engine exited.

    Process exit is terminal either way, done or no done. After a stop the
    script usually still writes its own done (its EXIT trap fires for every
    exit and appends to the events file directly, never through its tee),
    but a KILL, or a shell that died before its trap ran, leaves none, and
    a run whose console went away would otherwise block on its next prompt
    forever. The verdict waits for the tail's read that began after the
    exit, so a done written on the way out is never missed.

    The stop is checked twice: before the launch (nothing starts) and right
    after Popen (a stop that landed in between signals the group at once).
    Set-up lives inside the try so a failed mkfifo or Popen leaves no
    directory behind; a command that cannot start is the terminal error,
    not an exception.
    """
    if job.stopped:
        job.emit(E.error("Stopped by operator.", fix="Reload to start over."))
        return None
    work = tail = rc = None
    exited = threading.Event()
    state = _TailState()
    try:
        try:
            work = tempfile.mkdtemp(prefix="cloudlens-console-{}-".format(job.id))
            job.events_path = os.path.join(work, "events.jsonl")
            with open(job.events_path, "a"):
                pass
            argv = list(cmd)
            if wired:
                job.pipe_path = os.path.join(work, "prompts.fifo")
                os.mkfifo(job.pipe_path, 0o600)
                argv += ["--events", job.events_path, "--prompt-pipe", job.pipe_path]
            penv = dict(os.environ, PYTHONUNBUFFERED="1")
            if env:
                penv.update(env)
            # Same rules as _stream_subprocess: no stdin (a raw read sees EOF,
            # not a hang), no controlling terminal (the script's /dev/tty
            # re-attach cannot happen, and the console's own Ctrl-C does not
            # reach it), its own session (what makes the group kill possible)
            # and bytes that are not UTF-8 replaced, never fatal.
            job._proc = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True,
                text=True, bufsize=1, errors="replace", env=penv,
            )
        except OSError as exc:
            job.emit(E.error("could not start the engine: {}".format(exc),
                             fix="Check that the deploy script exists and can run."))
            return None
        job._pgid = job._proc.pid
        job._group_open = True
        if job.stopped:
            job.stop()      # arrived between the check above and the launch
        tail = threading.Thread(target=_tail_events, args=(job, exited, state), daemon=True)
        tail.start()
        # read to EOF, stopped or not: after a group kill EOF is how the pipe
        # closes, and the lines before it are the last thing the run said.
        # Every line is redacted first: this is where a printed code or
        # password would otherwise become a log event
        for line in job._proc.stdout:
            line = job.redact(line.rstrip("\n"))
            if line:
                job.emit(E.log(line))
        rc = job._proc.wait()
        exited.set()
        tail.join()
        _verdict(job, rc, state)
    finally:
        exited.set()
        if tail is not None and tail.is_alive():
            tail.join()
        if not job.done:
            # Every path above ends in a terminal event, so a job that is
            # not done here is an exception that escaped between the Popen
            # and the verdict (the stdout loop, the tail join, _verdict
            # itself). Without a terminal event the job holds its stack in
            # api._in_flight until the console restarts. The type only: the
            # message can carry a path or the surroundings of a secret, and
            # the exception itself goes on to the caller as it did. The
            # engine is killed if it is still there: nobody reads its
            # stdout any more, and running() is about to say it is gone, so
            # stop() could never reach it again.
            exc = sys.exc_info()[1]
            if job._proc is not None and job._proc.poll() is None:
                job._signal_group(signal.SIGKILL)
                try:
                    # reap the leader: the stdout loop that would have done
                    # it is the thing that raised, and nobody else waits on
                    # this child, so without this it stays a zombie for as
                    # long as the console runs
                    job._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            job.emit(E.error("engine runner failed: " + (type(exc).__name__ if exc else "unknown"),
                             fix="The console's own stderr has the traceback; start the run again."))
        job._group_open = False
        with job._lock:
            job.pending_prompt = None
            job.pending_kind = None
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return rc


class _TailState:
    """What the tail thread leaves for the verdict: whether it read to the
    end, and the exception that stopped it when it did not."""
    ok = True
    exc = None


def _tail_events(job, exited, state):
    """Re-read the script's events file every TAIL_SECS and emit what is new,
    until the process has exited AND one read started after that exit: a
    read that began before the exit can miss the line the script wrote on
    its way out, so `final` is sampled before the read, never after.

    A done is not emitted here. It is stashed on the job for run_engine,
    which emits it after this thread has joined, so the done is the last
    event however the stdout loop and this loop interleaved. Every other
    frame is redacted before it is emitted: the script composes a phase's
    reason, a check's fix or a login's text from what it saw, and what it
    saw can be the code it activated or the password it was given.

    A read that raises (the file gone from under it, a disk error, a bug in
    a parser) does not end the tail: it is reported as a warn narrate (once
    per distinct text, not once per retry), then retried after a backoff of
    0.5s doubling to 2s. Once the process has exited the retries are bounded
    (three), or run_engine would wait on this thread forever; after that the
    tail gives up and the verdict says so.

    last_seq is the watermark iter_script_events uses to tell a replaced
    file from an appended one. It is the seq of the LAST event handled, not
    the largest ever seen: after a restart the new file's seqs begin at 1,
    and a watermark stuck at the old maximum would make every later read
    look like yet another replacement and re-read the file forever. Only an
    int (not a bool, not a string the script may have written) becomes the
    watermark; comparing anything else would raise here."""
    offset, last_seq = 0, None
    backoff, warned, after_exit = 0.5, None, 0
    try:
        while True:
            final = exited.is_set()
            try:
                offset, evs = E.iter_script_events(job.events_path, offset, last_seq)
                for ev in evs:
                    seq = ev.get("script_seq")
                    if E.is_seq(seq):
                        last_seq = seq
                    if ev["type"] == E.DONE:
                        job._script_done = ev
                        continue
                    job.emit(_redact_event(job, ev))
            except Exception as exc:  # noqa
                text = job.redact("event tail: {}: {}".format(type(exc).__name__, exc))
                if text != warned:
                    job.emit(E.narrate(text, "warn"))
                    warned = text
                if final:
                    after_exit += 1
                    if after_exit >= 3:
                        raise
                    time.sleep(backoff)
                else:
                    exited.wait(backoff)
                backoff = min(backoff * 2, 2.0)
                continue
            backoff, warned = 0.5, None
            if final:
                return
            exited.wait(TAIL_SECS)
    except Exception as exc:  # noqa
        state.ok = False
        state.exc = exc


def _verdict(job, rc, state):
    """The terminal event, emitted by run_engine once the tail has joined;
    the order is in run_engine's docstring. Every text the verdict writes
    goes through job.redact, the script's own done included (its reason
    is free text the script composed from what it saw)."""
    script_done = job._script_done
    if script_done is not None:
        script_done = _redact_event(job, script_done)
    if job.stopped:
        if script_done is not None and script_done.get("status") in COMPLETED:
            # the stop landed after the script had finished (it lingers
            # after emit_done: resume guidance, cleanup). The run completed
            # and its done says so; a stop sentence here would report a
            # finished deploy as cancelled.
            job.emit(script_done)
            return
        if script_done is not None:
            job.emit(E.narrate(job.redact(_script_done_text(script_done)), "note"))
        job.emit(E.error("Stopped by operator.", fix="Reload to start over."))
    elif script_done is not None:
        job.emit(script_done)
    elif not state.ok:
        job.emit(E.error(job.redact("event tail failed: {}: {}".format(type(state.exc).__name__, state.exc)),
                         fix="The engine's events file could not be read; its own log has the run."))
    elif rc != 0:
        job.emit(E.error(job.redact("engine exited {}".format(rc)),
                         fix="Read the console output above for the failing step."))
    else:
        job.emit(E.done(job.redact("engine exited 0")))


# The fields of a frame that are STRUCTURE, not content: every one of them
# holds either a closed vocabulary this console switches on, or an
# identifier the console or the script minted. None of them can carry a
# secret, and rewriting any of them breaks a renderer rather than
# protecting anybody:
#
#   type        emit() reads it to decide a frame is terminal, and the SSE
#               stream names the event after it. A mangled type is a run
#               that never finishes and a frame no listener hears.
#   id          the console's own counter, what Last-Event-ID resumes on
#   script_seq  the script's line number, the tail's watermark
#   prompt_id   what an answer is paired with, so a reply cannot land on
#               the wrong question
#   status      pass|warn|fail, done|failed|skipped, ok|interrupted|...
#   kind        text|secret on a prompt (this one decides whether an
#               answer is ever shown), vpc|kvo|vpb|... on a resource
#   name        the phase's name, out of the script's own PHASE_ORDER
#   node        a diagram node id
#   tone        info|good|note|warn|err
#   stream      out|err
#   component   vcontroller|kvo|vpb on a login card
#   role        the instance role the deploy tagged
#
# Everything else - question, reason, text, fix, item, url, summary, an
# address, a resource id - is content and stays redacted.
STRUCTURAL = frozenset((
    "type", "id", "script_seq", "prompt_id", "status", "kind", "name",
    "node", "tone", "stream", "component", "role",
))


def _redact_event(job, ev):
    """The script frame with each of its top-level string fields redacted,
    in place, STRUCTURAL fields excepted: the stash is the frame the
    verdict emits, and emit stamps the id on that same dict, so a copy
    here would leave the stash without one. Top level only, and that is
    sufficient: the script's frames are flat (emit_event writes one JSON
    object of string fields; a number or a bool has no secret in it), so
    there is nothing nested to descend into. Every frame the tail emits,
    and the stashed done, comes through here.

    The STRUCTURAL exception is the second half of the fix MIN_REDACTION
    is the first half of. A floor on the needle makes the collision
    unlikely; leaving the fields the console DECIDES on out of the
    substitution makes a run's outcome independent of what a secret
    happens to spell."""
    for k, v in list(ev.items()):
        if isinstance(v, str) and k not in STRUCTURAL:
            ev[k] = job.redact(v)
    return ev


def _script_done_text(ev):
    """The script's own done as one line of narration, for a stopped run
    whose terminal event is the stop sentence: "engine: interrupted in key
    (code 130): reason", each field only when the script sent it."""
    text = "engine: {}".format(ev.get("status") or "ended")
    if ev.get("phase"):
        text += " in {}".format(ev["phase"])
    if ev.get("code") not in (None, ""):
        text += " (code {})".format(ev["code"])
    if ev.get("reason"):
        text += ": {}".format(ev["reason"])
    return text


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
