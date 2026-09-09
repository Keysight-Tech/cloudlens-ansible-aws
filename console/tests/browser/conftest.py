"""The instruments the browser smoke tests drive the console with.

One real server, one real browser, and two stand-ins where the console
would otherwise reach a cloud account or an appliance:

  the aws CLI   a small executable named `aws`, first on PATH, that prints
                canned JSON per subcommand. It is a stub of the COMMAND,
                not of the console: api._aws builds its argv, runs it
                through subprocess, appends --region and --output json,
                clears AWS_PAGER and parses what comes back, exactly as it
                does against a real account. tests/test_api.py fakes
                api._aws itself, which is the right instrument for a unit
                test of a caller; here the point is the whole path, so the
                fake goes at the far end of it.

  the engine    a bash script that parses --events and --prompt-pipe off
                its argv the way deploy-stack.sh does, appends the frames
                of a fixture events file, and blocks on the FIFO at a
                prompt. api.DEPLOY (and api.TEARDOWN) is pointed at it for
                the tests that need a run whose frames they control; every
                other part of the path stays real - POST /api/run, the
                profile write, the _ENGINE_LOCK, run_engine's Popen, the
                tail thread, the verdict, the SSE stream and the browser's
                EventSource. tests/test_orchestrator_engine.py uses fakes
                of this exact shape.

Nothing here needs AWS credentials, the network or a KVO.

The server runs in THIS process (server.serve on port 0, serve_forever on
a thread), which is what lets a test point api.DEPLOY at the fake and put
the stub on PATH without a second process to configure. It never binds a
fixed port.

Playwright, or a chromium build it can launch, missing is a SKIP with the
command that installs it, never a failure.
"""
import io
import os
import json
import stat
import time
import socket
import threading

import pytest

from cloudlens_console import api, server
from cloudlens_console import __main__ as entry

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:                                     # pragma: no cover - the skip path
    sync_playwright = None
    expect = None

INSTALL = "pip install playwright && playwright install chromium"

# Every assertion in this suite waits for the browser rather than sleeping,
# and some of them wait on a real subprocess: an engine that starts, writes
# its events file and exits. Ten seconds is long enough for that on a busy
# machine and short enough that a genuine hang is still a failed test.
TIMEOUT = 10_000

AWS_STUB = '''#!/usr/bin/env python3
"""Stands in for the aws CLI. Prints the JSON the test in hand put under
this subcommand in $CLOUDLENS_TEST_AWS, and {} for one it did not name.
Every argv is appended to aws.log beside it, so a test can prove which
call the console actually made."""
import json
import os
import sys

answers_file = os.environ.get("CLOUDLENS_TEST_AWS", "")
with open(os.path.join(os.path.dirname(answers_file), "aws.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
try:
    with open(answers_file) as fh:
        answers = json.load(fh)
except (OSError, ValueError):
    answers = {}
words = [a for a in sys.argv[1:] if not a.startswith("-")][:2]
answer = answers.get(" ".join(words))
if answer is None:
    answer = {}
if isinstance(answer, dict) and "_error" in answer:
    sys.stderr.write(answer["_error"] + "\\n")
    sys.exit(254)
print(json.dumps(answer))
'''

# The engine stand-in. It reads its two flags the way deploy-stack.sh does,
# so the argv run_engine builds is parsed and not assumed, appends one line
# of the fixture events file at a time, and blocks on the FIFO at a prompt
# exactly where the real script's ask() does - one line per answer, the
# write end never held open. What it read comes back on stdout, which is
# the console's log stream: that is the proof the answer reached the engine
# and not only the page that typed it.
FAKE_ENGINE = r'''#!/usr/bin/env bash
ev=""; pipe=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --events) ev=$2; shift 2;;
    --prompt-pipe) pipe=$2; shift 2;;
    *) shift;;
  esac
done
[[ -n "$ev" ]] || { echo "no --events file on the argv"; exit 64; }
while IFS= read -r line; do
  [[ -n "$line" ]] || continue
  printf '%s\n' "$line" >> "$ev"
  case "$line" in
    *'"type":"prompt"'*)
      [[ -n "$pipe" ]] || { echo "a prompt with no --prompt-pipe on the argv"; exit 64; }
      IFS= read -r answer < "$pipe"
      echo "engine read the answer: $answer"
      ;;
  esac
done < "$CLOUDLENS_TEST_EVENTS"
exit "${CLOUDLENS_TEST_EXIT:-0}"
'''

