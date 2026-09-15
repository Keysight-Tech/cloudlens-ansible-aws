#!/usr/bin/env bash
# deploy/tests/test_teardown_release.sh: Phase 4a of the teardown, the KVO
# licence release, and what it does to the licence-loss gate.
#
# The rule under test: the gate (the red warning and the typed stack name,
# or --accept-licence-loss with no terminal) is skipped ONLY when the
# release reports the KVO clear (kvo_license.py exit 0). Every other
# outcome, an unreachable KVO, a refused password, a release that failed or
# whose outcome is unknown, no address, no terminal and no --release-licences,
# leaves the gate exactly as it was. And the password never reaches the
# terminal, the state file, or a command line.
#
# The order of the questions is part of the rule: "Proceed with the
# teardown?" comes BEFORE the release, so licences are only ever stripped
# from a KVO the operator has already committed to destroying. A release
# followed by a "no" at the delete used to leave a running KVO with
# nothing on it.
#
# Hermetic: no AWS account, no KVO, no network. `aws` is a script on PATH
# that answers the read-only probes with a one-instance stack that has a
# KVO, records every call, and flips the stack to DELETE_COMPLETE once
# delete-stack has been asked for. kvo_license.py is a stub reached through
# CLOUDLENS_KVO_LICENSE_PY (the override exists for this suite) that plays
# the exit-code contract: 0 clear, 3 not clear, 6 unreachable or refused,
# or hangs; with --list --json it prints the real script's report and then
# the JSON summary as its last line, which is where the teardown reads the
# count. It records its arguments and the password it was handed through
# --password-env, to a file only this suite reads. The real script's JSON
# against a fake KVO, fed through the teardown's own parse expression, is
# proven in console/tests/test_kvo_license_cli.py.
#
# Two ways of running the whole script, both under /bin/bash (3.2 on macOS,
# the floor the script targets):
#   run_bg   detached from any controlling terminal (a Python os.setsid
#            wrapper), so the script's /dev/tty re-attach cannot fire and
#            INTERACTIVE is false, as it is under cron or a CI runner;
#   run_tty  on a pseudo-terminal, so INTERACTIVE is true and the real
#            prompts run. Answers are typed only after their prompt has
#            appeared, which is what makes "the password was never echoed"
#            a statement about the script and not about typed-ahead input.
#
# Usage: bash deploy/tests/test_teardown_release.sh
#        TEARDOWN_STACK_SH=/path/to/teardown-stack.sh bash deploy/tests/test_teardown_release.sh
# The second form points the suite at another copy of the script. That is how
# the feature is proven: the suite has to FAIL against the version before it.
set -u
cd "$(dirname "$0")/../.."
SCRIPT="$(cd "$(dirname "${TEARDOWN_STACK_SH:-deploy/teardown-stack.sh}")" && pwd)/$(basename "${TEARDOWN_STACK_SH:-deploy/teardown-stack.sh}")"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT
mkdir -p "$S/bin"

# ---- the aws stub -------------------------------------------------------
cat > "$S/bin/aws" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$AWS_CALLS"
case "$*" in
  *"sts get-caller-identity"*"Arn"*)              echo "arn:aws:iam::123456789012:user/test" ;;
  *"sts get-caller-identity"*"Account"*)          echo "123456789012" ;;
  *"cloudformation delete-stack"*)                : > "$AWS_STATE/deleted" ;;
  *"describe-stacks"*"StackStatus"*)              if [[ -f "$AWS_STATE/deleted" ]]; then echo DELETE_COMPLETE; else echo CREATE_COMPLETE; fi ;;
  *"describe-stacks"*"DeployKVO"*)                echo yes ;;
  *"describe-stacks"*"KvoAddress"*)               echo "${STUB_KVO_ADDR-54.1.2.3}" ;;
  *"describe-stacks"*"KvoName"*)                  echo None ;;
  *"describe-stack-resources"*"KvoInstance"*)     echo "${STUB_KVO_IID-i-0kvo}" ;;
  *"describe-stack-resources"*"AWS::EC2::Instance"*)      printf 'i-0kvo\ti-0vc\n' ;;
  *"describe-stack-resources"*"AWS::EC2::SecurityGroup"*) echo "sg-0stack" ;;
  *"describe-stack-resources"*"AWS::EC2::VPC"*)           echo "vpc-0stack" ;;
  *"describe-stack-resources"*"length(StackResources)"*)  echo 12 ;;
  *"describe-instances"*"--instance-ids i-0kvo"*"PublicIpAddress"*) printf '%s\n' "${STUB_KVO_IPS-None,10.0.0.11}" | tr ',' '\t' ;;
  *"describe-instances"*"tag:Name"*"PublicIpAddress"*)    printf '%s\n' "${STUB_KVO_TAG_IPS-None,None}" | tr ',' '\t' ;;
  *"describe-instances"*"BlockDeviceMappings"*)   echo "vol-0a" ;;
  *"describe-volumes --volume-ids"*)              printf 'vol-0a\t8\tgp2\tin-use\n' ;;
  *)                                              echo "" ;;
