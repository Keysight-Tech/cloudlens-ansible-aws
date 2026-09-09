#!/usr/bin/env bash
# deploy/tests/test_prompt_pipe.sh: the console answers prompts through a FIFO
# it owns. With --prompt-pipe the script must emit each question as a prompt
# event and read the reply from the pipe, never from stdin; a secret is an
# event with no default and is never echoed; an empty reply means the default;
# and the ids stay unique even though nearly every call site is x="$(ask ...)",
# where a shell counter would never advance.
#
# Only the helpers are exercised, lifted out of the script by awk into a file.
# That relies on them being written name() { ... } with both braces at column
# 0. Two portability traps shaped this: macOS awk 20200816 keeps only the last
# of several range patterns, and bash 3.2 sources nothing from <(...) because
# it sizes the input with fstat, which reports a pipe as empty.
set -u
cd "$(dirname "$0")/../.."
S=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$S"' EXIT
mkfifo "$S/answers"
EV="$S/ev.jsonl"
awk '/^(json_str|emit_event|ask|ask_secret)\(\)/{p=1} p{print} p&&/^}/{p=0}' deploy/deploy-stack.sh > "$S/helpers.sh"

harness='
  source "$3"
  INTERACTIVE=true PROMPT_PIPE="$1" EVENTS_FILE="$2"
'
# The console's protocol: an answer is written only after its prompt event
# has appeared. Give up after 5s so a script that never asks cannot hang us.
answer_when() { # answer_when ID TEXT
  ( i=0; until grep -q "\"id\":\"$1\"" "$EV" 2>/dev/null || (( i++ > 50 )); do sleep 0.1; done
    printf '%s\n' "$2" > "$S/answers" ) &
}
rc=0

# 1. A text prompt: the answer comes from the pipe with stdin closed, and the
#    transcript still reads like a terminal session (prompt, then the answer).
answer_when p1 "hello-from-ui"
out=$(/bin/bash -c "$harness"'ask "Type something: " "default"' _ "$S/answers" "$EV" "$S/helpers.sh" </dev/null 2>"$S/err1")
if [[ "$out" == "hello-from-ui" ]]; then echo "PASS answer came from the pipe"; else echo "FAIL got '$out'"; rc=1; fi
if grep -q 'Type something: hello-from-ui' "$S/err1"; then echo "PASS transcript shows the question and the answer"
else echo "FAIL transcript: $(cat "$S/err1")"; rc=1; fi
python3 - "$EV" <<'PY' || rc=1
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1])]
p = [e for e in evs if e["type"] == "prompt"]
assert len(p) == 1 and p[0]["id"] == "p1" and p[0]["kind"] == "text" \
    and p[0]["question"] == "Type something: " and p[0]["default"] == "default", p
print("PASS prompt event emitted")
PY

# 2. A secret: kind=secret, no default key at all, the value never reaches
#    the transcript, and the id continues the sequence of the same run.
answer_when p2 "s3cret-value"
out=$(/bin/bash -c "$harness"'ask_secret "Paste the key: "' _ "$S/answers" "$EV" "$S/helpers.sh" </dev/null 2>"$S/err2")
if [[ "$out" == "s3cret-value" ]]; then echo "PASS secret came from the pipe"; else echo "FAIL secret: got '$out'"; rc=1; fi
if grep -q 's3cret' "$S/err2"; then echo "FAIL the secret was echoed: $(cat "$S/err2")"; rc=1
elif grep -q 'Paste the key: ' "$S/err2"; then echo "PASS the secret prompt shows, the value does not"
else echo "FAIL transcript lacks the secret prompt: $(cat "$S/err2")"; rc=1; fi
python3 - "$EV" <<'PY' || rc=1
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1])]
p = [e for e in evs if e["type"] == "prompt"]
assert len(p) == 2 and p[1]["id"] == "p2" and p[1]["kind"] == "secret" \
    and p[1]["question"] == "Paste the key: " and "default" not in p[1], p
assert not any("s3cret" in json.dumps(e) for e in evs), "the secret leaked into an event"
print("PASS secret prompt event: kind=secret, no default, value not in the events")
PY

# 3. An empty line on the pipe means "take the default", as Enter does.
answer_when p3 ""
out=$(/bin/bash -c "$harness"'ask "Region [us-east-1]: " "us-east-1"' _ "$S/answers" "$EV" "$S/helpers.sh" </dev/null 2>/dev/null)
if [[ "$out" == "us-east-1" ]]; then echo "PASS empty reply takes the default"; else echo "FAIL empty reply: got '$out'"; rc=1; fi

# 4. Two asks captured with $( ) in ONE process, the way the script calls
#    them: each answer lands in the right variable and the ids stay distinct.
answer_when p4 "first-answer"
answer_when p5 "second-answer"
out=$(/bin/bash -c "$harness"'a="$(ask "First: " "")"; b="$(ask "Second: " "")"; printf "%s|%s" "$a" "$b"' \
      _ "$S/answers" "$EV" "$S/helpers.sh" </dev/null 2>/dev/null)
if [[ "$out" == "first-answer|second-answer" ]]; then echo "PASS two captured asks in one process"
else echo "FAIL captured asks: got '$out'"; rc=1; fi
python3 - "$EV" <<'PY' || rc=1
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1])]
ids = [e["id"] for e in evs if e["type"] == "prompt"]
assert ids == ["p1", "p2", "p3", "p4", "p5"], ids
assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1)), [e["seq"] for e in evs]
print("PASS prompt ids are unique and ordered across subshells: %s" % ", ".join(ids))
PY

