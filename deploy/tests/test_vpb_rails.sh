#!/usr/bin/env bash
# deploy/tests/test_vpb_rails.sh: one vPB per area (--vpb-rails).
#
# Why this exists. KVO allows one cloud config per Cloud to Device Link and a
# vPB ingress port carries one link, so mirrored VMs (AWS cloud config) and
# Kubernetes pods (K8s cloud config) cannot share a vPB. compute_vpb_rails
# turns the answers into "the stack vPB serves the first area, one more vPB
# per further area"; ensure_extra_vpbs_now launches those vPBs the way AWS
# accepts (three interfaces, no AssociatePublicIpAddress, then an Elastic IP)
# and reuses one that exists. Proven live 2026-09-28 (docs/KUBERNETES_RAIL.md).
#
# Hermetic: `aws` is a stub that records its calls.
#
# Usage: bash deploy/tests/test_vpb_rails.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

awk '
  /^(ok|warn|fail|step|note|dryrun_say|upper)\(\)/ { print; next }
  /^(compute_vpb_rails|vpb_rail_instance_id|ensure_extra_vpbs_now|vpb_rail_facts)\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/helpers.sh"

PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }
for f in compute_vpb_rails vpb_rail_instance_id ensure_extra_vpbs_now vpb_rail_facts; do
  grep -q "^${f}()" "$S/helpers.sh" || { echo "FAIL ${f} missing from ${SCRIPT}"; echo; echo "0 PASS, 1 FAIL"; exit 1; }
done

# rails "<vpb>" "<mirror>" "<eks>" "<override>" -> "first|extra|rails"
rails() {
  ( set -u
    DEPLOY_VPB="$1"; WITH_MIRROR="$2"; DEPLOY_EKS="$3"; VPB_RAILS="$4"
    . "$S/helpers.sh"
    # stubs AFTER the source: the real ok/warn/fail print with colour variables
    fail() { echo "FAIL:$*"; exit 3; }
    ok() { :; }; warn() { :; }; note() { :; }
    compute_vpb_rails
    printf '%s|%s|%s' "$VPB_RAIL_FIRST" "$VPB_RAILS_EXTRA" "$VPB_RAILS"
  )
}
[[ "$(rails true true true "")" == "mirror|k8s|mirror,k8s" ]] && ok_ "mirror + EKS: stack vPB serves the mirror, one more vPB for k8s" || bad_ "mirror + EKS: $(rails true true true "")"
[[ "$(rails true false true "")" == "k8s||k8s" ]] && ok_ "EKS only: the stack vPB serves the pods, no extra vPB" || bad_ "EKS only: $(rails true false true "")"
[[ "$(rails true true false "")" == "mirror||mirror" ]] && ok_ "mirror only: no extra vPB" || bad_ "mirror only: $(rails true true false "")"
[[ "$(rails false true true "")" == "||" ]] && ok_ "no vPB: no areas at all" || bad_ "no vPB: $(rails false true true "")"
[[ "$(rails true true true "k8s,mirror")" == "k8s|mirror|k8s,mirror" ]] && ok_ "override order respected: k8s first on the stack vPB" || bad_ "override: $(rails true true true "k8s,mirror")"
[[ "$(rails true true true "mirror")" == "mirror||mirror" ]] && ok_ "override can opt the pods out of a vPB" || bad_ "opt-out: $(rails true true true "mirror")"
[[ "$(rails true true true "mirror,mirror,k8s")" == "mirror|k8s|mirror,k8s" ]] && ok_ "duplicates collapse" || bad_ "duplicates: $(rails true true true "mirror,mirror,k8s")"
out="$(rails true true true "vmware" 2>&1)"; [[ "$out" == FAIL:* ]] && ok_ "unknown area is refused" || bad_ "unknown area accepted: $out"

# The launch: a stubbed aws records every call.
launch() {
  # $1: existing instance id the describe-instances stub answers ("" = none)
  : > "$S/calls"
  ( set -u
    DEPLOY_VPB=true; VPB_RAILS_EXTRA="k8s"; DRY_RUN=false; STACK_NAME=t; REGION=us-east-1
    AWS_REGION_ARG=(--region us-east-1); MGMT_SUBNET_ID=subnet-m; INGRESS_SUBNET_ID=subnet-i; EGRESS_SUBNET_ID=subnet-e
    VPB_AMI=ami-1; VPB_TYPE=t3.xlarge; KEY_NAME=k; REPO_RAW=https://example.invalid/raw; VPB_NAME=""
    STATE_FILE="$S/state"; : > "$STATE_FILE"
    existing="$1"
    probe() { "$@"; }
    ec2_fact() { echo "sg-stack"; }
    state_set() { printf '%s=%s\n' "$1" "$2" >> "$STATE_FILE"; }
    aws() {
      local line="$*"
      printf '%s\n' "${line//$'\n'/ }" >> "$S/calls"
      case "$*" in
        *describe-instances*tag:cloudlens:vpb-rail*) echo "${existing:-None}" ;;
        *run-instances*) echo "i-new" ;;
        *describe-instances*NetworkInterfaces*) echo "eni-0" ;;
        *allocate-address*) echo "eipalloc-1" ;;
        *) : ;;
      esac
    }
    . "$S/helpers.sh"
    # stubs AFTER the source: the real ok/warn/note print with colour variables
    ok() { :; }; warn() { echo "WARN $*" >&2; }; note() { :; }
    upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
    ensure_extra_vpbs_now
  )
}
launch ""
if grep -q "run-instances" "$S/calls"; then ok_ "no vPB for the area yet: one is launched" ; else bad_ "no launch recorded: $(cat "$S/calls")"; fi
run_line=$(grep "run-instances" "$S/calls")
grep -q "AssociatePublicIpAddress" <<<"$run_line" && bad_ "run-instances must not ask for a public IP with several interfaces" || ok_ "launch has no AssociatePublicIpAddress (AWS refuses it with several interfaces)"
[[ $(grep -o '"DeviceIndex":[0-2]' <<<"$run_line" | sort -u | wc -l | tr -d ' ') == "3" ]] && ok_ "three interfaces on the stack vPB subnets" || bad_ "interfaces wrong: $run_line"
grep -q 'Key=cloudlens:vpb-rail,Value=k8s' <<<"$run_line" && grep -q 'Key=cloudlens:stack,Value=t' <<<"$run_line" && ok_ "tagged by stack and area (what adoption, wiring and teardown key on)" || bad_ "tags missing: $run_line"
grep -q "allocate-address" "$S/calls" && grep -q "associate-address --allocation-id eipalloc-1 --network-interface-id eni-0" "$S/calls" && ok_ "Elastic IP allocated (tagged) and attached to the management interface" || bad_ "EIP calls wrong: $(cat "$S/calls")"
grep -q "VPB_RAIL_K8S_ID=i-new" "$S/state" && ok_ "instance id recorded in the state ledger" || bad_ "state not written: $(cat "$S/state")"
launch "i-old"
if grep -q "run-instances" "$S/calls"; then bad_ "an existing area vPB was launched again"; else ok_ "an existing area vPB is reused, not relaunched"; fi
grep -q "VPB_RAIL_K8S_ID=i-old" "$S/state" && ok_ "reused instance id recorded" || bad_ "reused id not recorded"

echo; echo "$PASS PASS, $FAIL FAIL"
[[ $FAIL -eq 0 ]]
