"""run_engine: the orchestrator runs deploy-stack.sh with --events and
--prompt-pipe wired to one job, tails the events file into the job's stream,
answers prompts through the FIFO and treats process exit as the end of the
run whether or not a done event arrived.

Every fake script here parses the two flags off its argv the way the real one
does, so the argv run_engine builds is tested against a parser, not assumed.
The contract these tests hold the engine to is the comment above ask() in
deploy/deploy-stack.sh: one line per answer, write end never held open, a
blocked run needs its process GROUP signalled, and exit is terminal whether
or not the script wrote a done. On the console's side: run_engine owns the
terminal event, so the last event of every run is its verdict, a stopped run
ends with the stop sentence unless the script's done says the run had already
completed, and the script's own done is never emitted by the tail thread.

Run:  cd console && python3 -m pytest tests/test_orchestrator_engine.py -q
"""
import os
import time
import threading

import pytest

from cloudlens_console import events as E, orchestrator as O

# The argv parser every fake shares: the real script's flags, nothing else.
PARSE = r'''#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do case $1 in --events) ev=$2; shift 2;; --prompt-pipe) pipe=$2; shift 2;; *) shift;; esac; done
'''

# hello, a prompt, block on the FIFO for the answer, echo it back in the done
FAKE_PROMPT = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
echo '{"seq":2,"ts":"t","type":"prompt","id":"p1","question":"Code?","kind":"secret"}' >> "$ev"
IFS= read -r ans < "$pipe"
echo "{\"seq\":3,\"ts\":\"t\",\"type\":\"done\",\"status\":\"ok\",\"answer\":\"$ans\"}" >> "$ev"
'''

# hello, then exit with the given code and no done event
FAKE_EXIT = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
exit %d
'''

# a prompt, then block on the FIFO inside a $( ) subshell: the shape the real
# script has (x="$(ask ...)"), and the one a TERM to the pid alone cannot end
FAKE_BLOCKED = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"prompt","id":"p1","question":"Code?","kind":"text"}' >> "$ev"
x=$(IFS= read -r a < "$pipe"; echo "$a")
echo "{\"seq\":2,\"ts\":\"t\",\"type\":\"done\",\"status\":\"ok\",\"answer\":\"$x\"}" >> "$ev"
'''

# The real script's shape around a stop: stdout through a tee process
# substitution, a TERM trap that exits 130, an EXIT trap that writes the done
# straight to the events file (the way emit_event does, never through the
# tee), and the question asked from inside a $( ) subshell blocked on the
# FIFO. After a group TERM this shape still writes its done: the leader may
# well die of SIGPIPE echoing into the dead tee, but the EXIT trap runs in
# whichever shell of the tree gets to it, and the file write does not need
# the tee.
FAKE_REAL = PARSE + r'''
exec > >(tee -a "$(dirname "$ev")/run.log") 2>&1
trap 'echo interrupted; exit 130' INT TERM
trap 'echo "{\"seq\":3,\"ts\":\"t\",\"type\":\"done\",\"status\":\"failed\",\"phase\":\"key\",\"code\":\"130\"}" >> "$ev"' EXIT
echo '{"seq":1,"ts":"t","type":"prompt","id":"p1","question":"Code?","kind":"text"}' >> "$ev"
x=$( IFS= read -r a < "$pipe"; echo "$a" )
echo "{\"seq\":2,\"ts\":\"t\",\"type\":\"done\",\"status\":\"ok\",\"answer\":\"$x\"}" >> "$ev"
'''

# the leader exits at once, but a background subshell it started keeps the
# stdout pipe (inherited) and the FIFO read alive: EOF never comes on its own
FAKE_ORPHAN = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"prompt","id":"p1","question":"Code?","kind":"text"}' >> "$ev"
( IFS= read -r a < "$pipe"; echo "$a" ) &
exit 0
'''

# ignores TERM (and so does the sleep it execs: SIG_IGN is inherited)
FAKE_IGNORES_TERM = PARSE + r'''
trap '' TERM
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
sleep 30
'''

