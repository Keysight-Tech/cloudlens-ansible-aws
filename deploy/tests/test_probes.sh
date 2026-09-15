#!/usr/bin/env bash
# deploy/tests/test_probes.sh: a refused read-only probe warns and continues,
# a refused required call stops the run with a message that says what could
# not be done, and in both cases the AWS error text lands in the deploy log
# instead of /dev/null.
#
# Why this exists. The script runs under `set -euo pipefail`, and an assignment
# x=$(aws ...) carries the command's exit status. When AWS refuses a call the
# CLI exits 254, the assignment fails, and errexit ends the run on that line,
# before the guard written for exactly that case on the next line can run. The
# ERR trap does not reach into functions, so nothing is printed. A customer
# whose role lacked servicequotas:GetServiceQuota lost a day to that: the
# banner in force said "Marketplace", and every subscription was fine.
#
# Hermetic: no AWS account, no network. `aws` is a shell function per case
# that answers some calls and refuses others with the CLI's real exit code and
# message shape. Every case runs under /bin/bash (3.2 on macOS, the floor the
# script targets) with the script's own shell options in force.
#
# Only the functions under test are exercised, lifted out by awk to a file.
# That relies on them being written name() { ... } with both braces at column
# 0 (one-line helpers are taken whole). macOS awk keeps only the last of
# several range patterns and bash 3.2 sources nothing from <(...), hence the
# flag and the temp file.
#
# Usage: bash deploy/tests/test_probes.sh
#        DEPLOY_STACK_SH=/path/to/deploy-stack.sh bash deploy/tests/test_probes.sh
# The second form points the suite at another copy of the script. That is how
# a fix is proven: the suite has to FAIL against the version before it.
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT
awk '
  /^(ok|warn|fail|step|note|dryrun_say|to_lower)\(\)/ { print; next }
  /^(check_eip_headroom|ami_subscribe_url|report_marketplace_failure|vpc_mirror_session_count|ensure_key_pair|on_exit)\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/helpers.sh"

# The script's own conditions: errexit, nounset, pipefail, colours off, the
# region argument populated, a full stack (vController + KVO + vPB) wanted with
# public IPs, and a log file and HOME of our own to inspect afterwards.
# refuse CODE OPERATION MESSAGE plays AWS saying no, in the CLI's own shape
# (it leads its error output with an empty line) and with its exit code, 254.
harness='
  set -euo pipefail
  C_GREEN= C_YELLOW= C_BLUE= C_RED= C_GREY= C_BOLD= C_RESET=
  SCRIPT_DONE=false PHASE_NAME="Phase 4b: Elastic IP headroom" INTERACTIVE=false
  REGION=us-east-1
  AWS_REGION_ARG=(--region us-east-1)
  DRY_RUN=false ASSIGN_PUBLIC_IP=yes DEPLOY_KVO=true DEPLOY_VPB=true
  STACK_NAME=probe-test STACK_VPC_ID=vpc-0probe MARKETPLACE_OWNER=679593333241
  REPO_RAW=https://example.invalid STATE_FILE=""
  state_set() { :; }
  refuse() { printf "\nAn error occurred (%s) when calling the %s operation: %s\n" "$1" "$2" "$3" >&2; return 254; }
  LOG_FILE="$1"; HOME="$2"
  source "$3"
'
rc=0
pass() { printf 'PASS %s\n' "$1"; }
failt() { printf 'FAIL %s\n' "$1"; rc=1; }
flat() { tr '\n' '|' < "$1"; }

# run NAME STDIN CODE: runs CODE in the harness. Leaves the exit status in
# $code, stdout in $OUT, stderr in $ERR, the deploy log in $LOG, HOME in
# $HOMEDIR. STDIN feeds any prompt the code reaches.
run() {
  LOG="$S/$1.log"; HOMEDIR="$S/$1.home"; OUT="$S/$1.out"; ERR="$S/$1.err"
  : > "$LOG"; mkdir -p "$HOMEDIR"
  printf '%s' "$2" | /bin/bash -c "$harness$3" _ "$LOG" "$HOMEDIR" "$S/helpers.sh" >"$OUT" 2>"$ERR"
  code=$?
}