# teardown-stack.sh has no events channel: it runs unwired and its stdout
# IS the report the audit shows. This one refuses --events for that reason,
# as the real script does with any flag it does not know.
FAKE_TEARDOWN = r'''#!/usr/bin/env bash
for arg in "$@"; do
  case $arg in
    --events|--prompt-pipe) echo "unknown option: $arg"; exit 2;;
  esac
done
echo "teardown-stack.sh $*"
cat "$CLOUDLENS_TEST_REPORT"
exit 0
'''

# kvo_license.py's surface, as api._kvo_license imports it by path: the two
# functions the check action calls. Only the HTTPS conversation with the
# appliance is stood in for; api._lic_check, _lookup, _state and _op_running
# all run for real on what this returns.
FAKE_KVO_LICENSE = '''"""A KVO that answers, and recognises nothing."""


def accept_eula(kvo, verify):
    return True


def token(kvo, user, pw, verify):
    return "fake-token"


def lookup_code(kvo, base, tok, code, verify, timeout=120):
    """No entitlements, and a terminal state: the shape of a code the
    appliance looked up and did not recognise."""
    return [], {"state": "FAILED"}
'''


def _write_exec(path, text):
    with io.open(path, "w", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ------------------------------------------------------------ the browser
@pytest.fixture(scope="session")
def playwright():
    """Playwright itself, or a SKIP saying how to install it. The default
    suite does not collect this directory, so a machine without either is
    only ever told here, and only when it asked for these tests."""
    if sync_playwright is None:
        pytest.skip("playwright is not installed: %s" % INSTALL)
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright):
    """Headless chromium, or a SKIP: playwright installs without its
    browser builds, and a missing one is a set-up step, not a defect."""
    try:
        launched = playwright.chromium.launch(headless=True)
    except Exception as exc:                            # pragma: no cover - the skip path
        pytest.skip("playwright cannot launch chromium (%s): %s" % (type(exc).__name__, INSTALL))
    try:
        yield launched
    finally:
        launched.close()


@pytest.fixture
def page(browser, console):
    """A page on its own context, so every test starts with an empty
    localStorage: the wizard restores a saved plan, the Watch screen
    re-attaches the last run id, and Teardown reads the stack name out of
    the same store. An uncaught exception in the page fails the test that
    was on it."""
    context = browser.new_context(base_url=console)
    thrown = []
    pg = context.new_page()
    pg.on("pageerror", lambda exc: thrown.append(str(exc)))
    try:
        yield pg
    finally:
        context.close()
        assert not thrown, "the page threw: " + " | ".join(thrown)


# ------------------------------------------------------------- the server
@pytest.fixture(scope="session")
def instruments(tmp_path_factory):
    """The stand-ins, written once: the aws stub, the engine, the teardown
    script and the kvo_license module."""
    d = tmp_path_factory.mktemp("instruments")
    return {
        "dir": str(d),
        "answers": str(d / "answers.json"),
        "aws_log": str(d / "aws.log"),
        "engine": _write_exec(str(d / "fake-deploy.sh"), FAKE_ENGINE),
        "teardown": _write_exec(str(d / "fake-teardown.sh"), FAKE_TEARDOWN),
        "kvo_license": _write_exec(str(d / "fake_kvo_license.py"), FAKE_KVO_LICENSE),
    }