# a prompt the script never reads the answer to
FAKE_PROMPT_NO_READ = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"prompt","id":"p1","question":"Code?","kind":"text"}' >> "$ev"
sleep %s
exit 0
'''

# the done, then a stdout line well after it
FAKE_DONE_THEN_STDOUT = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
echo '{"seq":2,"ts":"t","type":"done","status":"ok"}' >> "$ev"
sleep 0.4
echo "after the done"
exit 0
'''

# bytes that are not UTF-8 on stdout, then a done
FAKE_BAD_BYTES = PARSE + r'''
printf '\xff\xfe bad\n'
echo '{"seq":1,"ts":"t","type":"done","status":"ok"}' >> "$ev"
exit 0
'''

# a marker file when it ran at all
FAKE_MARKER = PARSE + r'''
: > "$MARKER"
exit 0
'''

# a raw read from stdin: the console gives it /dev/null, so EOF, not a hang
FAKE_STDIN = PARSE + r'''
read -r x; echo "RC=$?"
exit 0
'''

# stdout only, then a clean exit
FAKE_STDOUT = PARSE + r'''
echo "hello from stdout"
exit 0
'''

# no events, just enough time for the tail loop to poll more than once
FAKE_SLEEP = PARSE + r'''
sleep 0.7
exit 0
'''