# a. The customer's case: describe-addresses allowed, GetServiceQuota refused,
#    and EC2's own describe-account-attributes allowed (it needs only an EC2
#    permission and reports the same number). The check must read the quota
#    from there, say where it came from, and let the run continue; the
#    AccessDenied text goes to the log, not the terminal.
run a '' '
  aws() { case "$*" in
    *service-quotas*) refuse AccessDeniedException GetServiceQuota "User: arn:aws:sts::123456789012:assumed-role/ISG_Sales_Engineers/x is not authorized to perform: servicequotas:GetServiceQuota" ;;
    *describe-account-attributes*) echo 20 ;;
    *describe-addresses*) echo 2 ;;
    *) echo "unexpected: aws $*" >&2; return 1 ;;
  esac; }
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^CONTINUED$' "$OUT"; then
  pass "a. quota refused, EC2 attribute allowed: check_eip_headroom returns 0 and the run continues"
else failt "a. quota refused: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")]"; fi
if grep -q '^\[ok\] Elastic IPs: 2/20 in use, need 3, 18 free (quota read from EC2; the Service Quotas API was refused)$' "$OUT" \
   && ! grep -q 'assuming the AWS default' "$OUT"; then
  pass "a. quota refused: the real quota is read from EC2 and the line says so"
else failt "a. quota refused: terminal was [$(flat "$OUT")]"; fi
if grep -q 'servicequotas:GetServiceQuota' "$LOG" && ! grep -q 'AccessDeniedException' "$OUT"; then
  pass "a. quota refused: the AccessDenied message is in the log and not on the terminal"
else failt "a. quota refused: log=[$(flat "$LOG")] terminal mentions AccessDenied: $(grep -c AccessDenied "$OUT")"; fi

# a2. Both quota sources refused and 20 addresses already allocated. An
#     unknown quota is not a shortage: the customer saw "-15 free" against an
#     assumed 5 and a question whose default answer aborts. Now: one warn with
#     what is known, one note saying the launch fails fast if the account is
#     really out, no shortage claim, no prompt, and the run continues.
run a2 '' '
  aws() { case "$*" in
    *service-quotas*) refuse AccessDeniedException GetServiceQuota "not authorized to perform: servicequotas:GetServiceQuota" ;;
    *describe-account-attributes*) refuse UnauthorizedOperation DescribeAccountAttributes "You are not authorized to perform this operation." ;;
    *describe-addresses*) echo 20 ;;
    *) echo "unexpected: aws $*" >&2; return 1 ;;
  esac; }
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^CONTINUED$' "$OUT"; then
  pass "a2. both quota sources refused: the run continues"
else failt "a2. both refused: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")]"; fi
if grep -q '^\[warn\] Could not read the Elastic IP quota (the AWS error is in .*). 20 in use, need 3\.$' "$OUT" \
   && grep -q 'AddressLimitExceeded' "$OUT" \
   && ! grep -q 'Not enough Elastic IPs' "$OUT" && ! grep -q 'Continue anyway' "$OUT" && ! grep -q -- '-15' "$OUT"; then
  pass "a2. both refused: says unknown with the count, never a shortage, never a negative, never a prompt"
else failt "a2. both refused: terminal was [$(flat "$OUT")]"; fi
if grep -q 'DescribeAccountAttributes operation' "$LOG" && grep -q 'GetServiceQuota operation' "$LOG"; then
  pass "a2. both refused: both refusals are in the log"
else failt "a2. both refused: log=[$(flat "$LOG")]"; fi

# a3. Both refused on an empty account: same, with 0 in use.
run a3 '' '
  aws() { case "$*" in
    *service-quotas*|*describe-account-attributes*) refuse AccessDeniedException X "no" ;;
    *describe-addresses*) echo 0 ;;
    *) echo "unexpected: aws $*" >&2; return 1 ;;
  esac; }
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^CONTINUED$' "$OUT" && grep -q '0 in use, need 3\.' "$OUT" && ! grep -q 'Continue anyway' "$OUT"; then
  pass "a3. both refused, empty account: continues without a prompt"
else failt "a3. both refused, empty account: exit $code, out=[$(flat "$OUT")]"; fi

# b. describe-addresses itself refused: the check is skipped with a warn that
#    names the log, and the run continues.
run b '' '
  aws() { case "$*" in
    *describe-addresses*) refuse UnauthorizedOperation DescribeAddresses "You are not authorized to perform this operation." ;;
    *service-quotas*) echo 5.0 ;;
  esac; }
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^CONTINUED$' "$OUT" \
   && grep -q '^\[warn\] Could not read Elastic IP usage (the AWS error is in .*); skipping the quota check.$' "$OUT"; then
  pass "b. usage refused: skipped with the warn, the run continues"