@pytest.fixture(scope="session")
def console(instruments):
    """The real server, on a port the OS picks (never 8760, never a fixed
    one: another console may be running on this machine). Yields its base
    URL and stops it cleanly.

    The stub aws goes first on PATH for the life of the session, so every
    api._aws call in every request thread finds it; api.VC_CREDS_FILE is
    pointed at a path that does not exist, so no test can read the
    operator's own vController credentials file.
    """
    _write_exec(os.path.join(instruments["dir"], "aws"), AWS_STUB)
    was = {k: os.environ.get(k) for k in ("PATH", "CLOUDLENS_TEST_AWS")}
    os.environ["PATH"] = instruments["dir"] + os.pathsep + os.environ.get("PATH", "")
    os.environ["CLOUDLENS_TEST_AWS"] = instruments["answers"]
    creds_was = api.VC_CREDS_FILE
    api.VC_CREDS_FILE = os.path.join(instruments["dir"], "no-such-creds.json")

    httpd = server.serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    assert port and port != 8760, "the test server must never take the console's own port"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        api.VC_CREDS_FILE = creds_was
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _wait_for_port(port, timeout=5.0):
    """The listening socket is open before serve_forever runs (serve()
    binds and listens), so this only guards against a machine that has not
    scheduled the thread yet."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.25):
                return
        except OSError:                                 # pragma: no cover - a slow machine
            pass
    raise AssertionError("the console server never started listening on %d" % port)


@pytest.fixture(autouse=True)
def no_job_left_running(console):
    """Every job this test started is stopped and forgotten before the next
    one runs, through the console's own shutdown path. A fake engine
    blocked on its FIFO would otherwise outlive the test that asked the
    question."""
    yield
    entry._stop_jobs()
    server.JOBS.clear()


# ---------------------------------------------------------- what they say
@pytest.fixture
def aws(instruments):
    """What the stub aws answers, per subcommand: aws({"ec2 describe-vpcs":
    {...}}). Reset to empty for every test, so nothing leaks between
    them, and the argv log is cleared with it."""
    def say(answers):
        with open(instruments["answers"], "w") as fh:
            json.dump(answers, fh)

    def calls():
        try:
            with open(instruments["aws_log"]) as fh:
                return [l.strip() for l in fh if l.strip()]
        except OSError:
            return []

    say({})
    open(instruments["aws_log"], "w").close()
    say.calls = calls
    yield say
    say({})


@pytest.fixture
def engine(instruments, tmp_path, monkeypatch):
    """Point the console's deploy script at the fake engine and give it the
    frames to write: engine([{...}, {...}]). api.REPO moves to a temporary
    directory with it, so the profile /api/run writes (and the cwd the
    engine runs in) is the test's own and never the repository."""
    monkeypatch.setattr(api, "REPO", str(tmp_path))
    monkeypatch.setattr(api, "DEPLOY", instruments["engine"])
    events_file = tmp_path / "events-fixture.jsonl"

    def frames(events, exit_code=0):
        """Write the fixture events file the fake engine replays. Each
        entry is one line of it, in the shape deploy-stack.sh's own
        emit_event appends: {"seq":N,"ts":...,"type":T,...}, seq being the
        line's number in the file. Compact separators, as the script
        writes them: its own emit_done greets a done by matching the
        literal '"type":"done"', so a space after the colon would be a
        fixture the real reader does not recognise."""
        with io.open(str(events_file), "w", newline="\n") as fh:
            for n, ev in enumerate(events, 1):
                line = dict({"seq": n, "ts": "2026-09-09T00:00:0%dZ" % (n % 10)}, **ev)
                fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        monkeypatch.setenv("CLOUDLENS_TEST_EVENTS", str(events_file))
        monkeypatch.setenv("CLOUDLENS_TEST_EXIT", str(exit_code))
        return str(events_file)

    return frames


@pytest.fixture
def teardown_script(instruments, tmp_path, monkeypatch):
    """Point the console's teardown script at the fake one and give it the
    report to print: teardown_script(["...", "..."])."""
    monkeypatch.setattr(api, "TEARDOWN", instruments["teardown"])
    report = tmp_path / "orphans-report.txt"

    def says(lines):
        with io.open(str(report), "w", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        monkeypatch.setenv("CLOUDLENS_TEST_REPORT", str(report))
        return str(report)

    return says


@pytest.fixture
def kvo(instruments, monkeypatch):
    """Point api at the fake kvo_license module. _KL caches the import, so
    it is cleared here and restored by monkeypatch afterwards."""
    monkeypatch.setattr(api, "KVO_LICENSE", instruments["kvo_license"])
    monkeypatch.setattr(api, "_KL", None)
    return instruments["kvo_license"]


def pytest_configure(config):
    if expect is not None:
        expect.set_options(timeout=TIMEOUT)
