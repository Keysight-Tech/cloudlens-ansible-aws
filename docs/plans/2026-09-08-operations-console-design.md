# CloudLens Autopilot Operations Console (AWS)

Design, 2026-09-08. Status: approved.

## Problem

The AWS automation is complete and proven, but its only face is a terminal:
an interview, flags, and log output. For a Keysight SE that is workable; for
a customer's network engineer it reads as "too technical", and the people who
sign off never see it at all. Competitors put a wizard and a live topology in
front of the same kind of automation (GigaVUE-FM: fabric launch wizard,
monitoring domains, single-pane topology, reached at a URL inside the
customer's cloud account). We have the harder half, the automation that
actually works end to end; what is missing is the face.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Where it runs | Both: the operator's laptop AND an appliance in the customer's AWS account | The laptop console serves SEs today with zero hosting; the appliance removes the terminal entirely for customers |
| Build order | Console v2 first, appliance second | 90% of the work is the UI and the event stream, all provable on a laptop against a real account; the appliance is a thin shell around the identical server |
| Scope | Deploy, Watch, Operate, Teardown (fleet view later) | The complete lifecycle one deployment needs; every piece already exists as a script |
| Appliance access | Public HTTPS behind the admin CIDR, password in SSM as a stack output | Identical to how the vController and KVO are reached today; private-only via SSM port-forward is one later parameter |
| Technology | Evolve the existing stdlib-Python console and vanilla-JS UI | No build toolchain in a repo people curl into CloudShell; one codebase; the July console already has the SSE server, diagram and narration |
| Contract between UI and engine | The wizard writes a `deploy-profile-<stack>.env`; the engine is `deploy-stack.sh --profile`; the engine emits structured events | UI and CLI become two faces of one engine and cannot drift; any UI plan can be re-run from the CLI with one flag |
| Secrets | Entered on the screen that needs them, passed as environment for that run only, never filed | The profile's exact allowlist already excludes every secret key |

Rejected: a new React/FastAPI product (build toolchain, duplicate codebase);
a container as the unit for both runtimes (Docker on SE laptops, Fargate and
an ALB in the account); a Keysight-hosted portal (abuse control, cost caps,
tenant isolation; rejected already in the July live-shell design).

## Architecture

```
  SE laptop                          Customer AWS account
  python3 -m cloudlens_console       Launch Stack: cloudlens-autopilot.yaml
  127.0.0.1:8760, shell creds        EC2 t3.small, instance role, HTTPS :443
          |                                    |
          +------------- same code ------------+
                            |
              cloudlens_console (stdlib Python)
                 /api/doctor  /api/discover  /api/plan  /api/run
                 /events/<job>  (SSE)   /api/teardown  /api/licences
                            |
              deploy-stack.sh --profile <plan> --events <file>
              teardown-stack.sh, kvo_license.py, deploy-eks-tapping.sh
```

The UI never invents a step: every screen is a face on a script that runs in
production. Discovery (VPCs, subnets, tag match counts, EKS clusters) uses the
same AWS queries the interview uses. The docs site keeps its "Go live" bridge
to a laptop console unchanged.

## Screens

### Pre-flight
`--doctor` as a page: one row per check, PASS / WARN / FAIL, the fix beside
each failure, a Re-check button. Deploy is disabled while a FAIL stands.

### Deploy (the wizard)
The interview, visually, in the same dependency order:

1. Where: new VPC, or an existing VPC picked from a table (id, CIDR, name)
   with its subnets (id, AZ, CIDR, public/private) as a second table.
2. Components: vController always; KVO and vPB as switches; vController
   capacity (t3.xlarge recommended / m5.xlarge), KVO and vPB sizes stated as
   fixed with the reason (the only types the Marketplace images permit).
3. Tapping: sensors / mirroring / both / none, with the one-line explanation
   of each; when KVO and sensors are both on, the management-plane choice.
4. Workloads: existing (tag key=value, the live match count updating as you
   type, the VPC list they live in) / test workloads with per-OS counts /
   later. Collector placement appears only when mirroring into an existing
   VPC (three subnet pickers, zone derived from the mgmt subnet).
5. Kubernetes: none / existing EKS cluster picked from a listing / sample
   cluster; DaemonSet or sidecar with the one-paragraph difference.
6. Plan: exactly the CLI's "Resolved configuration", plus the profile it will
   write, plus the equivalent CLI command. One Launch button.

Launch writes `deploy-profile-<stack>.env` (only the allowlisted keys) and
starts `deploy-stack.sh --profile <file> --events <file>` non-interactively
with the run's secrets in its environment.

### Watch
Rendered from the event file only:
- a phase timeline (the 17 phases; "waiting on AWS" while the stack builds)
- the topology drawing itself as `resource` events arrive: VPC, subnets,
  vController, KVO, vPB with its three NICs, mirror collectors, tapped
  workloads, EKS nodes
- logins as they become ready (`login` events)
- prompts as modals (`prompt` events; the answer returns to the run over a
  named pipe the console owns)
- a raw log drawer for anyone who wants it
A failed phase turns its node red and shows the same fix text the CLI prints.

### Operate
One screen per deployment, built from live queries, never from memory of the
run: topology with current state (instances, sensors registered via the
vController API, mirror sessions via `describe-traffic-mirror-sessions`, vPB
counters over SSH when the key is present), logins, and buttons that map to
existing commands: Re-run phase (`--only <phase>`), Resume (same stack name),
Verify traffic (the checks the CLI prints).

### Licensing
The batch flow as a form: paste all codes, one KSM sweep, a table of what
each holds, quantity per entitlement, Activate; Release per code
(`operations/deactivate`, proven live).

### Teardown
Shows what attribution found (`--orphans`, read-only) before asking. Releases
licences first, runs `teardown-stack.sh`, streams the same events, ends with
the verify-empty proof (instances, VPCs, volumes, stacks). Nothing destructive
without the stack name typed on the page.

## The event contract

`deploy-stack.sh --events FILE` appends JSON lines at the points where the
script already knows the truth. No flag, no file; the terminal output is
unchanged.

| event | emitted from | payload |
|---|---|---|
| `phase` | `state_phase` | `{name, status: start\|done\|failed\|skipped, reason?}` |
| `resource` | the CloudFormation poller and discovery | `{kind, id, name?, ip?, subnet?, status}` |
| `check` | doctor | `{item, status: pass\|warn\|fail, fix?}` |
| `prompt` | the few mid-run inputs | `{id, question, kind: text\|secret\|yesno}` |
| `login` | when an address is ready | `{component, url, user, where_the_password_is}` |
| `done` | end of run | `{status, report, profile}` |

Events are append-only with a monotonic `seq`; the SSE stream resumes from
the last `seq` a browser saw, so a lost connection never loses the run and
the run never depends on the browser.

## The appliance (second deliverable)

`deploy/cloudformation/cloudlens-autopilot.yaml`, a fourth Launch button:
- one t3.small (Amazon Linux 2023) with an instance role scoped to what the
  deploy needs (the actions in `deploy/iam/` plus CloudFormation)
- user-data installs Python dependencies, clones the repo at a pinned tag,
  generates a self-signed certificate and a password (stored in SSM Parameter
  Store), and runs the console on :443
- a security group admitting only the admin CIDR on 443
- stack outputs: the URL and the command that reads the password from SSM
- login is a session cookie; every action is logged
- updating is re-running the button with a newer tag
The appliance is itself a stack, so the existing teardown removes it with the
same discipline.

## Error handling

Errors are events, never modal dead ends: a failed phase shows the CLI's fix
text and the matching button (Retry, Resume, Doctor). A lost console
connection shows "reconnecting" and resumes from the last event; the run
continues regardless. A profile the UI writes is validated by the same
allowlist the CLI enforces, so the UI cannot produce a plan the CLI refuses.

## Testing

1. Unit: the event contract (every emitter, every field), and the profile
   round-trip (wizard -> profile -> `--profile` replay yields the identical
   Resolved configuration).
2. Harness: the pty driver with the stubbed AWS CLI runs the wizard end to
   end with no account, including the brownfield path.
3. Browser: Playwright smoke tests for what a person can do on each screen.
4. Live: the lab run of 2026-09-02 (greenfield full stack, then a brownfield
   deployment into it) driven from the UI, ending in the verified-empty
   teardown.

## Out of scope for this iteration

Fleet view across accounts, private-only appliance access via SSM
port-forward, Azure. Each is a later layer on the same contract.
