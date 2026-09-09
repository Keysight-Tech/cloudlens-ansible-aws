# The CloudLens console

The operations console for the AWS automation in this repo. It runs on your own
machine, binds loopback only, and drives the same scripts the CLI drives: the
wizard's output is a profile file `deploy/deploy-stack.sh` replays, so nothing
done in the browser is anything you could not have typed at a shell.

It is for the Keysight SE standing up a demo or a customer pilot, and for the
network engineer who has to run it again next quarter without reading three
thousand lines of bash first.

## Run it

```bash
cd console
python3 -m cloudlens_console              # http://localhost:8760, and opens your browser
python3 -m cloudlens_console --port 8890  # somewhere else
python3 -m cloudlens_console --no-open    # start it, leave the browser alone
```

- It binds `127.0.0.1` and only that. There is no password and no TLS yet, so
  leave `--host` alone: binding it to an address other people can reach hands
  them your AWS identity.
- It inherits your shell's AWS identity: `~/.aws`, the environment, an SSO
  session, a CloudShell or instance role. It never asks for a credential, never
  stores one, and never sends one anywhere.
- Requirements: Python 3.9+ and its standard library, plus the `aws` CLI, which
  is what every operations screen shells out to. `boto3` is needed only by the
  legacy quick flows' CloudFormation poller, and the replay demo needs neither.

Ctrl-C stops the server and every run it started. Each engine runs in a session
of its own, so the terminal's Ctrl-C stops at the console; the shutdown then
signals each running job's group, because a deploy blocked on a question would
otherwise outlive the page that was going to answer it.

## The six screens

Every screen is a face on a command that already exists. None of them knows AWS
on its own.

| Screen | The command underneath |
|---|---|
| **Pre-flight** | `deploy-stack.sh --doctor` |
| **Deploy** | the `aws` CLI for discovery, then `deploy-stack.sh --profile` |
| **Watch** | the `--events` file and the `--prompt-pipe` FIFO of the run in progress |
| **Operate** | the `aws` CLI, the vController REST API, ssh to the vPB; then `--profile --resume` |
| **Licensing** | `scripts/kvo_license.py`'s own functions against the KVO REST API |
| **Teardown** | `teardown-stack.sh --orphans`, then `teardown-stack.sh --yes` |

**Pre-flight** is `--doctor` as a page: subscriptions, quotas, the key pair,
tooling, network, one row per check with its exact fix beside a failure. It is
read-only and leaves nothing behind. The wizard's Launch stays off while a FAIL
stands for the plan's region.

**Deploy** is the interview as six steps: where (stack name, region, key pair,
new VPC or one you already run), which components (vController always, KVO and
vPB behind switches), how to tap (sensors, agentless mirroring, both, neither),
what to tap (your tagged instances, throwaway test VMs, or decide later),
Kubernetes (none, an existing EKS cluster, or a small sample one), and the plan.
Every listing on those screens is a real `aws` call against your account: your
VPCs, your subnets with whether each one has a route to an IGW, the instances
that actually carry the discovery tag, your EKS clusters. The last screen is the
plan: the resolved value of every profile key, the profile text itself, and the
CLI line that does the same thing.

**Watch** is the run as the engine reports it. The phase list is the script's
own `PHASE_ORDER`, in its order; the addresses and logins are the ones the
script reported; a question the script asks stops the run and opens a modal, and
the answer goes back down the prompt FIFO. Nothing on that screen is a timer or
a guess. A reload picks the run up again by its id and the stream replays it
from the first event, so nothing is missed.

**Operate** is a stack that is already up. Every card carries either what it
read or the reason it could not, with the command that would: no missing answer
is ever drawn as a zero. The sensor count comes from the vController's own REST
API through the credentials file the deploy wrote; the vPB counters need ssh
with the EC2 key pair, and without that key the card carries the command instead
of a number. Its two buttons replay the profile this stack was deployed from,
the same file and the same script: **Resume** is `--profile ... --resume` and
skips what the account proves is done, **Re-run this phase** adds `--only PHASE`
and forces exactly that one.

