"""run_engine: the orchestrator runs deploy-stack.sh with --events and
--prompt-pipe wired to one job, tails the events file into the job's stream,
answers prompts through the FIFO and treats process exit as the end of the
run whether or not a done event arrived.

Every fake script here parses the two flags off its argv the way the real one
does, so the argv run_engine builds is tested against a parser, not assumed.
The contract these tests hold the engine to is the comment above ask() in
deploy/deploy-stack.sh: one line per answer, write end never held open, a
blocked run needs its process GROUP signalled, and exit without a done is
terminal.

Run:  cd console && python3 -m pytest tests/test_orchestrator_engine.py -q
"""
import os
import sys
import time
import signal
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cloudlens_console import events as E, orchestrator as O  # noqa

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


def _script(tmp_path, body, name="fake.sh"):
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


def _start(job, cmd):
    t = threading.Thread(target=O.run_engine, args=(job, cmd), daemon=True)
    t.start()
    return t


def _wait_for(pred, secs=5.0):
    deadline = time.time() + secs
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _types(job):
    return [e["type"] for e in job.buffer]


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
    assert job.buffer[-1]["answer"] == "ABCD-1234"
    assert job.buffer[-1]["status"] == "ok", "the script's own done, not a synthesized one"
    assert job.pending_prompt is None
    assert job.done


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


# ------------------------------------------------------------------ answers
def test_answer_refuses_the_wrong_prompt_and_an_unasked_one(tmp_path):
    script = _script(tmp_path, FAKE_PROMPT)
    job = O.Job("j2", "stack", {})
    try:
        job.answer("p1", "early")
        assert False, "no prompt was waiting"
    except ValueError:
        pass
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    try:
        job.answer("p9", "wrong")
        assert False, "p9 is not the prompt that is waiting"
    except ValueError:
        pass
    assert job.pending_prompt == "p1", "a refused answer leaves the prompt waiting"
    try:
        job.answer("p1", "two\nlines")
        assert False, "an answer is exactly one line"
    except ValueError:
        pass
    job.answer("p1", "right")
    t.join(5)
    assert job.buffer[-1]["type"] == "done" and job.buffer[-1]["answer"] == "right"


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
    assert "3" in job.buffer[-1]["text"], job.buffer[-1]
    assert job.done

    # zero: a synthesized done that says the engine exited, not a fake success line
    script = _script(tmp_path, FAKE_EXIT % 0, "exit0.sh")
    job = O.Job("j3b", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert not t.is_alive()
    types = _types(job)
    assert types[0] == "hello" and types[-1] == "done", types
    assert job.buffer[-1]["summary"] == "engine exited 0"
    assert job.done


# ------------------------------------------------------------------ stop
def test_stop_reaps_a_script_blocked_on_the_fifo(tmp_path):
    """A TERM to the pid alone does not stop a run blocked on a prompt: the
    read sits in a $( ) subshell, a separate process, and bash waits for it.
    stop() signals the process group, so the leader and the subshell go
    together, and the run closes with the console's stop sentence."""
    script = _script(tmp_path, FAKE_BLOCKED)
    job = O.Job("j4", "stack", {})
    t = _start(job, [script])
    assert _wait_for(lambda: job.pending_prompt == "p1")
    pgid = job._proc.pid            # start_new_session: the leader's pid is the pgid
    assert os.getpgid(pgid) == pgid
    time.sleep(0.2)                 # let the subshell reach its read
    assert not _group_gone(pgid)

    t0 = time.time()
    job.stop()
    assert _wait_for(lambda: _group_gone(pgid), 3.0), "the process group is still there"
    assert time.time() - t0 < 3.0
    t.join(5)
    assert not t.is_alive()
    types = _types(job)
    assert "done" not in types, "after a group kill there is no done event"
    assert types[-1] == "error"
    assert job.buffer[-1]["text"] == "Stopped by operator."
    assert job.buffer[-1]["fix"] == "Reload to start over."
    assert job.stopped and job.done


def test_stop_on_a_finished_job_is_harmless(tmp_path):
    script = _script(tmp_path, FAKE_STDOUT)
    job = O.Job("j5", "stack", {})
    t = _start(job, [script])
    t.join(5)
    assert job.buffer[-1]["type"] == "done"
    n = len(job.buffer)
    job.stop()
    time.sleep(0.2)
    assert len(job.buffer) == n, "a stop after the end appends nothing"


# ------------------------------------------------------------------ tail
def test_tail_tracks_only_int_script_seq_and_passes_last_seq(tmp_path, monkeypatch):
    """The tail loop hands iter_script_events the highest script_seq it has
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


def test_stdout_lines_become_log_events(tmp_path):
    script = _script(tmp_path, FAKE_STDOUT)
    job = O.Job("j8", "stack", {})
    t = _start(job, [script])
    t.join(5)
    logs = [e for e in job.buffer if e["type"] == "log"]
    assert any(e["text"] == "hello from stdout" for e in logs), job.buffer


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


def test_engine_subprocess_has_no_tty_and_its_own_group():
    import inspect
    src = inspect.getsource(O.run_engine)
    assert "start_new_session=True" in src
    assert "stdin=subprocess.DEVNULL" in src


def test_stop_signals_the_group_not_the_pid():
    import inspect
    assert "SIGTERM" in inspect.getsource(O.Job.stop)
    src = inspect.getsource(O.Job)
    assert "killpg" in src, "the pid alone leaves the $( ) subshell blocked on the FIFO"
    assert "SIGKILL" in src, "a run that ignores TERM still has to end"


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
