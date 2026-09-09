#!/usr/bin/env bash
# deploy/tests/test_events.sh: the --events sink writes valid, ordered JSON
# lines, exactly one done per run, and never a byte JSON forbids; a partial
# last line is finished before the next hello; the run names its whole phase
# list up front; the dry run describes the stack's resources; --doctor turns
# every check into an event.
#
# Runs under /bin/bash explicitly (3.2 on macOS, the floor the script targets)
# and again under the bash first on PATH when that is a different binary.
set -u
cd "$(dirname "$0")/../.."
REPO=$(pwd -P)
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT
# Every run happens inside the temp dir with HOME pointed there. The script
# writes its state file into the cwd, reads ~/.cloudlens-vcontroller-creds.json
# and embeds $HOME paths in password_in, so a run from the repo root left a
# .cloudlens-deploy-evt-*.state behind, took the creds-file login branch on a
# machine that has that file, and would fail the factory-password check on a
# home directory containing "admin".
cd "$S"

shells=(/bin/bash)
other=$(command -v bash 2>/dev/null || true)
if [[ -n "$other" && -x "$other" && ! "$other" -ef /bin/bash ]]; then shells+=("$other"); fi

# No run in this file may reach STS or a real account: the profile, static
# keys, the container and web-identity credential sources CloudShell, ECS and
# CodeBuild inject, the config and credential files, and IMDS are all cleared.
nocreds=(-u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN
         -u AWS_CONTAINER_CREDENTIALS_FULL_URI -u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
         -u AWS_CONTAINER_AUTHORIZATION_TOKEN -u AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
         -u AWS_WEB_IDENTITY_TOKEN_FILE -u AWS_ROLE_ARN
         AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true)