else failt "b. usage refused: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")]"; fi
if grep -q 'DescribeAddresses operation' "$LOG"; then pass "b. usage refused: the reason is in the log"
else failt "b. usage refused: log is [$(flat "$LOG")]"; fi

# c. Everything permitted and the quota short: the orphan listing prints and
#    the "Continue anyway?" prompt is reached. read -p shows its prompt only on
#    a terminal, so the prompt path is proven by its outcomes: "n" aborts
#    through fail, "y" continues. Unchanged behaviour: this pair passes before
#    and after the fix.
quota_short='
  aws() { case "$*" in
    *"length(Addresses)"*) echo 5 ;;
    *AssociationId*) printf "3.3.3.3\teipalloc-0abc\told-stack\n" ;;
    *service-quotas*) echo 5.0 ;;
  esac; }'
run c 'n
' "$quota_short"'
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 1 ]] && grep -q '^\[warn\] Not enough Elastic IPs in us-east-1: need 3, only 0 free (5/5 in use).$' "$OUT" \
   && grep -q 'eipalloc-0abc' "$OUT" && grep -q '^\[x\] Aborted: free up Elastic IPs, then re-run.$' "$ERR" \
   && ! grep -q CONTINUED "$OUT"; then
  pass "c. quota short, all permitted: orphans listed, prompt reached, n aborts"
else failt "c. quota short: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")]"; fi
run c_yes 'y
' "$quota_short"'
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^CONTINUED$' "$OUT"; then
  pass "c. quota short, all permitted: y continues"
else failt "c. quota short, y: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")]"; fi

# c2. Quota short and the orphan listing refused: the third probe. It must
#     still reach the prompt (n aborts) and log why the listing is missing.
run c2 'n
' '
  aws() { case "$*" in
    *"length(Addresses)"*) echo 5 ;;
    *AssociationId*) refuse UnauthorizedOperation DescribeAddresses "You are not authorized to perform this operation." ;;
    *service-quotas*) echo 5.0 ;;
  esac; }
  check_eip_headroom
  echo CONTINUED'
if [[ $code -eq 1 ]] && grep -q '^\[x\] Aborted: free up Elastic IPs, then re-run.$' "$ERR" \
   && grep -q 'DescribeAddresses operation' "$LOG"; then
  pass "c2. quota short, orphan listing refused: still reaches the prompt, n aborts, reason logged"
else failt "c2. orphan listing refused: exit $code, err=[$(flat "$ERR")] log=[$(flat "$LOG")]"; fi

# d. A REQUIRED call: create-key-pair refused. Before the fix the run ended
#    with a bare exit 254 and an empty .pem left behind; now it stops through
#    fail, says what could not be created and why it matters, and leaves no
#    file for a later run to mistake for the private key.
run d '' '
  aws() { case "$*" in
    *describe-key-pairs*) refuse InvalidKeyPair.NotFound DescribeKeyPairs "The key pair k1 does not exist" ;;
    *create-key-pair*) refuse UnauthorizedOperation CreateKeyPair "You are not authorized to perform this operation." ;;
  esac; }
  ensure_key_pair k1
  echo CONTINUED'
if [[ $code -eq 1 ]] && grep -q "^\[x\] Could not create the EC2 key pair 'k1' in us-east-1" "$ERR" && ! grep -q CONTINUED "$OUT"; then
  pass "d. required create-key-pair refused: stops with fail and a message naming the key pair"
else failt "d. create-key-pair refused: exit $code, err=[$(flat "$ERR")]"; fi
if grep -q 'CreateKeyPair operation' "$ERR" && [[ ! -e "$HOMEDIR/.ssh/k1.pem" ]]; then
  pass "d. required create-key-pair refused: the AWS error is shown and no empty .pem is left behind"
else failt "d. create-key-pair refused: pem left=$([[ -e "$HOMEDIR/.ssh/k1.pem" ]] && echo yes || echo no), err=[$(flat "$ERR")]"; fi

# e. ami_subscribe_url is captured in $( ) by Phase 4. DescribeImages refused
#    must fall back to the Marketplace search page, note it on stderr (stdout
#    is the URL), and log the reason.
run e '' '
  aws() { refuse UnauthorizedOperation DescribeImages "You are not authorized to perform this operation."; }
  url="$(ami_subscribe_url ami-0123456789abcdef0)"
  echo "url=$url"
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^url=https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens$' "$OUT" \
   && grep -q 'Could not read the product code of ami-0123456789abcdef0' "$ERR" && grep -q 'DescribeImages operation' "$LOG"; then
  pass "e. ami_subscribe_url: DescribeImages refused falls back to the search page, says so, logs the reason"
