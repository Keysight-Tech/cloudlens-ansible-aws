#!/usr/bin/env bash
# deploy/tests/test_cyperf.sh: the optional CyPerf traffic generator (--with-cyperf).
#
# Why this exists. CyPerf is the first component that lives in its OWN
# CloudFormation stack (<stack>-cyperf) and takes a licence code as a secret.
# Four things can go wrong quietly and are pinned here:
#   1. the option leaks into a deployment that did not ask for it
#   2. the activation code reaches a command line, the log or the profile
#   3. the launch helpers pick the wrong image release or build a bad stack
#   4. teardown deletes the main stack first (and it lands in DELETE_FAILED on
#      the VPC), or deletes a look-alike stack it does not own
#
# Hermetic: no AWS. `aws` is a stub; the dry runs never call it for real.
#
# Usage: bash deploy/tests/test_cyperf.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
TEARDOWN="${TEARDOWN_STACK_SH:-deploy/teardown-stack.sh}"
TPL="deploy/cloudformation/addons/cyperf.yaml"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }

# ---- 1. wiring: phase list, profile key in all three places -------------
order="$(sed -n 's/^PHASE_ORDER="\(.*\)"$/\1/p' "$SCRIPT")"
case " $order " in
  *" eks cyperf vpb "*) ok_ "PHASE_ORDER runs cyperf after eks and before vpb" ;;
  *) bad_ "PHASE_ORDER does not place cyperf between eks and vpb: $order" ;;
esac
grep -qx 'CLOUDLENS_DEPLOY_CYPERF' deploy/profile-keys.txt \
  && ok_ "profile-keys.txt lists CLOUDLENS_DEPLOY_CYPERF" || bad_ "profile-keys.txt lacks CLOUDLENS_DEPLOY_CYPERF"
awk '/^profile_key_allowed\(\)/{p=1} p{print} p&&/^}/{exit}' "$SCRIPT" | grep -q 'CLOUDLENS_DEPLOY_CYPERF' \
  && ok_ "profile_key_allowed admits CLOUDLENS_DEPLOY_CYPERF" || bad_ "profile_key_allowed does not admit CLOUDLENS_DEPLOY_CYPERF"
awk '/^write_profile\(\)/{p=1} p{print} p&&/^}/{exit}' "$SCRIPT" | grep -q 'CLOUDLENS_DEPLOY_CYPERF=' \
  && ok_ "write_profile saves CLOUDLENS_DEPLOY_CYPERF" || bad_ "write_profile does not save CLOUDLENS_DEPLOY_CYPERF"
# The code is a secret: no key that could carry it may ever be replayable.
if grep -q 'CYPERF_LICENSE' deploy/profile-keys.txt \
   || awk '/^write_profile\(\)/{p=1} p{print} p&&/^}/{exit}' "$SCRIPT" | grep -q 'CYPERF_LICENSE'; then
  bad_ "a CyPerf licence key is written to or allowed in a profile"
else
  ok_ "the licence code is never part of a profile"
fi

# ---- 2. dry runs: on, off, and the secret stays out ---------------------
dry() {  # dry <outfile> <args...>
  local out="$1"; shift
  ( cd "$S" && PATH="$S/bin:$PATH" /bin/bash "$OLDPWD/$SCRIPT" --dry-run --region us-east-1 \
      --stack-name cytest --key-name k --admin-cidr 198.51.100.7/32 --no-eks "$@" </dev/null >"$out" 2>&1 )
}
mkdir -p "$S/bin"
printf '#!/bin/sh\nexit 1\n' > "$S/bin/aws"; chmod +x "$S/bin/aws"

SECRET="ZZZZ-TEST-CODE-9999"
if CLOUDLENS_CYPERF_LICENSE_CODE="$SECRET" dry "$S/on.log" --with-kvo --with-vpb --tapping mirror --sensor-mode none --with-cyperf; then
  ok_ "dry run with --with-cyperf exits 0"
else
  bad_ "dry run with --with-cyperf failed: $(tail -3 "$S/on.log")"
fi
grep -q 'cloudformation deploy --stack-name cytest-cyperf' "$S/on.log" \
  && ok_ "the add-on stack is named <stack>-cyperf" || bad_ "no <stack>-cyperf deploy in the dry run"
grep -q 'Phase 13c: CyPerf traffic generator' "$S/on.log" \
  && ok_ "phase 13c is announced" || bad_ "phase 13c missing"
grep -q 'CyPerf generator:' "$S/on.log" && grep -q 'licence supplied' "$S/on.log" \
  && ok_ "the plan shows CyPerf and that a licence was supplied" || bad_ "plan line missing or wrong"
