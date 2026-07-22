# Live shell bridge: handoff

Written 2026-07-22 at the end of the first implementation session.

## Start here

```bash
cd ~/cloudlens-live-shell            # worktree, branch feat/live-shell-bridge (pushed)
cd console && python3 tests/test_console.py     # expect: 37 tests passed
```

Then invoke `superpowers:executing-plans` against
`docs/plans/2026-07-22-live-shell-implementation.md`.

**There is no pytest.** `tests/test_console.py` ends in its own stdlib runner and
executes every `test_*` function. You cannot select a single test by name. The
console is dependency-free (stdlib + boto3) and must stay that way.

## Where things stand

| Task | State |
|---|---|
| 1 pairing code | done, both reviews passed |
| 2 `/health` | done, both reviews passed |
| 3 CORS + Private Network Access | done, both reviews passed |
| 4 pairing enforcement + Host check | **code complete at `cb717a0`, NOT yet re-reviewed** |
| 4b smaller items | not started |
| 5-12 | not started |

**Resume at Task 4's re-review.** The fix commit `cb717a0` addressed a critical
clickjacking bypass and has not been through a reviewer since. Do that before
starting Task 4b.

## What Task 4 currently implements

- `_check_pairing()` gates `/flows`, `/run`, `/stop/`. Exempt: `/health`,
  `/events/<job_id>`, `do_OPTIONS`.
- `_host_is_ours()` / `_host_name()` reject a non-loopback `Host` with 403
  before routing, in `do_GET`, `do_POST` and `do_OPTIONS`.
- `_cors()` is the single choke point for CORS plus `frame-ancestors 'none'` and
  `X-Frame-Options: DENY`, and suppresses ACAO entirely when the Host check fails.
- Guess handling: 0.5s delay per wrong guess as the primary control, with a
  cumulative 200-guess cap as a backstop that sets `PAIR_CODE = None`. A capped
  console answers 403 with a distinct message so the page can tell the visitor
  to restart rather than retype.
- `LOOPBACK_ONLY`, set by `serve()`, gates the absent-Origin exemption.

## Things that will bite you

**Do not header-gate `/events/`.** `EventSource` cannot send custom headers.
The `job_id` is the capability: 128 bits once Task 5 lands, and the only issuer
is `POST /run`, which is gated.

**Do not gate `do_OPTIONS` with pairing.** A preflight cannot carry the pairing
header; proving the header may be sent is what the preflight is for. Guarding it
401s every preflight and silently kills the bridge.

**`--allow-remote` is currently non-functional** under the Host guard: a LAN
client gets `403 bad host`. `LOOPBACK_ONLY` is the tripwire. If 4b makes remote
binds work, the absent-Origin exemption MUST be replaced at the same time, or
the console becomes an unauthenticated remote deploy endpoint.

**A background agent owns its worktree.** Do not stash, checkout or otherwise
mutate the tree while a subagent is mid-edit. That happened in this session; the
agent detected it and recovered, but it cost a full re-verification.

## The security story, corrected

CORS is defence in depth, **not** the boundary. `https://keysight-tech.github.io`
is the origin of every GitHub Pages site under the Keysight-Tech account, and the
same-origin policy has no path component, so any page on that origin reaches this
console. The pairing code is the actual gate. Everything hardening it (the delay,
the cap, the Host check, the framing headers) is load-bearing for that reason.

## Findings from session one

Thirteen defects, most of them in the plan rather than the implementations:

1. Clickjacking: a framed console is same-origin with itself, so the framed page
   inherited the pairing exemption. One click became a real AWS deploy.
2. DNS rebinding broke the premise of the absent-Origin exemption.
3. CORS was described as the boundary; it never was.
4. The planned exemption would have 401'd the local UI's own Run button, because
   browsers always send `Origin` on POST.
5. SSE carried no CORS headers, so the live stream would have been blocked.
6. `/health` leaked `Python/3.11.9` while its test passed.
7. `--host 0.0.0.0` was accepted while the docstring claimed loopback-only.
8. The guess cap reset on success, so a paired tab cleared an attacker's count.
9. The cap warning printed on every request, burying deploy output.
10. The cap warning sat in a block buffer and never printed when redirected.
11. `compare_digest` raised on non-ASCII header bytes.
12. The plan's IPv6 `Host` snippet was unreachable and would have 403'd its own UI.
13. Disabled pairing was indistinguishable from a typo.