# hello and a done, then a linger long enough for a stop to land: the real
# script prints resume guidance and cleans up after emit_done. %s: the status
FAKE_DONE_THEN_LINGER = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
echo '{"seq":2,"ts":"t","type":"done","status":"%s"}' >> "$ev"
sleep 1
exit 0
'''

# hello and a done, then enough time for a failed tail read to be retried
FAKE_DONE_SLOW = PARSE + r'''
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
echo '{"seq":2,"ts":"t","type":"done","status":"ok"}' >> "$ev"
sleep 0.8
exit 0
'''


def _script(tmp_path, body, name="fake.sh"):
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


def _start(job, cmd, **kw):
    t = threading.Thread(target=O.run_engine, args=(job, cmd), kwargs=kw, daemon=True)
    t.start()
    return t


def _wait_for(pred, secs=5.0):
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _types(job):
    return [e["type"] for e in job.buffer]


def _last(job):
    return job.buffer[-1]


def _group_gone(pgid):
    """True once no process is left in the group (ESRCH). On macOS a
    killpg(pgid, 0) returns EPERM for a few milliseconds after the members
    die: they are zombies being reaped, and the kernel refuses to
    credential-check a zombie. Measured here: EPERM from ~12ms to ~25ms
    after stop(), with `ps -g` already empty, then ESRCH. So EPERM is
    "not yet", and the caller keeps polling."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _assert_stopped(job):
    last = _last(job)
    assert last["type"] == "error", _types(job)
    assert last["text"] == "Stopped by operator."
    assert last["fix"] == "Reload to start over."
    assert job.stopped and job.done and not job.running()


# ------------------------------------------------------------------ stream
def test_engine_streams_events_and_answers_prompts(tmp_path):
    script = _script(tmp_path, FAKE_PROMPT)
    job = O.Job("j1", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: any(e["type"] == "prompt" for e in job.buffer)), _types(job)
    assert job.pending_prompt == "p1"
    job.answer("p1", "ABCD-1234")
    t.join(5)
    assert not t.is_alive(), "run_engine did not return after the done event"
    types = _types(job)
    assert types[:2] == ["hello", "prompt"] and types[-1] == "done", types
    assert _last(job)["answer"] == "ABCD-1234"
    assert _last(job)["status"] == "ok", "the script's own done, not a synthesized one"
    assert job.pending_prompt is None
    assert job.done and not job.running()


def test_prompt_events_keep_their_script_seq(tmp_path):
    # the tail goes through iter_script_events / from_script, so the frames are
    # console events with the script's seq kept as script_seq and its id as prompt_id
    script = _script(tmp_path, FAKE_PROMPT)
    job = O.Job("j1b", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    job.answer("p1", "x")
    t.join(5)
    prompt = next(e for e in job.buffer if e["type"] == "prompt")
    assert prompt["script_seq"] == 2 and prompt["prompt_id"] == "p1"
    assert "id" in prompt and isinstance(prompt["id"], int)


def test_stdout_lines_become_log_events(tmp_path):
    script = _script(tmp_path, FAKE_STDOUT)
    job = O.Job("j8", "stack", {})
    t = _start(job, [script])
    t.join(5)
    logs = [e for e in job.buffer if e["type"] == "log"]
    assert any(e["text"] == "hello from stdout" for e in logs), job.buffer


def test_stdout_that_is_not_utf8_does_not_end_the_run(tmp_path):
    """The script relays whatever a tool printed, and a tool can print bytes
    that are not UTF-8 (a locale-less container, a binary in an error). The
    text pipe replaces them; it must never raise in the stdout loop, which
    would leave the run without its verdict."""
    script = _script(tmp_path, FAKE_BAD_BYTES)
    job = O.Job("j8u", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    assert _last(job)["type"] == "done" and _last(job)["status"] == "ok", _types(job)
    logs = [e["text"] for e in job.buffer if e["type"] == "log"]
    assert any("bad" in x for x in logs), logs


def test_stdin_is_eof_not_a_hang(tmp_path):
    # a raw `read` in the script (the /dev/tty re-attach cannot happen: no
    # terminal) sees EOF from /dev/null and returns 1 at once
    script = _script(tmp_path, FAKE_STDIN)
    job = O.Job("j8s", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    assert any(e["type"] == "log" and e["text"] == "RC=1" for e in job.buffer), job.buffer


# ------------------------------------------------------------------ answers
def test_answer_refuses_the_wrong_prompt_and_an_unasked_one(tmp_path):
    script = _script(tmp_path, FAKE_PROMPT)
    job = O.Job("j2", "stack", {})
    with pytest.raises(ValueError):
        job.answer("p1", "early")          # no prompt was waiting
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    with pytest.raises(ValueError):
        job.answer("p9", "wrong")          # p9 is not the prompt that is waiting
    assert job.pending_prompt == "p1", "a refused answer leaves the prompt waiting"
    with pytest.raises(ValueError):
        job.answer("p1", "two\nlines")     # an answer is exactly one line
    job.answer("p1", "right")
    t.join(5)
    assert _last(job)["type"] == "done" and _last(job)["answer"] == "right"


def test_two_answers_to_one_prompt_let_exactly_one_through(tmp_path):
    """A double click, or two tabs on the same run: the id is claimed under
    the job's lock before the write, so one answer reaches the FIFO and the
    other is refused, whichever order the threads ran in. The script reads
    one line, and that line is the winner's."""
    script = _script(tmp_path, FAKE_PROMPT)
    job = O.Job("j2c", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    time.sleep(0.2)                     # let the reader reach the FIFO
    results = []
    gate = threading.Barrier(2)

    def go(text):
        gate.wait()
        try:
            job.answer("p1", text)
            results.append(("ok", text))
        except ValueError as exc:
            results.append(("refused", str(exc)))

    a = threading.Thread(target=go, args=("one",))
    b = threading.Thread(target=go, args=("two",))
    a.start(); b.start(); a.join(5); b.join(5)
    assert sorted(r[0] for r in results) == ["ok", "refused"], results
    t.join(5)
    assert not t.is_alive()
    winner = next(r[1] for r in results if r[0] == "ok")
    assert _last(job)["type"] == "done" and _last(job)["answer"] == winner


def test_a_finished_answer_does_not_release_the_next_prompts_claim(monkeypatch):
    """Two prompts back to back. A claims p1 and is in its write; the script
    asks p2 and the tail files it; C claims p2 (allowed: the claim held is
    p1's) and is in its write; then A finishes. A's success path must
    release only ITS claim. Clearing unconditionally wiped C's, and a third
    answer D was then let through on p2 while C was still writing: two
    lines on the FIFO for one question. The write helper is replaced by a
    gate per answer so the interleaving is exact, not a race."""
    writes = []
    gates = {"a": threading.Event(), "c": threading.Event(), "d": threading.Event()}

    def gated_write(self, pipe, text):
        writes.append((self._answering, text))
        assert gates[text].wait(5), "the test never released %s" % text

    monkeypatch.setattr(O.Job, "_write_answer", gated_write)
    job = O.Job("j2f", "stack", {})
    job.pipe_path = "/nonexistent/prompts.fifo"   # never opened: the gate is the write
    results = {}

    def go(prompt_id, text):
        try:
            job.answer(prompt_id, text)
            results[text] = "ok"
        except ValueError as exc:
            results[text] = str(exc)

    job.emit(E.from_script({"seq": 1, "type": "prompt", "id": "p1", "question": "one?"}))
    a = threading.Thread(target=go, args=("p1", "a"))
    a.start()
    assert _wait_for(lambda: writes == [("p1", "a")]), writes
    # the script asks the next question while A is still writing
    job.emit(E.from_script({"seq": 2, "type": "prompt", "id": "p2", "question": "two?"}))
    assert job.pending_prompt == "p2"
    c = threading.Thread(target=go, args=("p2", "c"))
    c.start()
    assert _wait_for(lambda: writes == [("p1", "a"), ("p2", "c")]), writes
    gates["a"].set()
    a.join(5)
    assert results["a"] == "ok"
    # C still holds p2: D is refused, not let through as a second write
    with pytest.raises(ValueError, match="already being answered"):
        job.answer("p2", "d")
    gates["c"].set()
    c.join(5)
    assert results["c"] == "ok"
    assert [w for w in writes if w[0] == "p2"] == [("p2", "c")], writes
    assert job.pending_prompt is None and job._answering is None


def test_answer_does_not_hang_when_the_engine_never_reads(tmp_path, monkeypatch):
    """The FIFO open is non-blocking and bounded: a script that emitted the
    prompt and then died, or never got to the read, must not hang the API
    thread on open(). Every way it fails is a ValueError the route can show."""
    monkeypatch.setattr(O, "ANSWER_WAIT_SECS", 0.4)
    # alive but never reading: the bounded wait ends it
    script = _script(tmp_path, FAKE_PROMPT_NO_READ % "1.5", "noread.sh")
    job = O.Job("j2d", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    t0 = time.monotonic()
    with pytest.raises(ValueError):
        job.answer("p1", "x")
    assert time.monotonic() - t0 < 1.2, "answer() waited past its deadline"
    assert job.pending_prompt == "p1", "a failed answer leaves the prompt waiting"
    t.join(5)
    assert not t.is_alive()
    # and once the run has ended: still a ValueError, at once
    with pytest.raises(ValueError):
        job.answer("p1", "x")
    # dead before the read: the prompt is out, the process is gone
    script = _script(tmp_path, FAKE_PROMPT_NO_READ % "0", "dead.sh")
    job = O.Job("j2e", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1" or job.done)
    t0 = time.monotonic()
    with pytest.raises(ValueError):
        job.answer("p1", "x")
    assert time.monotonic() - t0 < 1.2
    t.join(5)


# ------------------------------------------------------------------ exit
def test_exit_without_done_is_terminal(tmp_path):
    # non-zero: an error naming the exit code, and no done anywhere
    script = _script(tmp_path, FAKE_EXIT % 3, "exit3.sh")
    job = O.Job("j3", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    types = _types(job)
    assert "done" not in types, types
    assert types[-1] == "error", types
    assert "3" in _last(job)["text"], _last(job)
    assert job.done

    # zero: a synthesized done that says the engine exited, not a fake success line
    script = _script(tmp_path, FAKE_EXIT % 0, "exit0.sh")
    job = O.Job("j3b", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    types = _types(job)
    assert types[0] == "hello" and types[-1] == "done", types
    assert _last(job)["summary"] == "engine exited 0"
    assert job.done


def test_the_done_is_last_even_when_stdout_follows_it(tmp_path):
    """The tail thread stashes the script's done instead of emitting it, and
    run_engine emits it after the tail joined: a line the script printed
    after its done (the real one prints TO RESUME guidance after emit_done)
    lands in the buffer BEFORE the done, never after the terminal event."""
    script = _script(tmp_path, FAKE_DONE_THEN_STDOUT)
    job = O.Job("j3c", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    types = _types(job)
    assert types[-1] == "done" and _last(job)["status"] == "ok", types
    assert types.count("done") == 1
    logs = [i for i, e in enumerate(job.buffer) if e["type"] == "log"]
    assert logs and job.buffer[logs[-1]]["text"] == "after the done", job.buffer
    assert logs[-1] < len(job.buffer) - 1


def test_a_stop_before_the_launch_starts_nothing(tmp_path):
    marker = tmp_path / "launched"
    script = _script(tmp_path, FAKE_MARKER)
    job = O.Job("j3d", "stack", {})
    job.stopped = True
    rc = O.run_engine(job, [script], env={"MARKER": str(marker)})
    assert rc is None
    assert not marker.exists(), "the engine was launched after the stop"
    assert job._proc is None
    _assert_stopped(job)
    assert len(job.buffer) == 1


def test_a_command_that_cannot_start_is_the_terminal_error_and_leaks_nothing():
    job = O.Job("j3e", "stack", {})
    rc = O.run_engine(job, ["/nonexistent/engine"])
    assert rc is None
    assert _last(job)["type"] == "error", _types(job)
    assert _last(job)["text"].startswith("could not start the engine: "), _last(job)
    assert job.done and not job.running()
    assert job.events_path and not os.path.exists(os.path.dirname(job.events_path)), \
        "the temp directory outlived a launch that never happened"


# ------------------------------------------------------------------ stop
def test_stop_reaps_a_script_blocked_on_the_fifo(tmp_path):
    """A TERM to the pid alone does not stop a run blocked on a prompt: the
    read sits in a $( ) subshell, a separate process, and bash waits for it.
    stop() signals the process group, so the leader and the subshell go
    together, and the run closes with the console's stop sentence. This fake
    has no traps, so it writes no done; the console synthesizes none for a
    stopped run either, and the stop sentence is the last event."""
    script = _script(tmp_path, FAKE_BLOCKED)
    job = O.Job("j4", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    pgid = job._pgid
    assert pgid == job._proc.pid            # start_new_session: the leader's pid is the pgid
    assert os.getpgid(pgid) == pgid
    time.sleep(0.2)                         # let the subshell reach its read
    assert not _group_gone(pgid)
    assert job.running()

    t0 = time.monotonic()
    job.stop()
    assert _wait_for(lambda: _group_gone(pgid), 3.0), "the process group is still there"
    assert time.monotonic() - t0 < 3.0
    t.join(5)
    assert not t.is_alive()
    assert "done" not in _types(job), "a stopped run never ends in a done"
    _assert_stopped(job)


def test_stop_relays_the_scripts_own_done_and_still_ends_with_the_stop_sentence(tmp_path):
    """The real script's shape: after a group TERM its EXIT trap still writes
    a done (status failed or interrupted) to the events file. That done is
    the script's account of how it ended and belongs in the stream, but a
    stopped run's terminal event is the stop sentence: the done is relayed
    as a note and the error comes last, whatever the tail read when."""
    script = _script(tmp_path, FAKE_REAL)
    job = O.Job("j4r", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    pgid = job._pgid
    time.sleep(0.3)                         # the subshell at its read, the tee settled
    assert not _group_gone(pgid)

    job.stop()
    assert _wait_for(lambda: _group_gone(pgid), O.STOP_GRACE_SECS + 1.0), "the group is still there"
    t.join(5)
    assert not t.is_alive()
    assert "done" not in _types(job), "the script's done reaches the page as a note, not a done"
    notes = [e for e in job.buffer if e["type"] == "narrate" and e["text"].startswith("engine: ")]
    assert notes, "the script's own done was not relayed: %r" % (_types(job),)
    assert notes[0]["text"] == "engine: failed in key (code 130)", notes[0]
    assert notes[0]["tone"] == "note"
    _assert_stopped(job)


@pytest.mark.parametrize("status", ["ok", "dry-run"])
def test_a_stop_after_the_script_finished_is_not_a_stopped_run(tmp_path, status):
    """The script wrote its done (status ok, or dry-run for a plan-only
    run) and is lingering on its way out when the operator hits stop. The
    run completed: the script's done is the verdict, verbatim, and no stop
    sentence follows it. A finished deploy must never read as cancelled.
    Any other status keeps the note + stop sentence (the test above)."""
    script = _script(tmp_path, FAKE_DONE_THEN_LINGER % status)
    job = O.Job("j4d", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job._script_done is not None), _types(job)
    assert job.running(), "the script must still be lingering when the stop lands"
    job.stop()
    t.join(O.STOP_GRACE_SECS + 5)
    assert not t.is_alive()
    last = _last(job)
    assert last["type"] == "done" and last["status"] == status, _types(job)
    assert last == job._script_done and last["script_seq"] == 2
    assert "error" not in _types(job), _types(job)
    assert not [e for e in job.buffer if e["type"] == "narrate" and e["text"].startswith("engine: ")]
    assert job.stopped and job.done and not job.running()


def test_stop_ends_a_run_whose_leader_died_but_whose_subshell_holds_stdout(tmp_path):
    """The trap the old poll()-gated signalling fell into. A leader that
    exited while a subshell of its still holds the stdout pipe is a run
    that has not ended: no EOF, run_engine blocked in its stdout loop. The
    leader is a zombie, so a signal gated on poll() would never be sent;
    the group is still there (POSIX keeps it while any member lives), and
    stop() signals it for as long as run_engine has not returned."""
    script = _script(tmp_path, FAKE_ORPHAN)
    job = O.Job("j4o", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    pgid = job._pgid
    time.sleep(0.5)
    assert t.is_alive(), "EOF arrived although the subshell still holds stdout"
    assert job.running()
    assert not _group_gone(pgid)

    job.stop()
    assert _wait_for(lambda: _group_gone(pgid), O.STOP_GRACE_SECS + 1.0), "the group is still there"
    t.join(5)
    assert not t.is_alive()
    _assert_stopped(job)


def test_a_run_that_ignores_term_is_killed_after_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "STOP_GRACE_SECS", 0.5)
    script = _script(tmp_path, FAKE_IGNORES_TERM)
    job = O.Job("j4k", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: any(e["type"] == "hello" for e in job.buffer))
    pgid = job._pgid
    time.sleep(0.2)
    t0 = time.monotonic()
    job.stop()
    time.sleep(0.25)
    assert not _group_gone(pgid), "TERM was ignored; the group must still be there before the grace ran out"
    assert _wait_for(lambda: _group_gone(pgid), 3.0), "KILL never came"
    assert time.monotonic() - t0 >= 0.5
    t.join(5)
    assert not t.is_alive()
    _assert_stopped(job)


def test_stop_on_a_finished_job_is_harmless(tmp_path):
    script = _script(tmp_path, FAKE_STDOUT)
    job = O.Job("j5", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert _last(job)["type"] == "done"
    assert not job.running()
    n = len(job.buffer)
    job.stop()
    time.sleep(0.2)
    assert len(job.buffer) == n, "a stop after the end appends nothing"


# ------------------------------------------------------------------ tail
def test_tail_tracks_only_int_script_seq_and_passes_last_seq(tmp_path, monkeypatch):
    """The tail loop hands iter_script_events the seq of the last event it
    handled so a replaced file is caught. A seq that is not an int (the script
    writes strings for everything it does not format itself) must never become
    that watermark: comparing it would raise inside the tail thread."""
    script = _script(tmp_path, FAKE_SLEEP)
    calls = []
    batches = [(10, [E.from_script({"seq": 1, "type": "phase", "name": "a", "status": "done"}),
                     E.from_script({"seq": "2", "type": "phase", "name": "b", "status": "done"}),
                     E.from_script({"seq": 3, "type": "phase", "name": "c", "status": "done"})])]

    def fake(path, start_offset=0, last_seq=None):
        calls.append(last_seq)
        return batches.pop(0) if batches else (10, [])

    monkeypatch.setattr(E, "iter_script_events", fake)
    job = O.Job("j6", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    assert len(calls) >= 2, "the tail polled only once: %r" % (calls,)
    assert calls[0] is None
    assert all(c == 3 for c in calls[1:]), calls
    assert "2" not in calls
    names = [e["name"] for e in job.buffer if e["type"] == "phase"]
    assert names == ["a", "b", "c"], "every frame reached the stream, whatever its seq"


def test_tail_watermark_follows_a_restarted_stream(tmp_path, monkeypatch):
    """After iter_script_events re-reads a replaced file, the seqs start over.
    A watermark that kept the OLD maximum would make every later read look
    like another replacement (first seq <= last_seq) and re-read the file
    forever, duplicating every event. The watermark is the latest seq handled,
    not the largest ever seen."""
    script = _script(tmp_path, FAKE_SLEEP)
    calls = []
    batches = [(10, [E.from_script({"seq": 50, "type": "phase", "name": "old", "status": "done"})]),
               (5, [E.from_script({"seq": 1, "type": "hello", "stack": "new", "region": "r"})])]

    def fake(path, start_offset=0, last_seq=None):
        calls.append(last_seq)
        return batches.pop(0) if batches else (5, [])

    monkeypatch.setattr(E, "iter_script_events", fake)
    job = O.Job("j7", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert calls[0] is None and calls[1] == 50, calls
    assert all(c == 1 for c in calls[2:]), calls


def test_a_failing_tail_read_warns_once_and_recovers(tmp_path, monkeypatch):
    """One read that raises must not end the tail: the run would lose every
    event after it, its done included. The failure is a warn narrate in the
    stream, the read is retried after a backoff, and the run still ends with
    the script's own done."""
    real = E.iter_script_events
    calls = {"n": 0}

    def flaky(path, start_offset=0, last_seq=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated read failure")
        return real(path, start_offset, last_seq)

    monkeypatch.setattr(E, "iter_script_events", flaky)
    script = _script(tmp_path, FAKE_DONE_SLOW)
    job = O.Job("j7f", "stack", {})
    t = _start(job, [script])
    t.join(10)
    assert not t.is_alive()
    warns = [e for e in job.buffer if e["type"] == "narrate" and e["tone"] == "warn"]
    assert len(warns) == 1, warns
    assert "OSError" in warns[0]["text"] and "simulated read failure" in warns[0]["text"]
    assert _last(job)["type"] == "done" and _last(job)["status"] == "ok", _types(job)
    assert calls["n"] >= 2


def test_a_tail_that_cannot_read_after_the_exit_is_the_verdict(tmp_path, monkeypatch):
    # retries after the exit are bounded, or run_engine would wait forever;
    # when they run out the run ends with an error that names the failure,
    # not with a made-up "engine exited 0"
    def broken(path, start_offset=0, last_seq=None):
        raise OSError("disk on fire")

    monkeypatch.setattr(E, "iter_script_events", broken)
    script = _script(tmp_path, FAKE_STDOUT)
    job = O.Job("j7g", "stack", {})
    t = _start(job, [script])
    t.join(10)
    assert not t.is_alive(), "run_engine never gave up on the failing tail"
    # no read ever succeeded, so there is no done to be the verdict
    assert job._script_done is None and "done" not in _types(job), _types(job)
    assert _last(job)["type"] == "error", _types(job)
    assert _last(job)["text"] == "event tail failed: OSError: disk on fire", _last(job)
    warns = [e for e in job.buffer if e["type"] == "narrate" and e["tone"] == "warn"]
    assert len(warns) == 1, "the same failure is not narrated once per retry: %r" % (warns,)


def test_a_done_read_before_the_tail_failed_is_still_the_verdict(tmp_path, monkeypatch):
    """The done is the script's last word. Reads that fail AFTER it was read
    (the file gone at exit, a disk error) are already in the stream as the
    warn narrate; the verdict is the done, not the tail failure."""
    real = E.iter_script_events
    seen = {"done": False}

    def fails_after_the_done(path, start_offset=0, last_seq=None):
        if seen["done"]:
            raise OSError("disk on fire")
        offset, evs = real(path, start_offset, last_seq)
        seen["done"] = any(e["type"] == "done" for e in evs)
        return offset, evs

    monkeypatch.setattr(E, "iter_script_events", fails_after_the_done)
    script = _script(tmp_path, FAKE_DONE_SLOW)
    job = O.Job("j7h", "stack", {})
    t = _start(job, [script])
    t.join(15)
    assert not t.is_alive(), "run_engine never gave up on the failing tail"
    assert seen["done"], "the fake never reached its done: %r" % (_types(job),)
    warns = [e for e in job.buffer if e["type"] == "narrate" and e["tone"] == "warn"]
    assert len(warns) == 1 and "disk on fire" in warns[0]["text"], warns
    assert _last(job)["type"] == "done" and _last(job)["status"] == "ok", _types(job)
    assert "error" not in _types(job), _types(job)


def test_engine_appends_the_two_flags_itself(tmp_path):
    # the fake echoes its parsed paths; both must be the job's own, in a
    # private directory, and the pipe must really be a FIFO while it runs
    body = PARSE + r'''
