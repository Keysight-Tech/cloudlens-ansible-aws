# CloudLens Live Deployment Console

A local, loopback-only web app that runs the repo's **real** deploy automation and
streams live progress to a premium UI. Pick a flow, fill in your details, run it:
the network diagram wires itself up from real AWS state, the narration explains
each step as it happens, and the console shows the raw output underneath.

It's the interactive companion to the four automation tracks:

| Flow | Runs | Watches |
|---|---|---|
| **Launch full stack** | `deploy/deploy-stack.sh` | CloudFormation stack events (real) |
| **CLMS + sensors** | `vcontroller_project_key.py` + Ansible | each host registering |
| **KVO + vPB + sensors** | `kvo_adopt_clms.py` / `vpb_kvo_adopt.py` | CLMS CONNECTED, vPB Online |
| **AWS mirror session** | `kvo_aws_mirror.py` | collector + traffic mirror sessions |

## Run it

```bash
cd console
python3 -m cloudlens_console            # starts on http://localhost:8760, opens your browser
```

- **Live mode** runs the real scripts against **your shell's AWS identity**
  (`~/.aws` / env vars / CloudShell role). Credentials never leave the machine;
  the server binds to `127.0.0.1` only.
- **Demo mode** (toggle in the header, on by default) replays real captured event
  streams: no AWS, no boto3 needed. Great for walkthroughs and screenshots.

Requirements: Python 3.9+ and (for live mode) `boto3` and valid AWS credentials.

## How it stays honest

Every tick the UI shows maps to a **real** AWS state transition or a **real** line
of script output. When it's waiting, it says *waiting on AWS*: it never invents
progress. Failures are first-class: the failing node turns red and the real
`ResourceStatusReason` / fix is shown.

## Layout

```
console/
  cloudlens_console/
    __main__.py       # entrypoint: bind 127.0.0.1, preflight, open browser
    server.py         # stdlib HTTP + SSE (GET / , /flows , POST /run , GET /events/<id>)
    orchestrator.py   # subprocess + boto3 poller merged into one event stream; replay mode
    flows.py          # the 4 flows as data: inputs, diagram nodes, event→narration map
    events.py         # the typed event contract (hello/log/state/narrate/stat/done/error)
    web/              # the premium UI (index.html + app.js)
  fixtures/           # replay event streams (+ _build.py to regenerate)
  tests/              # unit tests (no AWS)
  tests/browser/      # browser smoke tests (playwright + chromium; not in the default run)
```

## Tests

```bash
cd console
python3 -m pytest tests -q            # the fast suite: no AWS, no browser
python3 -m pytest tests/browser -q    # the browser smoke tests
python3 -m pytest tests tests/browser -q      # both
```

`tests/` is the default run and never touches the network, an AWS account or a
browser. `tests/browser/` is **excluded from it** (`tests/conftest.py` collects
that directory only when the command line names it): those tests drive a real
chromium against a real server, they take about as long as the whole fast suite,
and they need a browser build most machines do not have.

Install what they need once:

```bash
pip install playwright && playwright install chromium
```

Without either, `python3 -m pytest tests/browser -q` **skips** with that command
in the reason; it never fails, and `python3 -m pytest tests -q` is unaffected.

### What the browser tests cover

Eight things a person can do, each asserted on what is on the screen - the row
that was drawn, the refusal under the button, the modal, the red phase:

1. the wizard walks six screens of defaults to the plan, and the profile the
   script would replay says `CLOUDLENS_INFRA="new"`
2. an existing-VPC plan lists the account's VPCs and subnets, and will not move
   past screen 1 until a management subnet is picked
3. the workloads screen shows the live count and rows for the instances the
   account answers with for the discovery tag
4. Launch posts the plan, the page moves to Watch, and the timeline is drawn
   from the run's own frames
5. a `prompt` frame opens the modal; the answer goes down the prompt FIFO to the
   engine and the modal clears
6. a failed phase turns its row red and carries the script's own reason, and the
   banner repeats it with the phase and the exit code
7. the teardown stays disabled, saying so, until the stack name is typed back
   exactly
8. an activation code the KVO does not recognise is a row in the table, not an
   error page, and is shown only by its last four characters

They start the server on a port the OS picks (never 8760, so a console you are
already running is untouched) and need no AWS credentials, no network and no
KVO: the `aws` CLI is a stub executable on `PATH`, and the deploy and teardown
scripts are fake engines that write the frames a test chose. Everything between
those two ends is the real thing: the API, the profile writer, the engine
runner, the events file, the prompt FIFO, the SSE stream and every line of the
page. `tests/browser/conftest.py` says exactly what is stood in for and why.

## Regenerate demo fixtures

```bash
python3 fixtures/_build.py
```