esac
exit 0
EOF
chmod +x "$S/bin/aws"

# ---- the kvo_license.py stub ---------------------------------------------
cat > "$S/kvo_license.py" <<'EOF'
import json, os, sys, time
args = sys.argv[1:]
open(os.environ["LIC_CALLS"], "a").write(" ".join(args) + "\n")
pw_env = args[args.index("--password-env") + 1] if "--password-env" in args else ""
open(os.environ["LIC_SEEN"], "a").write("pw=%s\n" % (os.environ.get(pw_env, "<unset>") if pw_env else "<no --password-env>"))
kvo = args[args.index("--kvo") + 1] if "--kvo" in args else "?"
mode = os.environ.get("STUB_LIC_MODE", "ok")
if mode == "hang":
    time.sleep(300)
if mode == "unreach":
    print("[license] could not reach the KVO at %s (timed out)" % kvo, file=sys.stderr); sys.exit(6)
if mode == "badpw":
    print("[license] the KVO at %s refused the credentials for user 'admin' (HTTP 401)" % kvo, file=sys.stderr); sys.exit(6)
if "--list" in args:
    n = 0 if mode == "empty" else 2
    rows = [] if not n else [
        {"part": "KVO-DEV-01", "product": "KVO-DEVICE", "quantity": 5, "code_last4": "****-1111", "expiry": "2027-01-31"},
        {"part": "-", "product": "CL-CREDIT", "quantity": 20, "code_last4": "****-2222", "expiry": "-"}]
    print("[license] %d licence(s) installed on KVO %s" % (n, kvo))
    if n:
        print("    part             product                  quantity  code       expiry")
        print("    KVO-DEV-01       KVO-DEVICE                      5  ****-1111  2027-01-31")
        print("    -                CL-CREDIT                      20  ****-2222  -")
    # "noline": the prose of a copy that predates --json, and no JSON line
    if "--json" in args and mode != "noline":
        sys.stdout.flush()
        print(json.dumps({"kvo": kvo, "count": n, "clear": n == 0, "licences": rows, "unreadable": None, "exit": 0}))
    sys.exit(0)
if "--release-all" in args:
    if mode == "fail3":
        print("[license]   ****-1111 x5: released")
        print("[license]   ****-2222 x20: outcome UNKNOWN (no terminal state within the time budget; last state 'IN_PROGRESS'): not counted as released")
        print("[license] 1 operation(s) with an UNKNOWN outcome (not success): ****-2222", file=sys.stderr)
        print("[license] NOT clear: the counts still on this KVO will be stranded if it is deleted", file=sys.stderr)
        sys.exit(3)
    print("[license]   ****-1111 x5: released")
    print("[license]   ****-2222 x20: released")
    print("[license] released 2 licence(s); the KVO reports no licence left. Nothing will be stranded.")
    sys.exit(0)
print("stub: unexpected arguments", file=sys.stderr); sys.exit(9)
EOF

# ---- run the script with no controlling terminal --------------------------
cat > "$S/detach.py" <<'EOF'
import os, subprocess, sys
os.setsid()
sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
EOF

# ---- run the script on a pty, answering prompts as they appear ------------
# pty_run.py OUTFILE 'expect<TAB>answer'... -- cmd args...
# An answer written as '@secret:VALUE' is typed only once the script has
# turned the terminal's ECHO off (bash prints a read -s prompt first and
# clears ECHO a moment later, and a human is slower than that moment). If
# ECHO never drops the run fails with 125: the script would have echoed the
# password. That is the check, not a grep after the fact.
cat > "$S/pty_run.py" <<'EOF'
import os, pty, select, sys, termios, time
out = open(sys.argv[1], "wb")
spec, i = [], 2
while sys.argv[i] != "--":
    e, a = sys.argv[i].split("\t", 1); spec.append((e, a)); i += 1