else failt "e. ami_subscribe_url: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")] log=[$(flat "$LOG")]"; fi

# f. report_marketplace_failure, called plainly (not behind || true as its one
#    caller does): a refused DescribeStackEvents is "nothing to report" (1),
#    never a death (254), and the reason is logged.
run f '' '
  aws() { refuse AccessDenied DescribeStackEvents "User is not authorized to perform: cloudformation:DescribeStackEvents"; }
  report_marketplace_failure'
if [[ $code -eq 1 ]] && grep -q 'DescribeStackEvents operation' "$LOG" && ! grep -q 'Marketplace terms' "$OUT"; then
  pass "f. report_marketplace_failure: DescribeStackEvents refused answers nothing-to-report (1), reason logged"
else failt "f. report_marketplace_failure: exit $code (want 1), out=[$(flat "$OUT")] log=[$(flat "$LOG")]"; fi

# g. vpc_mirror_session_count is captured in $( ) by Phase 17. A refused
#    DescribeNetworkInterfaces counts as 0, warns on stderr, logs the reason.
run g '' '
  aws() { refuse UnauthorizedOperation DescribeNetworkInterfaces "You are not authorized to perform this operation."; }
  n="$(vpc_mirror_session_count)"
  echo "count=$n"
  echo CONTINUED'
if [[ $code -eq 0 ]] && grep -q '^count=0$' "$OUT" && grep -q '^\[warn\] Could not list the interfaces in vpc-0probe' "$ERR" \
   && grep -q 'DescribeNetworkInterfaces operation' "$LOG"; then
  pass "g. vpc_mirror_session_count: DescribeNetworkInterfaces refused counts 0, warns, logs the reason"
else failt "g. vpc_mirror_session_count: exit $code, out=[$(flat "$OUT")] err=[$(flat "$ERR")] log=[$(flat "$LOG")]"; fi

# h. The last line of defence explains 254 as what it is. Called the way the
#    EXIT trap calls it, with $? already set; other codes keep the old text.
run h '' 'set +e; (exit 254); on_exit'
if grep -q 'Exit 254 is the AWS CLI reporting that an AWS service refused a call' "$ERR" \
   && grep -q 'Stopped in phase: Phase 4b: Elastic IP headroom (exit 254)' "$ERR"; then
  pass "h. on_exit: exit 254 is explained as a refused AWS call, under the phase that ran"
else failt "h. on_exit 254: err=[$(flat "$ERR")]"; fi
run h1 '' 'set +e; (exit 1); on_exit'
if grep -q 'Nothing above explained this' "$ERR" && ! grep -q 'Exit 254' "$ERR"; then
  pass "h. on_exit: other codes keep the generic text"
else failt "h. on_exit 1: err=[$(flat "$ERR")]"; fi

# i. Attribution: the check runs under its own banner, placed after Phase 4
#    and before Phase 5, and the resume ledger (PHASE_ORDER) is untouched.
p4=$(grep -n '^step "Phase 4: Marketplace AMI subscriptions"' "$SCRIPT" | cut -d: -f1)
p4b=$(grep -n '^step "Phase 4b: Elastic IP headroom"' "$SCRIPT" | cut -d: -f1)
call=$(grep -n '^  check_eip_headroom$' "$SCRIPT" | cut -d: -f1)
p5=$(grep -n '^step "Phase 5: ' "$SCRIPT" | cut -d: -f1)
if [[ -n "$p4b" && -n "$p4" && -n "$call" && -n "$p5" ]] && (( p4 < p4b && p4b < call && call < p5 )); then
  pass "i. the Elastic IP check runs under its own Phase 4b banner, after Phase 4 and before Phase 5"
else failt "i. banner: phase4=${p4:-none} phase4b=${p4b:-none} call=${call:-none} phase5=${p5:-none}"; fi
if grep -q '^PHASE_ORDER="stack wait bootstrap key license adopt sensors eks vpb mirror path prove"$' "$SCRIPT"; then
  pass "i. PHASE_ORDER, the resume ledger, is unchanged"
else failt "i. PHASE_ORDER changed: $(grep -n '^PHASE_ORDER=' "$SCRIPT")"; fi

exit $rc
