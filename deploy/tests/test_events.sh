#!/usr/bin/env bash
# deploy/tests/test_events.sh: the --events sink writes valid, ordered JSON
# lines, exactly one done per run, and never a byte JSON forbids; a partial
# last line is finished before the next hello; the dry run describes the
# stack's resources; --doctor turns every check into an event.
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

rc=0
for B in "${shells[@]}"; do
  echo "== $B ($("$B" -c 'echo "$BASH_VERSION"'))"
  rm -f "$S"/*.jsonl

  # 1. A full dry run. --dry-run touches no AWS and needs no credentials or
  #    profile; the events file must still describe the whole run. It runs
  #    twice: once with nothing optional (no kvo or vpb rows may appear) and
  #    once with KVO and vPB, the only way every login event is emitted.
  dry=(env -u CLOUDLENS_VC_PASSWORD -u CLOUDLENS_KVO_ADMIN_PASS -u CLOUDLENS_KEY_PEM -u CLOUDLENS_VC_CREDS_FILE \
       HOME="$S" "$B" "$REPO/deploy/deploy-stack.sh")
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
  python3 - "$S/events.jsonl" "$S/events-full.jsonl" <<'PY' || rc=1
import json, sys
runs = {name: [json.loads(line) for line in open(path)]   # every line is one JSON object
        for name, path in (("minimal", sys.argv[1]), ("full", sys.argv[2]))}
for name, evs in runs.items():
    seqs = [e["seq"] for e in evs]
    types = [e["type"] for e in evs]
    for e in evs:
        assert "ts" in e and "type" in e, e
    assert seqs == list(range(1, len(seqs) + 1)), "%s: seq must be 1..N with no gap or repeat: %r" % (name, seqs)
    assert types[0] == "hello", (name, types[:3])
    assert "phase" in types, (name, types)
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
print("PASS dry-run: %d lines, %d phase events, %d resources, one done; %d logins name no password, vcontroller is factory-default"
      % (len(evs), types.count("phase"), len(res), len(logins)))
PY

  # 2. A parse-time fail() after the sink exists, with every byte JSON hates in
  #    the reason: a quote, a backslash, a newline and a raw ESC.
  "$B" "$REPO/deploy/deploy-stack.sh" --iac $'x"y\\z\nw\x1b' --events "$S/fail.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 1 ]]; then echo "FAIL parse-time fail: exit $code, expected 1"; rc=1; fi
  python3 - "$S/fail.jsonl" <<'PY' || rc=1
import json, sys
lines = open(sys.argv[1]).read().splitlines()
assert len(lines) == 2, "expected hello + done only: %r" % lines
hello, done = (json.loads(line) for line in lines)
assert hello["type"] == "hello" and done["type"] == "done", (hello, done)
assert done["status"] == "failed", done
r = done["reason"]
assert 'x"y\\z' in r and "\n" in r and "\x1b" in r, repr(r)
print("PASS parse-time fail: hello + one done, reason round-trips %r" % r)
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
assert len(lines) == 4 and lines[-1] == "", "partial + hello + done, each newline-terminated: %r" % lines
def parses(s):
    try: json.loads(s); return True
    except ValueError: return False
assert not parses(lines[0]) and lines[0] == '{"seq":1,"type":"pha', "line 1 must stay the untouched fragment: %r" % lines[0]
hello, done = json.loads(lines[1]), json.loads(lines[2])
assert hello["type"] == "hello" and hello["seq"] == 2, hello
assert done["type"] == "done" and done["seq"] == 3, done
print("PASS newline guard: fragment finished, hello is line 2 with seq 2, done seq 3")
PY

  # 3. --doctor with no usable AWS credentials (the profile, static keys and
  #    the container and web-identity credential sources CloudShell, ECS and
  #    CodeBuild inject are unset, the config and credential files are stubbed
  #    out and IMDS is off, so this never reaches STS or a real account). The doctor
  #    still runs every check it can: the CLI-present one passes, the
  #    credentials one fails. Each check is an event with a status, and every
  #    check that is not a pass names its fix.
  env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_CONTAINER_CREDENTIALS_FULL_URI -u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
    -u AWS_CONTAINER_AUTHORIZATION_TOKEN -u AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE \
    -u AWS_WEB_IDENTITY_TOKEN_FILE -u AWS_ROLE_ARN \
    AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true \
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
