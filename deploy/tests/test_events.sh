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