echo "EV=$ev"
echo "PIPE=$pipe"
[[ -p "$pipe" ]] && echo "PIPE_IS_FIFO=yes"
exit 0
'''
    script = _script(tmp_path, body)
    job = O.Job("j9", "stack", {})
    t = _start(job, [script])
    t.join(5)
    texts = [e["text"] for e in job.buffer if e["type"] == "log"]
    assert "PIPE_IS_FIFO=yes" in texts, texts
    ev = next(x for x in texts if x.startswith("EV=")).split("=", 1)[1]
    pipe = next(x for x in texts if x.startswith("PIPE=")).split("=", 1)[1]
    assert os.path.dirname(ev) == os.path.dirname(pipe)
    assert not os.path.exists(pipe), "the FIFO does not outlive the run"


# ------------------------------------------------------------------ events.py
def test_iter_script_events_ignores_a_str_last_seq(tmp_path):
    """The guard on last_seq is the same shape as _first_seq's: an int that is
    not a bool. A str last_seq (a caller that tracked a string seq) is
    ignored, never compared, so it cannot raise or force a restart."""
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"seq":1,"type":"hello","stack":"a","region":"r"}\n'
                  b'{"seq":2,"type":"phase","name":"x","status":"done"}\n')
    off, evs = E.iter_script_events(str(p), 0)
    assert [e["script_seq"] for e in evs] == [1, 2]
    p.write_bytes(p.read_bytes() + b'{"seq":3,"type":"phase","name":"y","status":"done"}\n')
    off2, evs = E.iter_script_events(str(p), off, last_seq="2")
    assert [e["script_seq"] for e in evs] == [3], "a str watermark is no watermark"
    off2, evs = E.iter_script_events(str(p), off, last_seq=True)
    assert [e["script_seq"] for e in evs] == [3], "neither is a bool"


def test_is_seq_is_the_one_check_both_sides_use():
    assert E.is_seq(1) and E.is_seq(0)
    assert not E.is_seq(True) and not E.is_seq("2") and not E.is_seq(None) and not E.is_seq(1.0)
