# Operations Console Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A product-grade operations UI for the AWS automation, as two faces of one engine: the deploy scripts emit structured events and consume the wizard's profile, so the UI and the CLI cannot drift.

**Architecture:** `deploy/deploy-stack.sh` gains `--events FILE` (JSON lines: phase/resource/check/prompt/login/done, monotonic `seq`) and `--prompt-pipe FIFO` (mid-run prompts answered from the UI). The existing stdlib console (`console/cloudlens_console`) grows an API (`/api/doctor`, `/api/discover`, `/api/plan`, `/api/run`, `/api/answer`, `/events/<job>` with resume, `/api/teardown`, `/api/licences`) and a six-screen wizard whose output is the allowlisted `deploy-profile-<stack>.env` replayed by `deploy-stack.sh --profile`. Deliverable 2 wraps the identical server in a CloudFormation appliance (t3.small, HTTPS on 443 behind the admin CIDR, password in SSM).

**Tech Stack:** bash 3.2-compatible shell, Python 3.9+ stdlib (`http.server`, `ssl`, `json`, `subprocess`), vanilla HTML/JS (no build step), pytest, expect + a stubbed `aws` for the harness, Playwright (Python) for browser smoke, CloudFormation + cfn-lint.

**Design:** `docs/plans/2026-09-08-operations-console-design.md`