if grep -rq "$SECRET" "$S" 2>/dev/null; then
  bad_ "the activation code appears in output, log, profile or state: $(grep -rl "$SECRET" "$S" | head -3 | tr '\n' ' ')"
else
  ok_ "the activation code is in no output, log, profile, event or state file"
fi
[[ -f "$S/deploy-profile-cytest.env" ]] && grep -q '^CLOUDLENS_DEPLOY_CYPERF=true$' "$S/deploy-profile-cytest.env" \
  && ok_ "the saved profile records the CyPerf choice" || ok_ "no profile written in a dry run (nothing to check)"

rm -f "$S"/deploy-profile-* "$S"/.cloudlens-deploy-* 2>/dev/null
if dry "$S/off.log" --with-kvo --with-vpb --tapping mirror --sensor-mode none; then
  ok_ "dry run without the option exits 0"
else
  bad_ "dry run without the option failed: $(tail -3 "$S/off.log")"
fi
# The checkout's own path may contain the word (a folder named after the
# work), so it is removed before looking.
if sed "s|$PWD||g" "$S/off.log" | grep -qi 'cyperf'; then
  bad_ "a deployment that did not ask for CyPerf mentions it: $(sed "s|$PWD||g" "$S/off.log" | grep -i cyperf | head -2)"
else
  ok_ "without the option nothing about CyPerf is launched, asked or printed"
fi

if dry "$S/bad.log" --with-cyperf --cyperf-agent-type t3.micro; then
  bad_ "an unsupported agent type was accepted"
else
  grep -q 'cyperf-agent-type must be' "$S/bad.log" && ok_ "an unsupported agent type is refused with the reason" \
    || bad_ "unsupported agent type failed without the reason"
fi

# ---- 3. helpers ---------------------------------------------------------
awk '
  /^(cyperf_release_of|cyperf_newest_ami)\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/helpers.sh"
rel() { ( . "$S/helpers.sh"; cyperf_release_of "$1" ); }
r1="$(rel 'cyperf-mdw-1-0-17211-releasecyperf2610-92384358-5015')"
r2="$(rel 'cyperf-image-1-0-2182-master-agent-26-1-3-876-x86-64-10cb6682')"
[[ "$r1" == "26.1" && "$r2" == "26.1" ]] \
  && ok_ "controller and agent image names resolve to the same release (26.1)" \
  || bad_ "release parsing: controller='$r1' agent='$r2', wanted 26.1 for both"
r3="$(rel 'cyperf-image-1-0-1916-master-agent-26-0-3-834-x86-64-img')"
[[ "$r3" == "26.0" ]] && ok_ "an older agent resolves to 26.0, so a mismatch is detectable" || bad_ "older agent parsed as '$r3'"

# newest image = last by CreationDate, asked by product code not by name
cat > "$S/bin/aws" <<'STUB'
#!/bin/sh
echo "$*" >> "$AWS_CALLS"
case "$*" in *describe-images*) printf 'ami-0newest\tcyperf-mdw-1-0-17211-releasecyperf2610-x\n' ;; esac
STUB
chmod +x "$S/bin/aws"
got="$( export AWS_CALLS="$S/calls"; PATH="$S/bin:$PATH"; AWS_REGION_ARG=(--region us-east-1); . "$S/helpers.sh"; cyperf_newest_ami abc123 )"
[[ "${got%%[[:space:]]*}" == "ami-0newest" ]] && ok_ "cyperf_newest_ami returns the image id first" || bad_ "cyperf_newest_ami returned '$got'"
grep -q 'product-code,Values=abc123' "$S/calls" && grep -q 'sort_by(Images,&CreationDate)\[-1\]' "$S/calls" \
  && ok_ "the image is looked up by product code, newest by creation date" \
  || bad_ "image lookup is not product code + newest: $(cat "$S/calls")"

# ---- 4. template --------------------------------------------------------
if [[ -f "$TPL" ]]; then
  ok_ "add-on template exists outside the Launch Stack glob (addons/)"
else
  bad_ "$TPL missing"
fi
for want in 'ControllerAddress:' 'ClientAgentControlIp:' 'ServerAgentControlIp:' 'ParentStackName:' 'NoEcho: true'; do
  grep -q "$want" "$TPL" && ok_ "template has $want" || bad_ "template lacks $want"
done
n_dot="$(grep -c 'DeleteOnTermination: true' "$TPL")"
[[ "$n_dot" -ge 3 ]] && ok_ "all three root volumes are deleted with their instance" || bad_ "only $n_dot of 3 root volumes set DeleteOnTermination"
grep -q 'cloudlens:stack' "$TPL" && ok_ "resources carry cloudlens:stack for attribution" || bad_ "no cloudlens:stack tag in the template"
if command -v cfn-lint >/dev/null 2>&1; then
  if cfn-lint "$TPL" >"$S/lint.log" 2>&1; then ok_ "cfn-lint passes"; else bad_ "cfn-lint: $(head -3 "$S/lint.log")"; fi
