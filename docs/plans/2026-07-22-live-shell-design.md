# Live deployment console on the public docs site

Design, 2026-07-22. Status: approved, not yet implemented.

## Problem

The docs site should let a visitor run a real CloudLens deployment and watch it
happen, on the page. Today the embedded console replays real captured events: it
is honest but it is not live.

The site is static GitHub Pages. There is no backend and we do not want one.

## Audience

1. A Keysight SE demonstrating to a customer, from their own laptop and AWS account.
2. A customer evaluating CloudLens, deploying into their own account.

Both deploy into the visitor's account. A public sandbox running in Keysight's
account was rejected: it turns this into an abuse-control, cost-cap and
tenant-isolation project rather than a docs feature.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Credentials | Never in the browser | The console already inherits the visitor's shell credentials. Nothing is transmitted to Keysight. Pasting AWS keys into a web page is a habit we refuse to teach. |
| Execution scope | Guided flows only | The page sends a flow id, never a command string. A malicious site probing localhost can at worst start a CloudLens deploy, not run code. |
| Trust | Pairing code, guess-capped | Explicit human consent, with CORS origin pinning as a second layer. The code is 8 characters (40 bits) and pairing disables itself after 10 consecutive failures. |
| Transport | Page to local console over 127.0.0.1 | No hosted infrastructure. Reuses the orchestrator, flows, events contract, SSE and UI unchanged. |

Rejected: an outbound relay to a hosted service. It survives networks that block
page-to-localhost and would let an SE project the page while deploying
elsewhere, but it needs infrastructure we would then operate, and every deploy
event would transit a third party. Reconsider only if the bridge proves blocked
in customer browsers.

## Threat: the code is grindable without a cap

Code review surfaced this and it changed the design. The attacker is not offline.
A malicious page the visitor happens to open can also POST to `127.0.0.1:8760`
and grind `X-CloudLens-Pair`. Loopback has no network cost and the pairing check
runs before body parsing, so a browser can sustain thousands of guesses per
second. At 6 characters (30 bits) the mean time to compromise is days, not
centuries, and "short-lived" only helps if sessions are short: an SE who leaves
the console running through a demo week erodes the margin.

Two controls, both cheap:

- 8 characters, 40 bits. Two more characters typed once per session, 1000x margin.
- After 10 consecutive bad codes the process discards `PAIR_CODE`, so pairing
  fails closed until the console is restarted. This also converts a silent
  attack into an observable one: the console prints a warning naming the
  attempt count.

The cap is what carries the weight. The length is defence in depth.

## Architecture

```
https://keysight-tech.github.io/cloudlens-ansible-aws   (static)
  console panel  --probe-->  http://127.0.0.1:8760/health
                 --POST--->  /run    {flow, inputs} + X-CloudLens-Pair
                 <--SSE----  /events/<job_id>
                                     |
                     cloudlens_console (visitor's machine)
                       orchestrator -> deploy-stack.sh / boto3
                                     |
                              their AWS account
```

Three states. Replay is the default and the fallback, so the page is never
broken for the majority of visitors who have no console running.

| State | Trigger | Behaviour |
|---|---|---|
| Replay | no console answers | ships today, plus a "Go live" strip with the start command |
| Pairing | console answered, no valid code | prompts for the code the console printed |
| Live | code accepted | real SSE, real deploys, LIVE badge with account and region |

## Components

`server.py`
- `GET /health` returns `{ok, version}` only. It is CORS-open so the probe works
  without a code, therefore it must leak nothing. Account and region move behind
  pairing.
- CORS pinned to the Pages origin plus `http://127.0.0.1:8760`.
  `Access-Control-Allow-Headers: X-CloudLens-Pair`.
  `Access-Control-Allow-Private-Network: true` on the preflight.
- Pairing enforced on `/run`, `/flows`, `/stop/`. Constant-time compare.

`__main__.py`
- Generates a short random pairing code per process, prints it in the banner,
  memory only. A stale tab cannot drive a fresh console.

`web/index.html` + `app.js`
- Shared by the local console and baked into `docs/console.html` by
  `build_site.py`, so one change serves both.
- Adds a bridge concern: probe on load, hold the code in `sessionStorage`, swap
  the data source between the replay engine and `EventSource`.
- Served from the console itself it is same-origin and skips pairing, so the SE
  running locally sees zero friction.
- All user-facing strings live in one table from the start, so translation later
  is a data change and not a refactor.

`build_site.py`
- Bakes the bridge client into `docs/console.html` beside the existing replay
  fixtures, which remain the fallback.

The orchestrator, flows and events contract do not change. The live path is the
code that runs today; it gains a second consumer.

## Data flow

`EventSource` cannot send custom headers, so the pairing code cannot ride on the
SSE stream. Rather than put it in a query string, where it lands in logs, the
`job_id` is the capability: a cryptographically random token handed out only to
a paired caller.

```
probe    GET  /health                       -> {ok, version}   no auth, leaks nothing
pair     GET  /flows    X-CloudLens-Pair    -> 200 or 401
run      POST /run      X-CloudLens-Pair    -> {job_id}  random, unguessable
stream   GET  /events/<job_id>              -> SSE, no header required
```

Knowing the job id is the authorization. This leaves `app.js` on `EventSource`
unchanged.

## Failure modes

| Failure | Handling |
|---|---|
| No console running | Silent, fall to replay. The common case, never an error |
| Chrome Private Network Access blocks the preflight | Detect this specific failure and name it with the fix. Do not render a generic connection error |
| Wrong or stale pairing code | 401, "that code did not match, check the console banner". Codes are per-process |
| Port 8760 is another service | `/health` shape does not match, treat as not-our-console, stay in replay |
| Console dies mid-run | `EventSource.onerror`, report lost connection and keep the transcript on screen |
| AWS credentials missing or expired | Already covered by the orchestrator `sts:GetCallerIdentity` preflight, surfaced as an error event |
| Deployment fails | Already covered by the events contract, rollback surfaces as-is |

Governing rule: absence of a console is never an error. Only an explicit user
action that cannot be honoured produces a message.

## Testing

Python, extending `tests/test_console.py`
- `/health` returns only `{ok, version}`. A regression here leaks account data to
  any origin.
- Preflight carries `Access-Control-Allow-Private-Network: true`.
- CORS origin pinned: a request from another origin is rejected.
- `/run` without a valid code returns 401, with one returns 200.
- `job_id` entropy: no collisions, not sequential.

Client
- Extract `probe -> pair -> live -> degraded` as a pure function over
  `(probeResult, code, sseState)` and test it in Node without a browser.

Browser, Playwright
- console down: replay renders, no visible error
- console up: pairing prompt, bad code shows 401 message, good code goes live
- console killed mid-stream: transcript survives and a message appears

Cross-browser: Chrome and Edge for Private Network Access, Safari, Firefox. PNA
behaviour differs across all three and is the most likely field failure.

Once, for real: drive a genuine flow against live AWS from the public page and
confirm the events are real rather than replayed.

## Notes

The premium quality comes from the honesty rule already in the console: every
tick on screen is a real AWS state transition or a real stdout line, and waiting
shows "waiting on AWS" rather than a fake tick. With the LIVE badge showing the
visitor's own account and region, and the fabric diagram wiring itself from real
CloudFormation events, the customer is watching their own infrastructure being
created.

Multilingual support for the regions Keysight operates in is planned but out of
scope here. The only concession made now is the single string table.