rc=0
for B in "${shells[@]}"; do
  echo "== $B ($("$B" -c 'echo "$BASH_VERSION"'))"
  # A fresh slate per shell, the state file included, so its assertion below
  # is about this shell's run and not the previous one's leftover.
  rm -f "$S"/*.jsonl "$S"/.cloudlens-deploy-*.state

  # 1. A full dry run. --dry-run touches no AWS resources, and with no
  #    credentials at all it cannot even sign in (it used to spend seconds on
  #    a real STS call from the machine's profile); the events file must
  #    still describe the whole run and the exit code stay 0. It runs twice:
  #    once with nothing optional (no kvo or vpb rows may appear) and once
  #    with KVO and vPB, the only way every login event is emitted.
  #    (env wants every -u before the first NAME=VALUE, hence the order.)
  dry=(env -u CLOUDLENS_VC_PASSWORD -u CLOUDLENS_KVO_ADMIN_PASS -u CLOUDLENS_KEY_PEM -u CLOUDLENS_VC_CREDS_FILE \
       "${nocreds[@]}" HOME="$S" "$B" "$REPO/deploy/deploy-stack.sh")
  "${dry[@]}" --dry-run --region us-east-1 --key-name k --stack-name evt \
    --tapping none --no-kvo --no-vpb --events "$S/events.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 0 ]]; then echo "FAIL dry-run: exit $code, expected 0"; rc=1; fi
  "${dry[@]}" --dry-run --region us-east-1 --key-name k --stack-name evt \
    --tapping none --with-kvo --with-vpb --events "$S/events-full.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 0 ]]; then echo "FAIL dry-run (with-kvo, with-vpb): exit $code, expected 0"; rc=1; fi
  # The state file lands in the cwd, which must be the temp dir, not the repo.
  if [[ ! -f "$S/.cloudlens-deploy-evt-us-east-1.state" ]]; then echo "FAIL dry-run: state file not in the temp dir"; rc=1; fi
  python3 - "$S/events.jsonl" "$S/events-full.jsonl" "$REPO/deploy/deploy-stack.sh" <<'PY' || rc=1
import json, re, sys
runs = {name: [json.loads(line) for line in open(path)]   # every line is one JSON object
        for name, path in (("minimal", sys.argv[1]), ("full", sys.argv[2]))}
order = re.search(r'^PHASE_ORDER="([^"]+)"', open(sys.argv[3]).read(), re.M).group(1).split()
for name, evs in runs.items():
    seqs = [e["seq"] for e in evs]
    types = [e["type"] for e in evs]
    for e in evs:
        assert "ts" in e and "type" in e, e
    assert seqs == list(range(1, len(seqs) + 1)), "%s: seq must be 1..N with no gap or repeat: %r" % (name, seqs)
    assert types[0] == "hello", (name, types[:3])
    # The phase list, once, second: a console cannot draw the phases still to
    # come from the phase events, which only ever report one that has ended.
    # These runs pass no --from/--only, so the list is the whole PHASE_ORDER;
    # 1b below is the scoped runs, where it is the selected phases instead.
    assert types[1] == "phases", (name, types[:3])
    assert types.count("phases") == 1, (name, types)
    assert evs[1]["order"].split() == order, (name, evs[1], order)
    assert "phase" in types, (name, types)
    for e in evs:
        if e["type"] == "phase":
            assert e["name"] in order, "a phase nothing announced: %r" % e
    assert types.count("done") == 1, "%s: exactly one done per run: %r" % (name, types)
    assert types[-1] == "done", (name, types)
    hello = [e for e in evs if e["type"] == "hello"][-1]
    assert hello["stack"] == "evt" and hello["region"] == "us-east-1", hello
    assert evs[-1]["status"] == "dry-run", evs[-1]
    # No event may ever carry a password value: logins say where it lives.
    assert not any("password" in e for e in evs), [e for e in evs if "password" in e]
evs = runs["minimal"]
types = [e["type"] for e in evs]
# The console draws the stack from resource events, so the dry run emits its
# placeholders too (--no-kvo --no-vpb: no kvo and no vpb rows).
res = [e for e in evs if e["type"] == "resource"]
kinds = [(r["kind"], r.get("role")) for r in res]
assert ("vpc", None) in kinds and ("subnet", "mgmt") in kinds and ("vcontroller", None) in kinds, kinds
assert not any(r["kind"] in ("kvo", "vpb") for r in res), kinds
assert all(r["id"] for r in res if r["kind"] in ("vpc", "subnet")), res
full_kinds = [r["kind"] for r in runs["full"] if r["type"] == "resource"]
assert "kvo" in full_kinds and "vpb" in full_kinds, full_kinds
# Every component announces a login, and password_in names WHERE the password
# is (a file, an environment variable, a key pair, a factory default) and
# never contains its value. The factory passwords are the values a careless
# string would leak, so none may appear as a substring (case-sensitive).
logins = [e for e in runs["minimal"] + runs["full"] if e["type"] == "login"]
assert {l["component"] for l in logins} == {"vcontroller", "kvo", "vpb"}, logins
factory = {"admin", "ixia", "Cl0udLens@dm!n"}
for l in logins:
    assert l.get("user") and l.get("url") and l.get("password_in"), l
    assert not any(pw in l["password_in"] for pw in factory), "password_in leaks a factory password: %r" % l
# HOME is the temp dir and the creds variables are unset, so phase 9 has never
# run here: the vController login takes the factory-default branch on every
# machine, not the creds-file branch on the one box that happens to have the file.
vc = [l for l in logins if l["component"] == "vcontroller"]
assert vc and all("factory default" in l["password_in"] for l in vc), vc
print("PASS dry-run: %d lines, %d phases announced, %d phase events, %d resources, one done; %d logins name no password, vcontroller is factory-default"
      % (len(evs), len(order), types.count("phase"), len(res), len(logins)))
PY

  # 1b. A phase-scoped run announces the phases it can reach and no others.
  #     The phase list is emitted after the --from/--only resolution, so a
  #     scoped run does not promise a console a timeline of phases it will
  #     never run, and a selector naming a phase that does not exist ends the
  #     run before anything is announced at all.
  "${dry[@]}" --dry-run --region us-east-1 --key-name k --stack-name evtonly \
    --tapping none --no-kvo --no-vpb --only stack --events "$S/only.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 0 ]]; then echo "FAIL --only dry run: exit $code, expected 0"; rc=1; fi
  "${dry[@]}" --dry-run --region us-east-1 --key-name k --stack-name evtfrom \
    --tapping none --no-kvo --no-vpb --from vpb --events "$S/from.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 0 ]]; then echo "FAIL --from dry run: exit $code, expected 0"; rc=1; fi
  "${dry[@]}" --dry-run --region us-east-1 --only nosuchphase --events "$S/badphase.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 1 ]]; then echo "FAIL --only nosuchphase: exit $code, expected 1"; rc=1; fi
  python3 - "$S/only.jsonl" "$S/from.jsonl" "$S/badphase.jsonl" "$REPO/deploy/deploy-stack.sh" <<'PY' || rc=1
import json, re, sys
order = re.search(r'^PHASE_ORDER="([^"]+)"', open(sys.argv[4]).read(), re.M).group(1).split()
def announced(path):
    evs = [json.loads(line) for line in open(path)]
    types = [e["type"] for e in evs]
    assert types[0] == "hello", types[:3]
    return evs, types, [e for e in evs if e["type"] == "phases"]
evs, types, phases = announced(sys.argv[1])
assert len(phases) == 1, "the list is said exactly once: %r" % types
assert phases[0]["order"].split() == ["stack"], phases[0]
evs, types, phases = announced(sys.argv[2])
assert len(phases) == 1, types
assert phases[0]["order"].split() == order[order.index("vpb"):], (phases[0], order)
# an unknown phase: the run ends on it, and nothing was announced
evs, types, phases = announced(sys.argv[3])
assert not phases, "a run that cannot start announces no phases: %r" % types
assert types == ["hello", "done"], types
assert evs[-1]["status"] == "failed" and "nosuchphase" in evs[-1]["reason"], evs[-1]
# A phase the selector took out of the run did not run, so it is never
# recorded done. `state_phase wait done` and `state_phase key done` sat AFTER
# the closing fi of their own `if ! run_phase ...` blocks, so every --only and
# --from run reported both as finished: a green check on the Watch timeline
# for work nothing did, and, on a real run, a state file and an HTML report
# claiming the vController had been waited for and a project key minted. The
# key one is the expensive lie - sensors have nothing to register with.
def reported(path):
    return [e for e in (json.loads(line) for line in open(path)) if e["type"] == "phase"]
# what each selector actually left in the run: --only stack is the one phase,
# --from vpb is vpb onwards. Everything else was taken out of it.
for path, ran in ((sys.argv[1], {"stack"}), (sys.argv[2], set(order[order.index("vpb"):]))):
    for e in reported(path):
        if e["name"] in ran:
            continue
        assert e["status"] != "done", "a phase the selector removed, reported done: %r" % e
        assert e["status"] == "skipped", "a phase that did not run is skipped, not %r" % e
        assert e.get("reason"), "a skipped phase says why: %r" % e
only_said = {e["name"]: e["status"] for e in reported(sys.argv[1])}
for name in ("wait", "key"):
    assert only_said.get(name, "skipped") == "skipped", (name, only_said)
print("PASS phase selectors: --only announces 1 phase, --from announces %d, an unknown phase announces none;"
      " every phase a selector removed is skipped with a reason, never done"
      % len(order[order.index("vpb"):]))
PY

  # 2. A parse-time fail() after the sink exists, with every byte JSON hates in
  #    the reason: a quote, a backslash, a newline and a raw ESC.
  "$B" "$REPO/deploy/deploy-stack.sh" --iac $'x"y\\z\nw\x1b' --events "$S/fail.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 1 ]]; then echo "FAIL parse-time fail: exit $code, expected 1"; rc=1; fi
  python3 - "$S/fail.jsonl" <<'PY' || rc=1
import json, sys
lines = open(sys.argv[1]).read().splitlines()
assert len(lines) == 3, "expected hello + phases + done only: %r" % lines
hello, phases, done = (json.loads(line) for line in lines)
assert hello["type"] == "hello" and done["type"] == "done", (hello, done)
assert phases["type"] == "phases" and phases["order"].split(), phases
assert done["status"] == "failed", done
r = done["reason"]
assert 'x"y\\z' in r and "\n" in r and "\x1b" in r, repr(r)
print("PASS parse-time fail: hello + phases + one done, reason round-trips %r" % r)
PY

  # 2b. The newline guard. A run killed mid-write leaves a partial last line;
  #     the next run finishes it before its hello, so line 1 stays malformed
  #     (by design: nothing can repair it, readers skip it) and line 2 is a
  #     whole hello with seq 2, not hello glued onto the fragment.
  printf '%s' '{"seq":1,"type":"pha' > "$S/partial.jsonl"
  "$B" "$REPO/deploy/deploy-stack.sh" --iac bogus --events "$S/partial.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 1 ]]; then echo "FAIL newline guard: exit $code, expected 1"; rc=1; fi
  python3 - "$S/partial.jsonl" <<'PY' || rc=1
import json, sys
lines = open(sys.argv[1]).read().split("\n")
assert len(lines) == 5 and lines[-1] == "", "partial + hello + phases + done, each newline-terminated: %r" % lines
def parses(s):
    try: json.loads(s); return True
    except ValueError: return False
assert not parses(lines[0]) and lines[0] == '{"seq":1,"type":"pha', "line 1 must stay the untouched fragment: %r" % lines[0]
hello, phases, done = (json.loads(l) for l in lines[1:4])
assert hello["type"] == "hello" and hello["seq"] == 2, hello
assert phases["type"] == "phases" and phases["seq"] == 3, phases
assert done["type"] == "done" and done["seq"] == 4, done
print("PASS newline guard: fragment finished, hello is line 2 with seq 2, phases seq 3, done seq 4")
PY

  # 3. --doctor with no usable AWS credentials (the nocreds set above). The
  #    doctor still runs every check it can: the CLI-present one passes, the
  #    credentials one fails. Each check is an event with a status, and every
  #    check that is not a pass names its fix.
  env "${nocreds[@]}" \
    "$B" "$REPO/deploy/deploy-stack.sh" --doctor --region us-east-1 --events "$S/doctor.jsonl" </dev/null >/dev/null 2>&1 || true
  python3 - "$S/doctor.jsonl" <<'PY' || rc=1
import json, sys
evs = [json.loads(line) for line in open(sys.argv[1])]
types = [e["type"] for e in evs]
assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1)), [e["seq"] for e in evs]
assert types[0] == "hello" and types[-1] == "done" and types.count("done") == 1, types
assert evs[-1].get("mode") == "doctor", evs[-1]
checks = [e for e in evs if e["type"] == "check"]
assert checks, "doctor produced no check events: %r" % types
for c in checks:
    assert c.get("item"), c
    assert c["status"] in ("pass", "warn", "fail"), c
    if c["status"] != "pass":
        assert c.get("fix"), "a %s without a fix: %r" % (c["status"], c)
assert not any("password" in e for e in evs), [e for e in evs if "password" in e]
# Backstop: the doctor never signed in. A test that only cleared static keys
# still reached STS wherever the CLI takes credentials from a container or
# web-identity endpoint, and the credentials check must be the local failure.
assert not any(c["item"].startswith("Signed in as") for c in checks), checks
assert any(c["status"] == "fail" and ("credentials" in c["item"] or "not installed" in c["item"]) for c in checks), checks
by = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail")}
print("PASS doctor: %d checks (%d pass, %d warn, %d fail), every non-pass has a fix"
      % (len(checks), by["pass"], by["warn"], by["fail"]))
PY
done
exit $rc