**Conventions that apply to every task:** no em dashes anywhere; no AI attribution in commits; commit messages explain WHY (the repo's style); every bash change passes `bash -n`; every Python change passes `python3 -m pytest console/tests -q`.

---

## Deliverable 1: Console v2

### Task 0: Fold in the unmerged console fixes

The branch `feat/live-shell-bridge` carries console fixes (`5daf0a3` inputs thrown away, `c9bfd0c` flags the script does not accept, `83eb1c5` stopped replay reported as success, `51310d9` browser smoke tests) that main lacks. Take the console fixes only; the bridge itself is unchanged by this plan.

**Files:**
- Modify: `console/cloudlens_console/*` (via cherry-pick)

**Step 1: See what the branch changes under console/**

Run: `cd ~/cloudlens-ansible-aws && git log --oneline main..feat/live-shell-bridge -- console/ | cat`
Expected: the commit list above.

**Step 2: Cherry-pick the console commits, oldest first**

Run: `git cherry-pick 83eb1c5 51310d9 c9bfd0c 187059f 5daf0a3`
Expected: clean picks (they touch only `console/`). If one conflicts, resolve keeping the branch's version of the console file, then `git cherry-pick --continue`.

**Step 3: Run the console tests**

Run: `cd console && python3 -m pytest tests -q`
Expected: all pass.

**Step 4: Commit** (the cherry-picks are the commits; push)

Run: `git push origin main`

---

### Task 1: The event sink in deploy-stack.sh

One helper, one flag. Every later emitter calls `emit_event`.

**Files:**
- Modify: `deploy/deploy-stack.sh` (near `state_set()`, and the arg parser)
- Test: `deploy/tests/test_events.sh` (new; bash, no AWS)

**Step 1: Write the failing test**

```bash
#!/usr/bin/env bash
# deploy/tests/test_events.sh: the --events sink writes valid, ordered JSON lines.
set -u
cd "$(dirname "$0")/../.."
S=$(mktemp -d)
export AWS_PROFILE=autopilot
# --dry-run touches no AWS; the events file must still describe the run
bash deploy/deploy-stack.sh --dry-run --region us-east-1 --key-name k --stack-name evt \
  --tapping none --no-kvo --no-vpb --events "$S/events.jsonl" </dev/null >/dev/null 2>&1
python3 - "$S/events.jsonl" <<'PY'
import json, sys
seqs, types = [], []
for line in open(sys.argv[1]):
    ev = json.loads(line)            # every line is one JSON object
    seqs.append(ev["seq"]); types.append(ev["type"])
    assert "ts" in ev and "type" in ev
assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be strictly increasing"
assert types[0] == "hello", types[:3]
assert "phase" in types and types[-1] == "done", types
print("PASS events: %d lines, %d phase events" % (len(seqs), types.count("phase")))
PY
```

**Step 2: Run it to verify it fails**

Run: `bash deploy/tests/test_events.sh`
Expected: FAIL (`--events` is an unknown flag).

**Step 3: Implement the sink**

In `deploy/deploy-stack.sh`, after the `state_set()` function, add:

```bash
# ---------------------------------------------------------------------
# Structured events: the side channel the operations console renders.
# --events FILE appends one JSON object per line at the points where this
# script already knows the truth (phase changes, discovered resources, doctor
# checks, prompts, logins, the end). No flag, no file, no change to the
# terminal output. seq is strictly increasing so a reconnecting reader can
# resume without gaps or duplicates.
EVENTS_FILE="${CLOUDLENS_EVENTS_FILE:-}"
EVENT_SEQ=0
json_str() { # minimal JSON string escaper (bash 3.2, no jq dependency)
  local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\n'/\\n}"; s="${s//$'\t'/\\t}"
  printf '"%s"' "$s"
}
emit_event() { # emit_event TYPE key=value ... (values are strings)
  [[ -n "$EVENTS_FILE" ]] || return 0
  local type="$1"; shift
  EVENT_SEQ=$((EVENT_SEQ + 1))
  local body="{\"seq\":${EVENT_SEQ},\"ts\":\"$(date -u +%FT%TZ)\",\"type\":$(json_str "$type")"
  local kv
  for kv in "$@"; do
    body+=",$(json_str "${kv%%=*}"):$(json_str "${kv#*=}")"
  done
  body+="}"
  printf '%s\n' "$body" >> "$EVENTS_FILE" 2>/dev/null || true
}
```

Parser: add `--events) EVENTS_FILE="$2"; shift 2 ;;` beside `--profile`.

Right after the parser finishes (before Phase 1's banner), emit the first event: `emit_event hello stack="$STACK_NAME" region="$REGION" dry_run="$DRY_RUN"` (STACK_NAME/REGION may still be empty there; emit a second `hello` after Phase 3 resolves them, and the console treats the last `hello` as authoritative).

In `state_phase()`: add `emit_event phase name="$1" status="$2" reason="${3:-}"` as its last line.

At the final banner ("Stack deployment complete"): `emit_event done status=ok report="${REPORT_FILE:-}" profile="${PROFILE_FILE:-}"`. In `on_exit`, when `SCRIPT_DONE != true`: `emit_event done status=failed phase="$CURRENT_PHASE_LABEL"` (use whatever variable `on_exit` already prints as the phase).

**Step 4: Run the test**

Run: `bash deploy/tests/test_events.sh && bash -n deploy/deploy-stack.sh`
Expected: `PASS events: N lines, M phase events`.

**Step 5: Commit**

```bash
git add deploy/deploy-stack.sh deploy/tests/test_events.sh
git commit -m "deploy: --events writes the structured side channel the console renders"
```

---

### Task 2: Resource, login, and check events

**Files:**
- Modify: `deploy/deploy-stack.sh` (`discover_stack_facts`, the logins block, `run_doctor`'s `_pass/_warn/_fail`)
- Test: extend `deploy/tests/test_events.sh`

**Step 1: Extend the test** (append before the final `print`):

```python
kinds = [ev for ev in map(json.loads, open(sys.argv[1])) if ev["type"] == "check"]
assert kinds, "doctor checks must be events too"   # --dry-run runs the doctor-lite checks
```

and add a second invocation in the shell part: `bash deploy/deploy-stack.sh --doctor --region us-east-1 --events "$S/doctor.jsonl" </dev/null >/dev/null 2>&1 || true` followed by a python assertion that `doctor.jsonl` contains `check` events with `status` in `pass|warn|fail` and a `fix` key on non-pass rows.

**Step 2: Run to verify it fails** (no `check` events yet).

**Step 3: Implement**

- In `run_doctor`'s three helpers: `_pass` -> `emit_event check item="$1" status=pass`; `_warn` -> `emit_event check item="$1" status=warn fix="$2"`; `_fail` -> `emit_event check item="$1" status=fail fix="$2"`.
- In `discover_stack_facts`, after the IPs are known:
  ```bash
  emit_event resource kind=vpc id="$STACK_VPC_ID"
  emit_event resource kind=subnet id="$MGMT_SUBNET_ID" role=mgmt zone="$STACK_ZONE"
  [[ -n "$INGRESS_SUBNET_ID" ]] && emit_event resource kind=subnet id="$INGRESS_SUBNET_ID" role=ingress
  [[ -n "$EGRESS_SUBNET_ID" ]]  && emit_event resource kind=subnet id="$EGRESS_SUBNET_ID" role=egress
  emit_event resource kind=vcontroller ip="$CLMS_PUBLIC_IP" private_ip="$CLMS_PRIVATE_IP"
  [[ "$DEPLOY_KVO" == "true" ]] && emit_event resource kind=kvo ip="$KVO_PUBLIC_IP" private_ip="$KVO_PRIVATE_IP"
  [[ "$DEPLOY_VPB" == "true" ]] && emit_event resource kind=vpb ip="$VPB_PUBLIC_IP" ingress_ip="${VPB_INGRESS_IP:-}" egress_ip="${VPB_EGRESS_IP:-}"
  ```
- In the logins block (the "Log in now and watch the rest happen" prints for vController, KVO, vPB): beside each print, `emit_event login component=vcontroller url="https://${CLMS_PUBLIC_IP}/cloudlens/login" user="$VC_ADMIN_USER" password_in="$VC_CREDS_FILE"` (KVO: `user=admin password_in="admin (default)"`; vPB: `url="ssh -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@${VPB_PUBLIC_IP}" password_in="${KEY_NAME}.pem"`). Never the password itself.
- Where the workloads are counted (`Matching running EC2s`): `emit_event resource kind=workloads count="$TAGGED_COUNT" tag="${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}"`.
- In the EKS phase after `deploy-eks-tapping.sh` succeeds: `emit_event resource kind=eks cluster="${EKS_CLUSTER:-${STACK_NAME}-eks}" mode="$EKS_MODE"`.

**Step 4: Run** `bash deploy/tests/test_events.sh` -> PASS. **Step 5: Commit** `deploy: resources, logins and doctor checks are events`.

---

### Task 3: Prompts answered from the UI

**Files:**
- Modify: `deploy/deploy-stack.sh` (`ask()`, parser)
- Test: `deploy/tests/test_prompt_pipe.sh` (new)

**Step 1: Write the failing test**

```bash
#!/usr/bin/env bash
# The console answers prompts through a FIFO; the script must emit the prompt
# and read the reply from the pipe, not from stdin.
set -u
cd "$(dirname "$0")/../.."
S=$(mktemp -d); mkfifo "$S/answers"
# a tiny harness that sources ask() with the pipe set
( sleep 1; echo "hello-from-ui" > "$S/answers" ) &
out=$(bash -c '
  source <(awk "/^ask\(\)/,/^}/" deploy/deploy-stack.sh)
  source <(awk "/^json_str\(\)/,/^}/; /^emit_event\(\)/,/^}/" deploy/deploy-stack.sh)
  INTERACTIVE=true PROMPT_PIPE="'"$S/answers"'" EVENTS_FILE="'"$S/ev.jsonl"'" EVENT_SEQ=0
  ask "Type something: " "default"')
[[ "$out" == "hello-from-ui" ]] && echo "PASS answer came from the pipe" || { echo "FAIL got '$out'"; exit 1; }
grep -q '"type":"prompt"' "$S/ev.jsonl" && echo "PASS prompt event emitted" || { echo "FAIL no prompt event"; exit 1; }
```

**Step 2: Run** -> FAIL (`PROMPT_PIPE` unknown to `ask`).

**Step 3: Implement** - replace `ask()`:

```bash
PROMPT_PIPE="${CLOUDLENS_PROMPT_PIPE:-}"
PROMPT_SEQ=0
ask() {
  local prompt="$1" def="${2:-}" ans=""
  if [[ -n "$PROMPT_PIPE" ]]; then
    # The console owns this FIFO. Emit the question, block on the reply. The
    # prompt id lets the page pair answer with question after a reconnect.
    PROMPT_SEQ=$((PROMPT_SEQ + 1))
    emit_event prompt id="p${PROMPT_SEQ}" question="$prompt" default="$def" kind=text
    IFS= read -r ans < "$PROMPT_PIPE" || ans=""
    printf '%s' "${ans:-$def}"
    return 0
  fi
  if [[ "$INTERACTIVE" == "true" ]]; then
    read -rp "$prompt" ans || true
  fi
  printf '%s' "${ans:-$def}"
}
```

Secrets: the two `read -rsp` calls (KVO secret key, and any password) must go through a new `ask_secret()` with `kind=secret`; replace them. Parser: `--prompt-pipe) PROMPT_PIPE="$2"; shift 2 ;;`. When `PROMPT_PIPE` is set, force `INTERACTIVE=true` after the tty detection so the interview runs (the UI is the terminal).

**Step 4: Run** both test scripts + `bash -n`. **Step 5: Commit** `deploy: prompts can be answered over a pipe the console owns`.

---

### Task 4: Shared profile allowlist

The bash `profile_key_allowed()` case and the Python profile writer must agree by construction.

**Files:**
- Create: `deploy/profile-keys.txt` (one key per line)
- Modify: `deploy/deploy-stack.sh` (`profile_key_allowed` reads the file, falling back to the built-in case when the file is absent, e.g. a bare curl|bash before the clone)
- Create: `console/cloudlens_console/profile.py`
- Test: `console/tests/test_profile.py`

**Step 1: Failing test**

```python
# console/tests/test_profile.py
import os, subprocess, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cloudlens_console import profile as P

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

def test_keys_match_the_script():
    keys = P.allowed_keys()
    assert "CLOUDLENS_REGION" in keys and "CLOUDLENS_ADMIN_USER" not in keys
    # every key the script's case statement accepts is in the file, and vice versa
    case = open(os.path.join(REPO, "deploy", "deploy-stack.sh")).read()
    for k in keys:
        assert k in case, k

def test_render_only_allowlisted_and_round_trips():
    plan = {"CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_STACK_NAME": "demo",
            "CLOUDLENS_ADMIN_USER": "-oProxyCommand=x", "CLOUDLENS_TAPPING": "both"}
    text = P.render(plan)
    assert "CLOUDLENS_ADMIN_USER" not in text and "CLOUDLENS_TAPPING=both" in text
    # the script's loader accepts every line we wrote (dry-run, no AWS)
    p = os.path.join("/tmp", "test-profile.env"); open(p, "w").write(text)
    out = subprocess.run(["bash", "deploy/deploy-stack.sh", "--dry-run", "--profile", p, "--key-name", "k"],
                         cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert "setting(s) applied" in out.stdout and "ignored keys" not in out.stdout + out.stderr
```

**Step 2: Run** `cd console && python3 -m pytest tests/test_profile.py -q` -> FAIL (no module).

**Step 3: Implement**

`deploy/profile-keys.txt`: the 44 keys from `profile_key_allowed`, one per line, `#` comments allowed.

`profile.py`:
```python
"""The deploy profile: the ONE contract between the wizard and deploy-stack.sh.
The key list lives in deploy/profile-keys.txt and is read by both sides."""
import os, re
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KEYS_FILE = os.path.join(REPO, "deploy", "profile-keys.txt")
_VALUE_OK = re.compile(r"^[^\n\r]*$")

def allowed_keys():
    with open(KEYS_FILE) as fh:
        return [l.strip() for l in fh if l.strip() and not l.startswith("#")]

def render(plan):
    """plan: {KEY: value}. Unknown keys are dropped (never an error: the UI
    builds plans from forms, and the script enforces the same list)."""
    keys = set(allowed_keys())
    lines = ["# deploy profile written by the CloudLens console"]
    for k in allowed_keys():
        if k in plan and plan[k] is not None and _VALUE_OK.match(str(plan[k])):
            v = str(plan[k]).replace('"', '\\"')
            lines.append('%s="%s"' % (k, v))
    return "\n".join(lines) + "\n"
```

In bash, `profile_key_allowed()`: if `deploy/profile-keys.txt` exists next to the script (`$SCRIPT_DIR/profile-keys.txt`), `grep -qx "$1"` it; else the built-in case (kept verbatim as the fallback for the bare curl path).

**Step 4: Run** -> PASS. **Step 5: Commit** `profile: one key list read by the script and the console`.

---

### Task 5: Events v2 in the console

**Files:**
- Modify: `console/cloudlens_console/events.py`
- Test: `console/tests/test_console.py` (extend)

**Step 1: Failing test**

```python
def test_script_events_pass_through_with_console_ids():
    raw = {"seq": 7, "ts": "2026-09-08T10:00:00Z", "type": "phase", "name": "stack", "status": "done"}
    ev = E.from_script(raw)
    assert ev["type"] == "phase" and ev["name"] == "stack" and ev["script_seq"] == 7
    assert isinstance(ev["id"], int)   # the console's own monotonic id for SSE resume
```

**Step 2: Run** -> FAIL. **Step 3: Implement** in `events.py`:

```python
PHASE, RESOURCE, CHECK, PROMPT, LOGIN = "phase", "resource", "check", "prompt", "login"
SCRIPT_TYPES = {"hello", PHASE, RESOURCE, CHECK, PROMPT, LOGIN, DONE}

def from_script(raw):
    """One JSON line from deploy-stack.sh --events, re-stamped with the
    console's own id so SSE resume works across both sources."""
    data = dict(raw); data["script_seq"] = data.pop("seq", None)
    typ = data.pop("type")
    if typ not in SCRIPT_TYPES:
        typ = LOG; data = {"text": json.dumps(raw)}
    return _mk(typ, **data)
```

**Step 4/5:** tests pass; commit `console: script events join the stream with resumable ids`.

---

### Task 6: Orchestrator runs the real engine

**Files:**
- Modify: `console/cloudlens_console/orchestrator.py`
- Test: `console/tests/test_orchestrator_engine.py` (new; uses a fake script)

**Step 1: Failing test** - a fake `deploy-stack.sh` at a temp path that writes two events and reads one answer:

```python
FAKE = r'''#!/usr/bin/env bash
ev="$1"; pipe="$2"
echo '{"seq":1,"ts":"t","type":"hello","stack":"x","region":"us-east-1"}' >> "$ev"
echo '{"seq":2,"ts":"t","type":"prompt","id":"p1","question":"Code?","kind":"secret"}' >> "$ev"
IFS= read -r ans < "$pipe"
echo "{\"seq\":3,\"ts\":\"t\",\"type\":\"done\",\"status\":\"ok\",\"answer\":\"$ans\"}" >> "$ev"
'''
def test_engine_streams_events_and_answers_prompts(tmp_path):
    script = tmp_path / "fake.sh"; script.write_text(FAKE); script.chmod(0o755)
    job = O.Job("j1", "deploy", {})
    got = []
    job.emit = got.append
    t = threading.Thread(target=O.run_engine, args=(job, [str(script)]), daemon=True); t.start()
    # wait for the prompt, answer it
    for _ in range(50):
        if any(e["type"] == "prompt" for e in got): break
        time.sleep(0.1)
    job.answer("p1", "ABCD-1234")
    t.join(5)
    types = [e["type"] for e in got]
    assert types[:2] == ["hello", "prompt"] and types[-1] == "done"
    assert got[-1]["answer"] == "ABCD-1234"
```

**Step 2: Run** -> FAIL (`run_engine`, `answer` missing).

**Step 3: Implement** in `orchestrator.py`:

```python
def run_engine(job, cmd, cwd=None):
    """Run a deploy-stack.sh style command with --events/--prompt-pipe wired to
    this job, tailing the events file into the stream while the process runs.
    `cmd` is the argv WITHOUT the two flags; they are appended here so every
    caller gets them right."""
    work = tempfile.mkdtemp(prefix="cl-job-")
    job.events_path = os.path.join(work, "events.jsonl")
    job.pipe_path = os.path.join(work, "answers")
    os.mkfifo(job.pipe_path)
    open(job.events_path, "a").close()
    argv = list(cmd) + ["--events", job.events_path, "--prompt-pipe", job.pipe_path]
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, stdin=subprocess.DEVNULL)
    job.proc = proc
    stop = threading.Event()
    def tail():
        pos = 0
        while not stop.is_set() or True:
            with open(job.events_path) as fh:
                fh.seek(pos); chunk = fh.read(); pos = fh.tell()
            for line in chunk.splitlines():
                if line.strip():
                    try: job.emit(E.from_script(json.loads(line)))
                    except ValueError: job.emit(E.log(line))
            if stop.is_set() and not chunk: break
            time.sleep(0.25)
    tt = threading.Thread(target=tail, daemon=True); tt.start()
    for line in proc.stdout:
        job.emit(E.log(line.rstrip("\n")))
    proc.wait(); stop.set(); tt.join(3)
    if not any(e.get("type") == E.DONE for e in job.buffer):
        job.emit(E.done("exited %d" % proc.returncode) if proc.returncode == 0
                 else E.error("engine exited %d" % proc.returncode))

def _answer(self, prompt_id, text):
    """Write one reply to the FIFO the engine is blocked on."""
    with open(self.pipe_path, "w") as fh:
        fh.write(text + "\n")
Job.answer = _answer
```

`Job` needs `self.buffer` (list of emitted events, for SSE resume) - add in `__init__` and append in `emit`. Fake script takes `$1`/`$2` positionally in the test, so in the test call `O.run_engine(job, [str(script)])` and have the FAKE read `$ev`/`$pipe` from `--events X --prompt-pipe Y`: change FAKE to parse `while [[ $# -gt 0 ]]; do case $1 in --events) ev=$2; shift 2;; --prompt-pipe) pipe=$2; shift 2;; *) shift;; esac; done`.

**Step 4/5:** PASS; commit `console: the orchestrator runs the real engine and relays its events`.

---

### Task 7: The API

**Files:**
- Modify: `console/cloudlens_console/server.py`
- Create: `console/cloudlens_console/api.py` (handlers, pure functions over subprocess/boto3-free `aws` CLI JSON)
- Test: `console/tests/test_api.py`

Routes (all JSON, all loopback in deliverable 1):

| Route | Does |
|---|---|
| `GET /api/doctor?region=R` | runs `deploy-stack.sh --doctor --region R --events F`, returns `{checks:[{item,status,fix}], ok}` |
| `GET /api/discover/vpcs?region=R` | `aws ec2 describe-vpcs` -> `[{id,cidr,name}]` |
| `GET /api/discover/subnets?region=R&vpc=V` | `[{id,az,cidr,public}]` |
| `GET /api/discover/workloads?region=R&tag=K=V&vpcs=a,b` | count + up to 50 rows via `describe-instances` |
| `GET /api/discover/eks?region=R` | `aws eks list-clusters` |
| `POST /api/plan` | body `{plan}` -> `{profile_text, resolved:[...]}`; validation errors as `{errors:[...]}` |
| `POST /api/run` | body `{plan, secrets:{...}}` -> writes `deploy-profile-<stack>.env`, starts `run_engine(["bash","deploy/deploy-stack.sh","--profile",f])` with secrets in `env`, returns `{job_id}` |
| `POST /api/answer/<job>` | `{prompt_id, text}` -> `job.answer` |
| `GET /events/<job>` | SSE; honours `Last-Event-ID` by replaying `job.buffer` past that id |
| `POST /api/teardown` | `{stack, region, orphans_only, confirm_name}`; refuses unless `confirm_name == stack`; runs `teardown-stack.sh` via `run_engine` (`--yes --accept-licence-loss` only when licences were released, see Task 10) |
| `GET/POST /api/licences` | list (`GET /api/v2/licensing/licenses` via `kvo_license._req`), check codes, activate, release (`operations/deactivate`) |

**Step 1: Failing tests** (one per route family; stub `subprocess.run` and the engine):

```python
def test_plan_rejects_bad_stack_name_and_renders_profile(monkeypatch):
    from cloudlens_console import api
    r = api.plan({"CLOUDLENS_STACK_NAME": "bad name!", "CLOUDLENS_REGION": "us-east-1"})
    assert r["errors"]
    r = api.plan({"CLOUDLENS_STACK_NAME": "demo", "CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_TAPPING": "sensors"})
    assert not r.get("errors") and 'CLOUDLENS_TAPPING="sensors"' in r["profile_text"]

def test_teardown_requires_typed_name():
    from cloudlens_console import api
    r = api.teardown({"stack": "demo", "region": "us-east-1", "confirm_name": "nope"}, start=lambda *a, **k: "j")
    assert r["error"].startswith("type the stack name")

def test_sse_resumes_from_last_event_id():
    job = O.Job("j", "deploy", {}); [job.emit(E.log("l%d" % i)) for i in range(5)]
    ids = [e["id"] for e in job.buffer]
    later = server.events_after(job, last_id=ids[2])
    assert [e["id"] for e in later] == ids[3:]
```

**Step 2: Run** -> FAIL. **Step 3: Implement** `api.py` with `plan()`, `discover_*()`, `doctor()`, `run()`, `teardown()`, `licences_*()`; wire into `server.py` `do_GET/do_POST` by prefix; add `events_after(job, last_id)` and read `Last-Event-ID` in `_sse` (replay buffered events with `id > last_id`, then stream live). Stack-name validation: reuse the script's rule `^[A-Za-z][A-Za-z0-9-]*$`.

**Step 4/5:** PASS; commit `console: the API, every route a face on an existing command`.

---

### Task 8: The wizard (six screens) and the plan page

**Files:**
- Modify: `console/cloudlens_console/web/index.html`, `web/app.js`
- Create: `web/wizard.js` (screens, state, discovery calls), `web/plan.js`
- Test: `console/tests/test_web_static.py` (the pages load; ids exist) + Task 11's browser tests

Screens, in this order, each a `<section data-screen="N">` with Back/Next, state held in one `plan` object keyed by the profile keys:

1. **Where** - radio new/existing; on existing: `GET /api/discover/vpcs` table (click a row), then subnets table; sets `CLOUDLENS_INFRA`, `CLOUDLENS_EXISTING_VPC_ID`, `CLOUDLENS_EXISTING_SUBNET_ID`.
2. **Components** - KVO/vPB switches, vController capacity radio (t3.xlarge recommended / m5.xlarge) with the fixed sizes for KVO/vPB stated; `CLOUDLENS_DEPLOY_KVO/VPB`, `CLOUDLENS_VCONTROLLER_TYPE`.
3. **Tapping** - sensors/mirror/both/none cards; management plane (standalone/KVO-managed) shown only when KVO and sensors; `CLOUDLENS_TAPPING`, `CLOUDLENS_SENSOR_MODE`.
4. **Workloads** - existing (tag key/value inputs; `GET /api/discover/workloads` debounced, live count + rows; VPC chips) / test (per-OS steppers 0-10) / later; collector placement (three subnet pickers) when tapping includes mirror and infra is existing; `CLOUDLENS_WORKLOAD_CHOICE`, `CLOUDLENS_DISCOVERY_TAG_*`, `CLOUDLENS_TEST_VMS`, `CLOUDLENS_SOURCE_VPCS`, `CLOUDLENS_COLLECTOR_*`.
5. **Kubernetes** - none / existing (cluster list) / sample; DaemonSet or sidecar; `CLOUDLENS_DEPLOY_EKS`, `CLOUDLENS_EKS_CLUSTER`, `CLOUDLENS_EKS_SAMPLE`, `CLOUDLENS_EKS_MODE`.
6. **Plan** - `POST /api/plan` -> the resolved table (same rows as the CLI's Resolved configuration), the profile text (collapsible), the equivalent CLI line, secrets fields (KVO access/secret key when mirror; activation codes when KVO) and **Launch**.

Keep the existing visual language (the diagram, pills, narration styles). No framework.

**Step 1: Failing static test**: `index.html` contains `data-screen="1"` through `"6"` and `id="launchBtn"`. **Step 2: Run** -> FAIL. **Step 3: Implement** the sections and `wizard.js`. **Step 4/5:** PASS; commit `console: the six-screen wizard writes the profile the CLI replays`.

---

### Task 9: The Watch screen

**Files:**
- Modify: `web/app.js` (event dispatch), `web/index.html`
- Create: `web/watch.js`
- Test: `console/tests/test_watch_model.py` (a pure JS model is hard to unit test without node; instead test the Python side that shapes events, and cover the DOM in Task 11)

Rendering rules (all from events, nothing else):
- `phase` -> timeline row: start = spinner, done = check, failed = red with `reason`, skipped = dim with `reason`. Phase order from the script's `PHASE_ORDER`.
- `resource` -> topology node appears/updates: `vpc`, `subnet` (three slots under the VPC), `vcontroller`, `kvo`, `vpb` (with three NIC dots when `ingress_ip`/`egress_ip` present), `workloads` (a stack of N cards), `eks`.
- `login` -> a "Logins" card: url, user, where the password is (never the password).
- `prompt` -> modal; submit -> `POST /api/answer/<job>`; `kind=secret` renders a password field.
- `check` -> Pre-flight panel rows.
- `log` -> raw drawer, collapsed by default, count badge.
- `done` -> final banner with the report link.
- Reconnect: `EventSource` sends `Last-Event-ID` itself; on `onerror` show "reconnecting"; never reset the model.

**Steps:** static test for the new ids; implement `watch.js` (`applyEvent(model, ev)` as a pure function, then `render(model)`); commit `console: the Watch screen renders only what the engine says`.

---

### Task 10: Operate, Licensing, Teardown screens

**Files:** `web/operate.js`, `web/licences.js`, `web/teardown.js`, `index.html`; `api.py` additions; tests in `test_api.py`.

- **Operate**: `GET /api/status?stack=&region=` -> instances by Name tag, sensors registered (vController API via the creds file when present; else "log in to see"), mirror sessions count, vPB counters (SSH, only if the .pem is found; else the command shown). Buttons: Re-run phase (select from `PHASE_ORDER`) -> `POST /api/run` with `{only: phase}`; Resume -> `POST /api/run` with the stack name and no plan (the CLI's resume).
- **Licensing**: paste box -> `POST /api/licences/check` (`kvo_license.lookup_code` per code) -> table -> quantities -> `POST /api/licences/activate`; `POST /api/licences/release` per row (`operations/deactivate`, polled).
- **Teardown**: `POST /api/teardown {orphans_only:true}` first, render the "left loose" report; then the typed-name gate; `POST /api/teardown` runs `teardown-stack.sh`; final verify card from `GET /api/verify-empty?region=` (instances, VPCs, volumes, stacks counts).

Each gets a failing API test first (stubbed subprocess), then implementation, then commit: `console: Operate, Licensing and Teardown, each a face on a proven command`.

---

### Task 11: Browser smoke tests

**Files:**
- Create: `console/tests/browser/test_smoke.py` (Playwright for Python; `pip install playwright && playwright install chromium`)
- Modify: `console/README.md` (how to run)

Tests (start the server on a free port with `--replay`/stubbed API where needed):
1. wizard reaches the plan page with a greenfield plan and the profile text contains `CLOUDLENS_INFRA="new"`
2. an existing-VPC plan requires a subnet before Next is enabled
3. workloads screen shows the live count from a stubbed `/api/discover/workloads`
4. Launch posts a plan and the Watch screen shows the timeline from a fixture events file
5. a `prompt` event opens the modal and posting an answer clears it
6. a `phase failed` event turns the row red and shows the fix
7. teardown refuses without the typed name
8. licences table renders a failed lookup as a row, not an error page

Commit: `console: browser smoke tests for the eight things a person can do`.

---

### Task 12: Docs and entry points

**Files:** `docs/index.html` (a "Console" card: `python3 -m cloudlens_console`), `console/README.md` (rewrite: screens, the profile contract, how it stays honest), `README.md` hero line.

Commit: `docs: the console is the front door; the CLI is its engine`.

---

## Deliverable 2: The appliance

### Task 13: Console serves HTTPS with a password

**Files:**
- Modify: `console/cloudlens_console/__main__.py` (flags `--bind`, `--port`, `--tls-cert`, `--tls-key`, `--password-file`), `server.py` (session cookie)
- Test: `console/tests/test_auth.py`

**Step 1: Failing test**: with `password_file` set, `GET /` without a session cookie returns 401 and the login page; `POST /login` with the right password sets `cl_session` (HttpOnly, Secure when TLS) and `GET /` then returns 200; a wrong password returns 401 and is rate-limited (5/min).

**Step 3: Implement**: `ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)` wrapping the listening socket when cert+key given; sessions as `secrets.token_urlsafe(32)` in an in-memory dict with a 12h expiry; every `/api/*` and `/events/*` requires the cookie when a password is configured; loopback runs without a password stay exactly as today.

Commit: `console: optional HTTPS and a password, for the appliance`.

---

### Task 14: The appliance template

**Files:**
- Create: `deploy/cloudformation/cloudlens-autopilot.yaml`
- Test: `cfn-lint deploy/cloudformation/cloudlens-autopilot.yaml` and `aws cloudformation validate-template`

Parameters: `AdminCidr` (required), `InstanceType` (default t3.small), `RepoTag` (default `main`), `KeyPairName` (optional, for SSH into the appliance), `VpcId`/`SubnetId` (optional: default VPC when blank).

Resources:
- `AutopilotRole` + inline policy: the actions in `deploy/iam/cloudlens-zonetap-policy.json` PLUS `cloudformation:*` on stacks named `*`, `ec2:*` describe/create for what `stack.yaml` builds, `iam:PassRole` for the roles the stack creates, `ssm:PutParameter/GetParameter` on `/cloudlens/autopilot/${AWS::StackName}/*`, `eks:*` and `ecr:*` for the EKS track. (Start broad with an explicit list; tighten with the live run's CloudTrail.)
- `AutopilotSG`: 443 from `AdminCidr`; 22 from `AdminCidr` only when `KeyPairName` is set.
- `AutopilotInstance` (Amazon Linux 2023 via the SSM public parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`), `IamInstanceProfile`, user-data:

```bash
#!/bin/bash
set -euxo pipefail
dnf install -y python3 python3-pip git openssl
pip3 install pyyaml requests
git clone --depth 1 --branch "${RepoTag}" https://github.com/Keysight-Tech/cloudlens-ansible-aws.git /opt/cloudlens
mkdir -p /etc/cloudlens
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=cloudlens-autopilot" \
  -keyout /etc/cloudlens/key.pem -out /etc/cloudlens/cert.pem
PW=$(openssl rand -base64 18)
aws ssm put-parameter --name "/cloudlens/autopilot/${AWS::StackName}/password" --type SecureString --value "$PW" --overwrite --region "${AWS::Region}"
printf '%s' "$PW" > /etc/cloudlens/password; chmod 600 /etc/cloudlens/password
cat > /etc/systemd/system/cloudlens-console.service <<EOF
[Unit]
Description=CloudLens Autopilot Console
After=network-online.target
[Service]
WorkingDirectory=/opt/cloudlens/console
ExecStart=/usr/bin/python3 -m cloudlens_console --bind 0.0.0.0 --port 443 --tls-cert /etc/cloudlens/cert.pem --tls-key /etc/cloudlens/key.pem --password-file /etc/cloudlens/password
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now cloudlens-console
```

Outputs: `ConsoleUrl` (`https://<public-ip>/`), `PasswordCommand` (`aws ssm get-parameter --with-decryption --name /cloudlens/autopilot/<stack>/password --query Parameter.Value --output text --region <r>`), `Note` ("self-signed certificate: accept the browser warning once").

**Steps:** write, `cfn-lint`, `validate-template`, add to `deploy/scripts/sync-cfn-templates-to-s3.sh`'s list and to `check-launch-buttons.sh` (it globs the dir, so nothing to add), add the fourth Launch button to `docs/index.html`. Commit: `appliance: the console in the customer's account, one Launch button`.

---

### Task 15: Appliance teardown coverage

**Files:** `deploy/teardown-stack.sh`

The appliance is a normal stack, so `--stack-name <appliance>` already deletes it. Add: delete the SSM parameter `/cloudlens/autopilot/<stack>/password` in the sweep when the stack's template carried `AutopilotInstance` (read from the manifest), so no secret outlives the appliance. Test: `bash -n` + the existing teardown dry path.

Commit: `teardown: the appliance's password does not outlive it`.

---

### Task 16: Live proof

Not code. Run, in the lab account, from the console on a laptop:
1. Pre-flight -> greenfield full stack -> Watch to completion -> Operate shows sensors and the vPB counters -> Licensing release -> Teardown -> verify-empty.
2. Launch the appliance with the button; open the URL; repeat step 1's deploy from inside the account (brownfield into a fresh stack's VPC is the stretch test).
Record what broke in `docs/plans/2026-09-08-operations-console-design.md` under a "Live findings" heading, fix, and re-run the failing step.

---

## Task order and checkpoints

0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10 -> 11 -> 12 -> **checkpoint: console v2 usable by SEs** -> 13 -> 14 -> 15 -> 16.

Tasks 1-4 are independent of 5-7 and can be done by two people in parallel; 8-10 depend on 7; 13-15 depend on nothing in 8-12 except the server existing.
