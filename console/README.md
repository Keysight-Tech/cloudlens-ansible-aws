# CloudLens Live Deployment Console

A local, loopback-only web app that runs the repo's **real** deploy automation and
streams live progress to a premium UI. Pick a flow, fill in your details, run it —
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
  streams — no AWS, no boto3 needed. Great for walkthroughs and screenshots.

Requirements: Python 3.9+ and (for live mode) `boto3` and valid AWS credentials.

## How it stays honest

Every tick the UI shows maps to a **real** AWS state transition or a **real** line
of script output. When it's waiting, it says *waiting on AWS* — it never invents
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
```

## Regenerate demo fixtures

```bash
python3 fixtures/_build.py
```