# 5. The whole script, end to end: a dry run with --prompt-pipe, stdin closed
#    and no AWS credentials at all (the same hermetic set as test_events.sh),
#    driven by a stand-in for the console that answers every prompt event as
#    it appears: yes to KVO, no to vPB, the default to everything else. The
#    only way DEPLOY_KVO can become true here is through the pipe.
REPO=$(pwd -P)
nocreds=(-u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN
         -u AWS_CONTAINER_CREDENTIALS_FULL_URI -u AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
         -u AWS_CONTAINER_AUTHORIZATION_TOKEN -u AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
         -u AWS_WEB_IDENTITY_TOKEN_FILE -u AWS_ROLE_ARN
         -u CLOUDLENS_VC_PASSWORD -u CLOUDLENS_KVO_ADMIN_PASS -u CLOUDLENS_KEY_PEM -u CLOUDLENS_VC_CREDS_FILE
         AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true)
E2E="$S/e2e.jsonl"
console_stand_in() { # answers prompt events on $E2E through the FIFO until done, 90s at most
  ( answered=0 i=0
    while (( i++ < 900 )); do
      n=$(grep -c '"type":"prompt"' "$E2E" 2>/dev/null) || true
      if (( ${n:-0} > answered )); then
        q=$(grep '"type":"prompt"' "$E2E" | sed -n "$((answered + 1))p")
        case "$q" in *"Deploy KVO"*) a=y ;; *"Deploy vPB"*) a=n ;; *) a="" ;; esac
        printf '%s\n' "$a" > "$S/answers"; answered=$((answered + 1))
      fi
      grep -q '"type":"done"' "$E2E" 2>/dev/null && break
      sleep 0.1
    done ) &
}
console_stand_in
( cd "$S" && env "${nocreds[@]}" HOME="$S" /bin/bash "$REPO/deploy/deploy-stack.sh" \
    --dry-run --region us-east-1 --key-name k --stack-name pp --tapping none \
    --events "$E2E" --prompt-pipe "$S/answers" </dev/null >"$S/e2e.out" 2>&1 )
code=$?
wait
if [[ $code -eq 0 ]]; then echo "PASS end to end: dry run over the pipe exited 0"
else echo "FAIL end to end: exit $code; $(tail -3 "$S/e2e.out")"; rc=1; fi
if grep -q '^Deploy KVO (Keysight Vision Orchestrator) alongside vController? \[y/N\]: y$' "$S/e2e.out" \
   && grep -q '^\[ok\] Deploy KVO: true$' "$S/e2e.out"; then echo "PASS the transcript shows the KVO question answered y from the pipe"
else echo "FAIL transcript: $(grep -n 'Deploy KVO' "$S/e2e.out")"; rc=1; fi
python3 - "$E2E" <<'PY' || rc=1
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1])]
assert evs[0]["type"] == "hello" and evs[-1]["type"] == "done" and evs[-1]["status"] == "dry-run", (evs[0], evs[-1])
assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
p = [e for e in evs if e["type"] == "prompt"]
assert p and all(e["kind"] == "text" and e["id"] == "p%d" % (i + 1) for i, e in enumerate(p)), p
assert any("Deploy KVO" in e["question"] for e in p) and any("Deploy vPB" in e["question"] for e in p), p
kinds = [e["kind"] for e in evs if e["type"] == "resource"]
assert "kvo" in kinds and "vpb" not in kinds, kinds
assert not any("password" in e for e in evs)
print("PASS end to end: %d prompts answered, kvo row present, no vpb row" % len(p))
PY

# 6. The flag's two input errors are said, not silently ignored: no --events
#    (the questions would have nowhere to go) and a path that is no FIFO.
( cd "$S" && env "${nocreds[@]}" HOME="$S" /bin/bash "$REPO/deploy/deploy-stack.sh" \
    --dry-run --region us-east-1 --key-name k --stack-name pp --prompt-pipe "$S/answers" </dev/null >"$S/noev.out" 2>&1 )
code=$?
if [[ $code -eq 1 ]] && grep -q 'needs --events' "$S/noev.out"; then echo "PASS --prompt-pipe without --events is refused"
else echo "FAIL --prompt-pipe without --events: exit $code; $(tail -2 "$S/noev.out")"; rc=1; fi
( cd "$S" && env "${nocreds[@]}" HOME="$S" /bin/bash "$REPO/deploy/deploy-stack.sh" \
    --dry-run --region us-east-1 --key-name k --stack-name pp --events "$S/nofifo.jsonl" --prompt-pipe "$S/e2e.out" </dev/null >/dev/null 2>&1 )
code=$?
python3 - "$S/nofifo.jsonl" "$code" <<'PY' || rc=1
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1])]
assert sys.argv[2] == "1", "exit %s, expected 1" % sys.argv[2]
assert [e["type"] for e in evs] == ["hello", "done"] and evs[-1]["status"] == "failed", evs
assert "named pipe" in evs[-1]["reason"], evs[-1]
print("PASS a --prompt-pipe that is no FIFO fails with hello + done naming it")
PY
exit $rc