**Licensing** is the KVO's activation codes: check what a code holds, activate
what you need, release what an appliance is about to take with it. Check first,
every time: activating spends entitlement quantity and a consumed code does not
give it back.

**Teardown** is an order, and the order is the point: audit what the stack would
leave loose, release its licences if it has a KVO, type the name back, run, then
count what is left in the region.

The **quick flows** fold above the screens is the older four-flow demo, and the
Demo switch in the header replays real captured event streams with no AWS calls
at all. It is what the public page at `docs/console.html` is built from.

## The profile contract

One file is the whole contract between the wizard and the script.

- `deploy/profile-keys.txt` is the exact allowlist of keys a profile may set. It
  is read by both sides: `deploy-stack.sh` (`profile_key_allowed`, which keeps a
  built-in copy for a bare `curl | bash`) and the console
  (`cloudlens_console/profile.py`). A key added to one is added to the other by
  construction, and `tests/test_profile.py` holds the script's fallback copy to
  the same list.
- The Plan screen renders that file from your answers and shows it to you before
  anything runs. Launch writes it as `deploy-profile-<stack>.env` next to the
  deploy script, mode 600, and starts
  `bash deploy/deploy-stack.sh --profile deploy-profile-<stack>.env`.
- So every run is reproducible from a terminal with one flag, on any machine
  that has the file, with no question asked. And the UI can never produce a plan
  the CLI would refuse: it is the same allowlist, validated against the script's
  own rules (the stack-name pattern in `api.py` is `deploy-stack.sh`'s
  `valid_stack_name()`, and `tests/test_api.py` runs the samples through bash to
  keep them equal).
- The file also decides where a run happens: `--profile` is the whole argv. A
  replay whose profile names a different stack or region than the screen is
  refused with both values rather than run somewhere you are not looking.

## The event contract

- `deploy-stack.sh --events FILE` appends one JSON object per line,
  `{"seq":N,"ts":"...","type":T,...}`, at the points where the script already
  knows the truth. The types are `hello`, `phases`, `phase`, `resource`,
  `check`, `prompt`, `login` and `done`. A `login` event says where the password
  lives, never the password.
- `deploy-stack.sh --prompt-pipe FIFO` makes the page the terminal: each
  question goes out as a `prompt` event and the answer comes back on the pipe.
  It needs `--events`, and it refuses anything that is not a real named pipe.
- The console creates both in a temp directory of that run's own (the FIFO mode
  600), tails the file while the process runs, and re-emits each line as a
  console event stamped with its own monotonic id, so `Last-Event-ID` resumes
  the SSE stream across both sources without a gap.
- Watch renders those events and nothing else. A type the console does not know
  arrives as a log line carrying the raw text, so nothing the script says is
  dropped on the floor.

That is what keeps it honest. Every tick on the screen is a line the script
wrote or a state AWS reported. When it is waiting, it says it is waiting; it
never invents progress, and a failure is first-class: the phase turns red and
carries the script's own reason and exit code.

## Secrets

- Four names are secrets the script reads from its environment:
  `CLOUDLENS_VC_PASSWORD`, `CLOUDLENS_KVO_ADMIN_PASS`,
  `CLOUDLENS_MIRROR_ACCESS_KEY`, `CLOUDLENS_MIRROR_SECRET_KEY`. Any other name
  offered as a secret is refused, and so is a profile key offered as one.
- They are typed on the screen that needs them and passed as the environment of
  that one run. Never the profile file (the allowlist excludes them, and
  `tests/test_profile.py` holds a NEVER list to keep it that way), never the
  events file, never a log line, never a response body.
- KVO activation codes have no environment name at all: the script takes them
  only as `--kvo-codes CODE[,QTY]`, so they ride that run's argv, exactly as
  typing the flag by hand would. `kvo_license.py` needs a TTY to prompt and the
  engine gives it none, which is why the licence phase cannot run without them.
- Every code and every secret is registered with the job, and the engine redacts
  them out of the stream before a byte reaches the browser.
- Nothing is stored in the browser. A code is shown only by its last four
  characters, in a password field (a textarea shows the whole list in clear on
  browsers that ignore the masking). The KVO address and password on the
  Licensing screen are posted in the request body and kept nowhere, so a reload
  asks again.