cmd = sys.argv[i + 1:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
buf, deadline = b"", time.time() + 45

def pump():
    global buf
    r, _, _ = select.select([fd], [], [], 1)
    if not r:
        return True
    try:
        c = os.read(fd, 4096)
    except OSError:
        return False
    if not c:
        return False
    buf += c; out.write(c); out.flush()
    return True

alive = True
for expect, answer in spec:
    while buf.find(expect.encode()) < 0 and time.time() < deadline and alive:
        alive = pump()
    at = buf.find(expect.encode())
    if at < 0:
        out.write(("\nEXPECT TIMEOUT waiting for %r\n" % expect).encode()); out.flush()
        os.kill(pid, 9); os.waitpid(pid, 0); sys.exit(124)
    buf = buf[at + len(expect):]
    if answer.startswith("@secret:"):
        answer = answer[len("@secret:"):]
        t0 = time.time()
        while termios.tcgetattr(fd)[3] & termios.ECHO:
            if time.time() - t0 > 5:
                out.write(b"\nECHO STILL ON at the secret prompt\n"); out.flush()
                os.kill(pid, 9); os.waitpid(pid, 0); sys.exit(125)
            time.sleep(0.01)
    os.write(fd, answer.encode())
while alive and time.time() < deadline:
    alive = pump()
_, st = os.waitpid(pid, 0)
sys.exit(os.WEXITSTATUS(st) if os.WIFEXITED(st) else 128 + os.WTERMSIG(st))
EOF

rc=0
pass() { printf 'PASS %s\n' "$1"; }
failt() { printf 'FAIL %s\n' "$1"; rc=1; }
flat() { tr '\r\n' '||' < "$1" | cut -c1-1500; }
has() { grep -q -- "$2" "$1"; }
# at FILE STRING: the character position of STRING's first occurrence in
# FILE (0 when absent), so the ORDER of two prompts can be asserted.
at() { tr '\r\n' '  ' < "$1" | awk -v s="$2" '{ print index($0, s) }'; }
lic_called() { grep -q -- "$2" "$LIC" 2>/dev/null; }

# setup NAME: fresh per-case files and a work dir (the script writes its
# state file to $PWD, so the cwd must not be the repo).
setup() {
  W="$S/$1"; mkdir -p "$W/work" "$W/home"
  OUT="$W/out"; AWS="$W/aws-calls"; LIC="$W/lic-calls"; SEEN="$W/lic-seen"; STATE_DIR="$W/aws-state"
  : > "$AWS"; : > "$LIC"; : > "$SEEN"; mkdir -p "$STATE_DIR"
  ENV=(-u CLOUDLENS_KVO_ADMIN_USER -u CLOUDLENS_KVO_ADMIN_PASS
       PATH="$S/bin:$PATH" HOME="$W/home" AWS_CALLS="$AWS" AWS_STATE="$STATE_DIR"
       LIC_CALLS="$LIC" LIC_SEEN="$SEEN" CLOUDLENS_KVO_LICENSE_PY="$S/kvo_license.py"
       CLOUDLENS_PROBE_TIMEOUT=5 TERM=dumb)
}

# run_bg NAME "EXTRA_ENV..." ARGS...: no terminal anywhere. $code, $OUT.
run_bg() {
  local name="$1" extra="$2"; shift 2
  setup "$name"
  ( cd "$W/work" && env "${ENV[@]}" $extra python3 "$S/detach.py" /bin/bash "$SCRIPT" --stack-name lab --region us-east-1 "$@" ) >"$OUT" 2>&1
  code=$?
}

# run_tty NAME "EXTRA_ENV..." "expect<TAB>answer"... -- ARGS...: on a pty.
run_tty() {
  local name="$1" extra="$2"; shift 2
  setup "$name"
  local spec=()
  while [[ "$1" != "--" ]]; do spec+=("$1"); shift; done
  shift
  ( cd "$W/work" && env "${ENV[@]}" $extra python3 "$S/pty_run.py" "$OUT" "${spec[@]}" -- /bin/bash "$SCRIPT" --stack-name lab --region us-east-1 "$@" ) >/dev/null 2>&1
  code=$?
}

GATE='Type the stack name'
WARNING='LICENCES ARE ABOUT TO BE STRANDED'
PROCEED='Proceed with the teardown? [y/N]: '
RELEASE_Q='Release all 2 licences from this KVO now? [Y/n]: '
T=$'\t'

# 1. Interactive, the release succeeds: the teardown is confirmed FIRST,
#    then the licences are listed and released, the gate is skipped, no
#    typed name is asked for, the run proceeds to the delete. The password
#    was typed at a no-echo prompt, reached the script through the
#    environment, and appears nowhere: not on the terminal, not in the
#    state file, not on the command line the stub recorded.
run_tty tty_ok "STUB_LIC_MODE=ok" \
  "${PROCEED}${T}y"$'\n' \
  "user [admin]: ${T}"$'\n' \
  "password [admin]: ${T}@secret:s3cret-pw-77"$'\n' \
  "${RELEASE_Q}${T}"$'\n' \
  --
if [[ $code -eq 0 ]] && ! has "$OUT" "$GATE" && ! has "$OUT" "$WARNING" \
   && has "$OUT" "All 2 licences released" && has "$OUT" "nothing will be stranded" \
   && has "$OUT" "Phase 4a released every licence" && has "$OUT" "no licence-loss confirmation is needed" \
   && has "$AWS" "cloudformation delete-stack"; then
  pass "1. release ok (tty): gate skipped, no typed name, teardown proceeds to delete-stack, waiver says released"
else failt "1. release ok (tty): exit $code, delete-stack=$(grep -c delete-stack "$AWS"), out=[$(flat "$OUT")]"; fi
if (( $(at "$OUT" "$PROCEED") > 0 )) && (( $(at "$OUT" "$PROCEED") < $(at "$OUT" "$RELEASE_Q") )) \
   && (( $(at "$OUT" "$RELEASE_Q") < $(at "$OUT" "cloudformation delete-stack") || $(at "$OUT" "cloudformation delete-stack") == 0 )); then
  pass "1. release ok (tty): 'Proceed with the teardown?' is asked BEFORE the release prompt"
else failt "1. question order: proceed@$(at "$OUT" "$PROCEED") release@$(at "$OUT" "$RELEASE_Q") out=[$(flat "$OUT")]"; fi
if lic_called "$LIC" "--list" && lic_called "$LIC" "--release-all" && grep -q '^pw=s3cret-pw-77$' "$SEEN" \
   && ! grep -q 's3cret-pw-77' "$OUT" && ! grep -q 's3cret-pw-77' "$LIC" \
   && ! grep -rq 's3cret-pw-77' "$W/work" && grep -q -- '--password-env CLOUDLENS_KVO_ADMIN_PASS' "$LIC"; then
  pass "1. release ok (tty): password typed with ECHO off, reached the script via the environment only, never the terminal, state file or argv"
else failt "1. password handling: lic=[$(flat "$LIC")] seen=[$(flat "$SEEN")] echoed=$(grep -c s3cret-pw-77 "$OUT") in-work=$(grep -rl s3cret-pw-77 "$W/work" | wc -l | tr -d ' ')"; fi
if [[ -f "$W/work/.cloudlens-deploy-lab-us-east-1.state" ]] && ! grep -qi 'pass' "$W/work/.cloudlens-deploy-lab-us-east-1.state"; then
  pass "1. release ok (tty): the state file was written and carries no password"
else failt "1. state file: [$(ls "$W/work")] [$(cat "$W/work"/.cloudlens-deploy-* 2>/dev/null | tr '\n' '|')]"; fi

# 2. Interactive, the release comes back exit 3 (an op unknown, the KVO not
#    clear): the warning prints, the gate demands the stack name, a wrong
#    answer stops the run, nothing is deleted.
run_tty tty_fail3 "STUB_LIC_MODE=fail3" \
  "${PROCEED}${T}y"$'\n' \
  "user [admin]: ${T}"$'\n' \
  "password [admin]: ${T}@secret:"$'\n' \
  "${RELEASE_Q}${T}"$'\n' \
  "${GATE}${T}nope"$'\n' \
  --
if [[ $code -ne 0 ]] && has "$OUT" "$WARNING" && has "$OUT" "$GATE" && has "$OUT" "Not confirmed" \
   && has "$OUT" "did not leave the KVO clear" && lic_called "$LIC" "--release-all" \
   && ! has "$AWS" "delete-stack" && grep -q '^pw=admin$' "$SEEN"; then
  pass "2. release exit 3 (tty): warning + typed-name gate run unchanged, wrong name stops it, nothing deleted"
else failt "2. release exit 3 (tty): exit $code, delete-stack=$(grep -c delete-stack "$AWS"), out=[$(flat "$OUT")]"; fi

# 3. No terminal, --yes, no --release-licences: today's behaviour. The
#    licences are listed (read-only) but not released, the gate stops the
#    run for want of --accept-licence-loss, and the operator is told the
#    flag that would have released them.
run_bg bg_noflag "STUB_LIC_MODE=ok" --yes
if [[ $code -ne 0 ]] && has "$OUT" "$WARNING" && has "$OUT" "no terminal to confirm on" \
   && has "$OUT" "accept-licence-loss" && ! has "$AWS" "delete-stack" \
   && lic_called "$LIC" "--list" && ! lic_called "$LIC" "--release-all" \
   && has "$OUT" "no --release-licences: not releasing"; then
  pass "3. no terminal, no --release-licences: nothing released, gate stops the run as before, flag named"
else failt "3. no terminal, no flag: exit $code, lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 4. No terminal, --yes --release-licences, release ok: gate skipped, the
#    delete goes ahead, no --accept-licence-loss needed. --kvo-address wins
#    over the stack lookup.
run_bg bg_flag "STUB_LIC_MODE=ok" --yes --release-licences --kvo-address 10.9.9.9
if [[ $code -eq 0 ]] && ! has "$OUT" "$WARNING" && ! has "$OUT" "$GATE" \
   && has "$OUT" "All 2 licences released" && has "$AWS" "cloudformation delete-stack" \
   && lic_called "$LIC" "--list --json" && lic_called "$LIC" "--release-all" && grep -q -- '--kvo 10.9.9.9 ' "$LIC" \
   && has "$OUT" "    KVO-DEV-01       KVO-DEVICE" && ! has "$OUT" '"count": 2'; then
  pass "4. --release-licences, release ok: gate waived, delete proceeds, --kvo-address honoured, list read as --json with the report shown and the JSON line not"
else failt "4. --release-licences ok: exit $code, lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 5. No terminal, --yes --release-licences, release exit 3: fail closed.
run_bg bg_flag_fail3 "STUB_LIC_MODE=fail3" --yes --release-licences
if [[ $code -ne 0 ]] && has "$OUT" "$WARNING" && has "$OUT" "did not leave the KVO clear" \
   && has "$OUT" "no terminal to confirm on" && ! has "$AWS" "delete-stack" && lic_called "$LIC" "--release-all"; then
  pass "5. --release-licences, release exit 3: gate runs, run stops, nothing deleted"
else failt "5. --release-licences exit 3: exit $code, out=[$(flat "$OUT")]"; fi

# 6. --dry-run: says what it would do, calls the KVO for nothing, deletes
#    nothing.
run_bg bg_dry "STUB_LIC_MODE=ok" --dry-run --release-licences
if [[ $code -eq 0 ]] && ! has "$AWS" "delete-stack" && [[ ! -s "$LIC" ]] \
   && has "$OUT" "would run scripts/kvo_license.py --list" && has "$OUT" "nothing is called on the KVO in a dry run" \
   && has "$OUT" "KVO address: 54.1.2.3" && has "$OUT" "DRY RUN: nothing above was actually deleted"; then
  pass "6. --dry-run: says what it would do, kvo_license.py never invoked, nothing deleted"
else failt "6. --dry-run: exit $code, lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 7. KVO unreachable (exit 6 from the list): a warning with the reason, no
#    release attempted, the gate runs. Within the bound, not after a hang.
t0=$(date +%s)
run_bg bg_unreach "STUB_LIC_MODE=unreach" --yes --release-licences
took=$(( $(date +%s) - t0 ))
if [[ $code -ne 0 ]] && has "$OUT" "could not reach the KVO at 54.1.2.3" && has "$OUT" "could not be used: unreachable" \
   && has "$OUT" "$WARNING" && ! lic_called "$LIC" "--release-all" && ! has "$AWS" "delete-stack" && (( took < 30 )); then
  pass "7. KVO unreachable: warned with the reason in ${took}s, nothing released, gate runs"
else failt "7. KVO unreachable: exit $code in ${took}s, out=[$(flat "$OUT")]"; fi

# 8. KVO answers nothing at all (the script hangs): the list is killed at
#    the bash-side bound, warned, and the gate runs. The bound is the
#    per-call timeout times three plus five: 1s here, so 8s.
t0=$(date +%s)
run_bg bg_hang "STUB_LIC_MODE=hang CLOUDLENS_KVO_HTTP_TIMEOUT=1" --yes --release-licences
took=$(( $(date +%s) - t0 ))
if [[ $code -ne 0 ]] && has "$OUT" "did not answer within 8s" && has "$OUT" "$WARNING" \
   && ! lic_called "$LIC" "--release-all" && ! has "$AWS" "delete-stack" && (( took < 40 )); then
  pass "8. KVO never answers: list killed at the bound (${took}s total), gate runs"
else failt "8. KVO never answers: exit $code in ${took}s, out=[$(flat "$OUT")]"; fi

# 9. Wrong password from the environment: exit 6, warned, gate runs, and the
#    password value is nowhere in the output.
run_bg bg_badpw "STUB_LIC_MODE=badpw CLOUDLENS_KVO_ADMIN_PASS=wrong-pw-42 CLOUDLENS_KVO_ADMIN_USER=ops" --yes --release-licences
if [[ $code -ne 0 ]] && has "$OUT" "refused the credentials" && has "$OUT" "$WARNING" \
   && grep -q '^pw=wrong-pw-42$' "$SEEN" && ! grep -q 'wrong-pw-42' "$OUT" && ! grep -q 'wrong-pw-42' "$LIC" \
   && grep -q -- '--user ops ' "$LIC" && ! has "$AWS" "delete-stack"; then
  pass "9. wrong password (env): warned, gate runs, env user honoured, password never printed"
else failt "9. wrong password: exit $code, seen=[$(flat "$SEEN")] out=[$(flat "$OUT")]"; fi

# 10. The KVO holds nothing: nothing to strand, gate skipped, delete proceeds.
run_bg bg_empty "STUB_LIC_MODE=empty" --yes
if [[ $code -eq 0 ]] && has "$OUT" "holds no licences" && ! has "$OUT" "$WARNING" \
   && has "$OUT" "Phase 4a found the KVO held nothing" && ! has "$OUT" "released every licence" \
   && ! lic_called "$LIC" "--release-all" && has "$AWS" "delete-stack"; then
  pass "10. KVO holds no licences: nothing to release, gate waived with 'held nothing', delete proceeds"
else failt "10. empty KVO: exit $code, out=[$(flat "$OUT")]"; fi

# 11. No address anywhere (output None, instance has no IPs, no tagged
#     instance): warned, kvo_license.py never run, gate runs.
run_bg bg_noaddr "STUB_LIC_MODE=ok STUB_KVO_ADDR=None STUB_KVO_IPS=None,None" --yes --release-licences
if [[ $code -ne 0 ]] && has "$OUT" "Could not find the KVO's address" && [[ ! -s "$LIC" ]] \
   && has "$OUT" "$WARNING" && ! has "$AWS" "delete-stack"; then
  pass "11. no KVO address: warned, nothing called, gate runs"
else failt "11. no KVO address: exit $code, lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 12. The address falls back to the instance when the output is missing:
#     public first, else private.
run_bg bg_fallback_ip "STUB_LIC_MODE=ok STUB_KVO_ADDR=None STUB_KVO_IPS=None,10.0.0.11" --yes --release-licences
if [[ $code -eq 0 ]] && grep -q -- '--kvo 10.0.0.11 ' "$LIC"; then
  pass "12. no KvoAddress output: the KvoInstance's private IP is used"
else failt "12. address fallback: exit $code, lic=[$(flat "$LIC")]"; fi

# 13a. The count comes from kvo_license.py's JSON line, never its prose. A
#      copy that prints the old prose ("2 licence(s) installed") and no
#      JSON line is "could not tell": nothing is released on it, the gate
#      runs. Before this the prose was parsed and the release went ahead.
run_bg bg_noline "STUB_LIC_MODE=noline" --yes --release-licences
if [[ $code -ne 0 ]] && has "$OUT" "Could not tell how many licences the KVO holds" \
   && has "$OUT" "Not releasing on a guess" && lic_called "$LIC" "--list --json" \
   && ! lic_called "$LIC" "--release-all" && has "$OUT" "$WARNING" && ! has "$AWS" "delete-stack"; then
  pass "13a. list without a JSON line: count not taken from the prose, nothing released, gate runs"
else failt "13a. list without a JSON line: exit $code, lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 13. --help documents the release, and the order of the questions.
run_bg help "" --help
if [[ $code -eq 0 ]] && has "$OUT" "release-licences" && has "$OUT" "kvo-admin-pass" \
   && has "$OUT" "Deactivate licenses" && has "$OUT" "4a." && has "$OUT" "4b." \
   && has "$OUT" "Asked before any licence is touched"; then
  pass "13. --help names the release flags, the UI route, steps 4a and 4b, and why 4 comes first"
else failt "13. --help: exit $code, out=[$(flat "$OUT")]"; fi

# 14. Interactive, the operator confirms the teardown and then answers "n"
#     to the release: nothing is released, the warning and the typed-name
#     gate run, and the correct stack name accepts the loss and proceeds to
#     the delete. The release prompt comes after the proceed question and
#     before the gate.
run_tty tty_decline_release "STUB_LIC_MODE=ok" \
  "${PROCEED}${T}y"$'\n' \
  "user [admin]: ${T}"$'\n' \
  "password [admin]: ${T}@secret:"$'\n' \
  "${RELEASE_Q}${T}n"$'\n' \
  "${GATE}${T}lab"$'\n' \
  --
if [[ $code -eq 0 ]] && has "$OUT" "Nothing was released" && has "$OUT" "$WARNING" && has "$OUT" "$GATE" \
   && has "$OUT" "Licence loss accepted" && lic_called "$LIC" "--list --json" && ! lic_called "$LIC" "--release-all" \
   && has "$AWS" "cloudformation delete-stack" \
   && (( $(at "$OUT" "$PROCEED") < $(at "$OUT" "$RELEASE_Q") )) && (( $(at "$OUT" "$RELEASE_Q") < $(at "$OUT" "$GATE") )); then
  pass "14. release declined (tty): nothing released, gate runs after it, the typed name accepts the loss, delete proceeds"
else failt "14. release declined: exit $code, delete-stack=$(grep -c delete-stack "$AWS"), lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 15. Interactive, the operator declines the teardown itself: the run stops
#     before the KVO is even asked about. kvo_license.py is never invoked,
#     so a "no" here can never leave an unlicensed KVO behind.
run_tty tty_decline_teardown "STUB_LIC_MODE=ok" \
  "${PROCEED}${T}n"$'\n' \
  --
if [[ $code -ne 0 ]] && has "$OUT" "Aborted. Nothing was deleted, and no licence was released." \
   && [[ ! -s "$LIC" ]] && ! has "$OUT" "$RELEASE_Q" && ! has "$OUT" "password" && ! has "$AWS" "delete-stack"; then
  pass "15. teardown declined (tty): stops before the licences are listed, kvo_license.py never run, nothing deleted"
else failt "15. teardown declined: exit $code, lic=[$(flat "$LIC")] out=[$(flat "$OUT")]"; fi

# 16. No terminal, --yes --release-licences --accept-licence-loss, and the
#     release comes back exit 3: the loss was accepted, so the run proceeds
#     to the delete with the warning printed. The teardown was confirmed
#     (--yes) before the release was attempted.
run_bg bg_flag_fail3_accept "STUB_LIC_MODE=fail3" --yes --release-licences --accept-licence-loss
if [[ $code -eq 0 ]] && has "$OUT" "$WARNING" && has "$OUT" "did not leave the KVO clear" \
   && has "$OUT" "Licence loss accepted (--accept-licence-loss)" && lic_called "$LIC" "--release-all" \
   && has "$AWS" "cloudformation delete-stack" \
   && (( $(at "$OUT" "Proceeding (--yes)") > 0 )) && (( $(at "$OUT" "Proceeding (--yes)") < $(at "$OUT" "Releasing all 2 licences") )); then
  pass "16. --release-licences exit 3 with --accept-licence-loss: loss accepted, delete proceeds, --yes taken before the release"
else failt "16. exit 3 + accept: exit $code, delete-stack=$(grep -c delete-stack "$AWS"), out=[$(flat "$OUT")]"; fi

exit $rc