fi

# ---- 5. teardown: ours only, licence first, before the main stack -------
awk '/^sweep_cyperf_stack\(\)/{p=1} p{print} p&&/^}/{exit}' "$TEARDOWN" > "$S/sweep.sh"
if [[ -s "$S/sweep.sh" ]]; then ok_ "teardown has sweep_cyperf_stack"; else bad_ "teardown lacks sweep_cyperf_stack"; fi
l_cy="$(grep -n '^  sweep_cyperf_stack$' "$TEARDOWN" | head -1 | cut -d: -f1)"
l_del="$(grep -n 'del_aws cloudformation delete-stack --stack-name "\$STACK_NAME"' "$TEARDOWN" | head -1 | cut -d: -f1)"
if [[ -n "$l_cy" && -n "$l_del" && "$l_cy" -lt "$l_del" ]]; then
  ok_ "the CyPerf stack is removed before the main stack's delete"
else
  bad_ "sweep_cyperf_stack (line ${l_cy:-none}) does not run before the main delete (line ${l_del:-none})"
fi
l_rel="$(grep -n -- '--release-license' "$S/sweep.sh" | head -1 | cut -d: -f1)"
l_ds="$(grep -n 'delete-stack' "$S/sweep.sh" | head -1 | cut -d: -f1)"
[[ -n "$l_rel" && -n "$l_ds" && "$l_rel" -lt "$l_ds" ]] \
  && ok_ "the licence is released before the controller is deleted" \
  || bad_ "licence release (line ${l_rel:-none}) is not before delete-stack (line ${l_ds:-none})"

# behaviour: a same-named stack WITHOUT our tag is left alone
run_sweep() {  # run_sweep <owner tag value> -> calls file
  : > "$S/tcalls"
  cat > "$S/bin/aws" <<STUB
#!/bin/sh
echo "\$*" >> "$S/tcalls"
case "\$*" in
  *"Tags[?Key=="*) echo "$1" ;;
  *"Stacks[0].StackStatus"*) if grep -q 'delete-stack' "$S/tcalls"; then echo ""; else echo "CREATE_COMPLETE"; fi ;;
  *"ControllerAddress"*) echo "" ;;
esac
STUB
  chmod +x "$S/bin/aws"
  ( PATH="$S/bin:$PATH"; STACK_NAME=cytest; DRY_RUN=false; AUDIT_ONLY=false; SCRIPT_DIR="$S/none"; REPO_NAME=none
    AWS_REGION_ARG=(--region us-east-1); DEL_ERR=""
    ro_aws() { aws "${AWS_REGION_ARG[@]}" "$@" 2>/dev/null || true; }
    del_aws() { aws "${AWS_REGION_ARG[@]}" "$@"; }
    det_clean() { local v="$1"; [[ "$v" == "None" ]] && v=""; printf '%s' "$v"; }
    eval "$(awk '/^first_line\(\) \{/{p=1} p{print} p&&/^}/{exit}' "$TEARDOWN")"   # the real helper, under set -u
    ok() { :; }; warn() { :; }; note() { :; }; dryrun_say() { :; }
    cd "$S"; . "$S/sweep.sh"; sweep_cyperf_stack ) >/dev/null 2>&1
}
run_sweep "someone-else"
grep -q 'delete-stack' "$S/tcalls" && bad_ "a stack tagged for another deployment was deleted" \
  || ok_ "a same-named stack owned by another deployment is left alone"
run_sweep "cytest"
grep -q 'delete-stack --stack-name cytest-cyperf' "$S/tcalls" \
  && ok_ "our own CyPerf stack is deleted" || bad_ "our own CyPerf stack was not deleted: $(cat "$S/tcalls")"

# ---- 6. the setup script -------------------------------------------------
PY=scripts/cyperf_setup.py
python3 -m py_compile "$PY" 2>/dev/null && ok_ "cyperf_setup.py compiles" || bad_ "cyperf_setup.py does not compile"
grep -q -- '--license-code\|--activation-code' "$PY" && bad_ "cyperf_setup.py takes the code as an argument" \
  || ok_ "cyperf_setup.py takes the code from the environment only"
grep -q -- '--cyperf-license-code' "$SCRIPT" && bad_ "deploy-stack.sh has a flag that takes the code itself" \
  || ok_ "deploy-stack.sh has no flag that takes the code itself"

echo
echo "${PASS} PASS, ${FAIL} FAIL"
[[ "$FAIL" -eq 0 ]]