## Licences, and what the teardown gate requires

Release before deleting a KVO. Release is
`POST /api/v2/licensing/operations/deactivate`, polled to its end, and it puts
the counts back in the pool. A KVO deleted with licences still installed strands
those counts, and after it is gone there is nothing left to ask. This is not
tidying up; it is the difference between having the entitlement next quarter and
not.

The Teardown screen enforces the order:

- The audit is `teardown-stack.sh --orphans`. Read-only, deletes nothing, needs
  no confirmation. It is what names the volumes the Marketplace AMIs leave
  behind, the security groups that make a VPC undeletable, and the collector
  auto-scaling groups KVO builds outside CloudFormation.
- The destructive run needs the stack name typed back exactly, character for
  character. It runs with `--yes`, because the engine has no terminal to confirm
  on.
- `--accept-licence-loss` is added only when this session actually released that
  stack's KVO licences, from that KVO. Without it a stack holding a KVO stops at
  the script's own licence warning, which is the right outcome.
- Afterwards, a region-wide count of what is left. Region-wide on purpose: a
  teardown's own sweep is what ties a resource to a stack, so this is the
  independent check that it worked.

## Tests

```bash
cd console
python3 -m pytest tests -q                # the fast suite: no AWS, no browser
python3 -m pytest tests/browser -q        # the browser smoke tests
python3 -m pytest tests tests/browser -q  # both

bash ../deploy/tests/test_events.sh       # the --events sink, under bash 3.2 and yours
bash ../deploy/tests/test_prompt_pipe.sh  # the prompt FIFO, and that stdin is never read
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

The two bash suites are the other half of the contract above: they run the real
`deploy-stack.sh` with no credentials of any kind in the environment, and prove
the events file is valid ordered JSON with exactly one `done` per run, and that
with a prompt pipe the script asks on the events channel and never reads stdin.

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

## What is proven, and what is not

Proven, live, in a real AWS account, by the CLI this console drives: the full
stack (vController + KVO + vPB), Linux and Windows sensor installation, KVO
adoption and licensing, and an agentless VPC Traffic Mirroring session verified
packet by packet. `docs/` carries the runbook and the evidence.

Not proven yet, and this file will say so until it is:

- **The console's own live run.** Every screen is exercised against a stubbed
  `aws` CLI and fake engines, and the engine it starts is the same script that
  deployed the stacks above, but the console has not yet driven a deploy to
  completion against a real account. That is Task 16 of the plan.
- **The EKS and KVO Kubernetes paths.** The wizard collects them and the profile
  carries them; they have not been run against a real cluster.

Not built yet:

- **Hosting.** There is none. This is a program on your laptop.
- **A password and HTTPS.** Not there. That is why it binds loopback only.
- **An in-account appliance.** The design has one, reached at a URL inside the
  customer's account. It does not exist. Anyone who wants the console today runs
  it themselves, next to their own credentials.

## Layout

```
console/
  cloudlens_console/
    __main__.py       # entrypoint: bind 127.0.0.1, open the browser, stop the jobs on Ctrl-C
    server.py         # stdlib HTTP + SSE: the pages, /api/*, /events/<job>
    api.py            # every route, each one a face on an existing command
    profile.py        # deploy-profile-<stack>.env, from the one allowlist
    orchestrator.py   # the engine: --events tail, --prompt-pipe, redaction, replay
    events.py         # the typed event contract, both directions
    flows.py          # the legacy four quick flows as data
    web/              # the UI: index.html plus one script per screen, no build step
  fixtures/           # captured event streams for the replay demo (+ _build.py)
  build_site.py       # writes docs/console.html: this page with the operations block cut out
  tests/              # the fast suite
  tests/browser/      # the browser smoke tests (not in the default run)
```

Regenerate the demo fixtures with `python3 fixtures/_build.py`, and the public
page with `python3 build_site.py` after any change to `web/index.html`. The
build asserts that what it publishes carries none of the operations screens, no
`/api/` path and no secret field: GitHub Pages serves no backend, so a wizard
that survived the cut would be a page of dead buttons and password fields with
nowhere to send what is typed into them.
