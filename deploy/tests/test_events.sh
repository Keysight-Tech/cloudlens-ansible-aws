#!/usr/bin/env bash
# deploy/tests/test_events.sh: the --events sink writes valid, ordered JSON
# lines, exactly one done per run, and never a byte JSON forbids; the dry run
# describes the stack's resources; --doctor turns every check into an event.
#
# Runs under /bin/bash explicitly (3.2 on macOS, the floor the script targets)
# and again under the bash first on PATH when that is a different binary.
set -u
cd "$(dirname "$0")/../.."
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

shells=(/bin/bash)
other=$(command -v bash 2>/dev/null || true)
if [[ -n "$other" && -x "$other" && ! "$other" -ef /bin/bash ]]; then shells+=("$other"); fi

rc=0
for B in "${shells[@]}"; do
  echo "== $B ($("$B" -c 'echo "$BASH_VERSION"'))"
  rm -f "$S"/*.jsonl

  # 1. A full dry run. --dry-run touches no AWS and needs no credentials or
  #    profile; the events file must still describe the whole run.
  "$B" deploy/deploy-stack.sh --dry-run --region us-east-1 --key-name k --stack-name evt \
    --tapping none --no-kvo --no-vpb --events "$S/events.jsonl" </dev/null >/dev/null 2>&1
  code=$?
  if [[ $code -ne 0 ]]; then echo "FAIL dry-run: exit $code, expected 0"; rc=1; fi
  python3 - "$S/events.jsonl" <<'PY' || rc=1
import json, sys
evs = [json.loads(line) for line in open(sys.argv[1])]   # every line is one JSON object
seqs = [e["seq"] for e in evs]
types = [e["type"] for e in evs]
for e in evs:
    assert "ts" in e and "type" in e, e
assert seqs == list(range(1, len(seqs) + 1)), "seq must be 1..N with no gap or repeat: %r" % seqs
assert types[0] == "hello", types[:3]
assert "phase" in types, types
assert types.count("done") == 1, "exactly one done per run: %r" % types
assert types[-1] == "done", types
hello = [e for e in evs if e["type"] == "hello"][-1]
assert hello["stack"] == "evt" and hello["region"] == "us-east-1", hello
assert evs[-1]["status"] == "dry-run", evs[-1]
# The console draws the stack from resource events, so the dry run emits its
# placeholders too (--no-kvo --no-vpb: no kvo and no vpb rows).
res = [e for e in evs if e["type"] == "resource"]
kinds = [(r["kind"], r.get("role")) for r in res]
assert ("vpc", None) in kinds and ("subnet", "mgmt") in kinds and ("vcontroller", None) in kinds, kinds
assert not any(r["kind"] in ("kvo", "vpb") for r in res), kinds
assert all(r["id"] for r in res if r["kind"] in ("vpc", "subnet")), res
# No event may ever carry a password value: logins say where it lives.
assert not any("password" in e for e in evs), [e for e in evs if "password" in e]
print("PASS dry-run: %d lines, %d phase events, %d resources, one done" % (len(evs), types.count("phase"), len(res)))
PY

  # 2. A parse-time fail() after the sink exists, with every byte JSON hates in
  #    the reason: a quote, a backslash, a newline and a raw ESC.
  "$B" deploy/deploy-stack.sh --iac $'x"y\\z\nw\x1b' --events "$S/fail.jsonl" </dev/null >/dev/null 2>&1
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

  # 3. --doctor with no usable AWS credentials (the config and credential
  #    files are stubbed out and IMDS is off, so this never reaches a real
  #    account). The doctor still runs every check it can: the CLI-present one
  #    passes, the credentials one fails. Each check is an event with a
  #    status, and every check that is not a pass names its fix.
  env -u AWS_PROFILE AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_EC2_METADATA_DISABLED=true \
    "$B" deploy/deploy-stack.sh --doctor --region us-east-1 --events "$S/doctor.jsonl" </dev/null >/dev/null 2>&1 || true
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
by = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail")}
print("PASS doctor: %d checks (%d pass, %d warn, %d fail), every non-pass has a fix"
      % (len(checks), by["pass"], by["warn"], by["fail"]))
PY
done
exit $rc
