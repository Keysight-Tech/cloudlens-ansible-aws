#!/usr/bin/env bash
# Delete a CloudLens stack COMPLETELY, including everything CloudFormation does
# not own.
#
# Deleting the CFN stack alone does not work, and the ways it fails are not
# obvious:
#
#   * The collector SVMs are launched by KVO into an AUTO SCALING GROUP, not by
#     CloudFormation. Terminating the instances just makes the ASG build new
#     ones. The ASG has to go first, with --force-delete.
#   * Those collectors, the capture host and the traffic mirror targets all hold
#     ENIs inside the stack's VPC. CloudFormation cannot delete a VPC with
#     foreign ENIs in it, so the stack sits in DELETE_FAILED.
#   * scripts/deploy-test-workload-vms.sh creates its security group INSIDE the
#     stack's VPC. That single group is what actually deadlocked a delete once,
#     not the internet gateway everyone suspects first.
#   * Root volumes survive as "available" and are billed forever. A previous
#     lab accumulated 43 volumes, 6,348 GB, about $635/month, far more than the
#     running compute anyone was watching.
#   * KVO licences are bound to the KVO HOST. Destroy the instance without
#     deactivating and the entitlement is STRANDED: not returned, not
#     reactivatable elsewhere without Keysight rehosting it. Older lab codes
#     were lost exactly this way.
#
# Order is not cosmetic. Sessions reference targets and filters, ASGs rebuild
# instances, and ENIs block the VPC, so this goes:
#
#   licences -> mirror sessions -> targets -> filters -> ASGs -> stray
#   instances -> wait for ENIs -> CFN stack -> orphaned volumes + SGs
#
# SAFE BY DEFAULT: prints what it would delete and changes nothing. Pass --yes
# to actually delete.
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME=""; VPC_ID=""; KVO_IP=""; ASSUME_YES=false; KEEP_LICENCES=false

say()  { echo "$*"; }
ok()   { echo "  [ok] $*"; }
warn() { echo "  [warn] $*" >&2; }
die()  { echo "[fail] $*" >&2; exit 1; }
run()  { if $ASSUME_YES; then "$@"; else echo "    would run: $*"; fi; }

usage() {
  cat <<'EOF'
Usage: teardown.sh --stack-name NAME [--vpc-id vpc-xxx] [--region us-east-1]
                   [--kvo-ip IP] [--keep-licences] [--yes]

  --stack-name    CloudFormation stack to delete (required)
  --vpc-id        VPC to sweep. Resolved from the stack when omitted.
  --kvo-ip        KVO to deactivate licences on before anything is destroyed.
                  Strongly recommended: see the header.
  --keep-licences Skip deactivation and knowingly strand the entitlement.
  --yes           Actually delete. Without it, nothing is changed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --vpc-id) VPC_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --kvo-ip) KVO_IP="$2"; shift 2 ;;
    --keep-licences) KEEP_LICENCES=true; shift ;;
    --yes) ASSUME_YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -z "$STACK_NAME" && -z "$VPC_ID" ]] && { usage; exit 1; }

AWSQ=(aws --region "$REGION")
"${AWSQ[@]}" sts get-caller-identity >/dev/null 2>&1 \
  || die "AWS credentials are not valid for ${REGION}. Refresh them and re-run."

$ASSUME_YES || say "DRY RUN. Nothing will be deleted. Add --yes to act."
say ""

# Resolve the VPC from the stack when not given, so nothing is guessed.
if [[ -z "$VPC_ID" && -n "$STACK_NAME" ]]; then
  VPC_ID="$("${AWSQ[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='SharedVpcId'].OutputValue|[0]" --output text 2>/dev/null)"
  # The stack is very often ALREADY deleted, leaving the KVO-launched collectors
  # behind. That is the whole reason this script exists, so treat it as normal
  # and fall back to finding the VPC by its leftovers.
  if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
    say "stack ${STACK_NAME} no longer exists; looking for leftovers by VPC instead"
    VPC_ID="$("${AWSQ[@]}" ec2 describe-instances \
      --filters "Name=tag:Name,Values=*collector*" "Name=instance-state-name,Values=running,stopped" \
      --query 'Reservations[0].Instances[0].VpcId' --output text 2>/dev/null)"
  fi
fi
[[ -z "$VPC_ID" || "$VPC_ID" == "None" ]] && die "could not resolve the VPC for ${STACK_NAME}; pass --vpc-id"
say "stack ${STACK_NAME}   vpc ${VPC_ID}   region ${REGION}"
say ""

# --- 0. Licences FIRST. This is the only irreversible loss here. -------------
say "0. KVO licences"
if $KEEP_LICENCES; then
  warn "--keep-licences given: whatever is activated on this KVO will be STRANDED."
elif [[ -z "$KVO_IP" ]]; then
  warn "No --kvo-ip given, so licences cannot be deactivated."
  warn "If this stack has a licensed KVO, its entitlement will be LOST and needs"
  warn "a Keysight rehost to recover. Re-run with --kvo-ip, or accept the loss."
else
  CODES="$(curl -sk --max-time 20 "https://${KVO_IP}/api/v2/licensing/licenses" \
    -H "Authorization: Bearer $(curl -sk --max-time 20 \
      "https://${KVO_IP}/auth/realms/keysight/protocol/openid-connect/token" \
      -d grant_type=password -d client_id=vision-orchestrator \
      -d username=admin -d password=admin 2>/dev/null \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)" \
    2>/dev/null | python3 -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
rows = d if isinstance(d,list) else d.get("data",[])
print(" ".join(r.get("activationCode","") for r in rows if r.get("activationCode")))' 2>/dev/null)"
  if [[ -z "$CODES" ]]; then
    ok "no licences to release (or KVO unreachable)"
  else
    for C in $CODES; do
      say "    deactivating ${C}"
      # The body MUST be a list; an object returns HTTP 500. And do NOT trust the
      # reported status: every deactivate reports ERROR while actually
      # succeeding. Verify against the pool afterwards.
      $ASSUME_YES && curl -sk --max-time 30 -X POST \
        "https://${KVO_IP}/api/v2/licensing/operations/deactivate" \
        -H 'Content-Type: application/json' -d "[{\"activationCode\":\"${C}\"}]" >/dev/null 2>&1
    done
    warn "verify available == total for each code before trusting this:"
    warn "  the deactivate call reports ERROR even when it worked"
  fi
fi
say ""

# --- 1. Mirror sessions, then targets, then filters -------------------------
# Scoped to THIS VPC. describe-traffic-mirror-* is region-wide and sessions carry
# no VpcId, so an unscoped sweep happily deletes a sibling stack's tap.
say "1. Traffic mirroring (scoped to ${VPC_ID})"
ENI_FILE="$(mktemp)"; trap 'rm -f "$ENI_FILE"' EXIT
"${AWSQ[@]}" ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null \
  | tr '\t' '\n' | sed '/^$/d' | sort -u > "$ENI_FILE"

SESSIONS="$("${AWSQ[@]}" ec2 describe-traffic-mirror-sessions \
  --query 'TrafficMirrorSessions[].[TrafficMirrorSessionId,NetworkInterfaceId]' --output text 2>/dev/null \
  | awk 'NR==FNR{e[$1];next} ($2 in e){print $1}' "$ENI_FILE" -)"
for S in $SESSIONS; do run "${AWSQ[@]}" ec2 delete-traffic-mirror-session --traffic-mirror-session-id "$S"; done
[[ -z "$SESSIONS" ]] && ok "no mirror sessions in this VPC"

TARGETS="$("${AWSQ[@]}" ec2 describe-traffic-mirror-targets \
  --query 'TrafficMirrorTargets[].[TrafficMirrorTargetId,NetworkInterfaceId]' --output text 2>/dev/null \
  | awk 'NR==FNR{e[$1];next} ($2 in e){print $1}' "$ENI_FILE" -)"
for T in $TARGETS; do run "${AWSQ[@]}" ec2 delete-traffic-mirror-target --traffic-mirror-target-id "$T"; done

# ORPHANED targets and filters: once the instances are gone so are their ENIs,
# and a target whose ENI no longer resolves can never be matched to a VPC again.
# Measured: a first pass left 4 targets and 2 filters behind for exactly this
# reason, invisible to any VPC-scoped query. A target pointing at a
# non-existent interface belongs to nobody, so removing it is safe.
LIVE_ENI_FILE="$(mktemp)"
"${AWSQ[@]}" ec2 describe-network-interfaces --query 'NetworkInterfaces[].NetworkInterfaceId' \
  --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d' | sort -u > "$LIVE_ENI_FILE"
ORPHANED="$("${AWSQ[@]}" ec2 describe-traffic-mirror-targets \
  --query 'TrafficMirrorTargets[].[TrafficMirrorTargetId,NetworkInterfaceId]' --output text 2>/dev/null \
  | awk 'NR==FNR{e[$1];next} !($2 in e){print $1}' "$LIVE_ENI_FILE" -)"
for T in $ORPHANED; do
  say "    orphaned target ${T} (its interface no longer exists)"
  run "${AWSQ[@]}" ec2 delete-traffic-mirror-target --traffic-mirror-target-id "$T"
done
rm -f "$LIVE_ENI_FILE"

# Filters are referenced by sessions, so once every session is gone any filter
# still carrying our naming is dead weight. Only touch ones with no session.
USED_FILTERS="$("${AWSQ[@]}" ec2 describe-traffic-mirror-sessions \
  --query 'TrafficMirrorSessions[].TrafficMirrorFilterId' --output text 2>/dev/null | tr '\t' '\n' | sort -u)"
for F in $("${AWSQ[@]}" ec2 describe-traffic-mirror-filters \
             --query 'TrafficMirrorFilters[].TrafficMirrorFilterId' --output text 2>/dev/null | tr '\t' '\n'); do
  if ! printf '%s\n' "$USED_FILTERS" | grep -qx "$F"; then
    say "    unused filter ${F}"
    run "${AWSQ[@]}" ec2 delete-traffic-mirror-filter --traffic-mirror-filter-id "$F"
  fi
done

# --- 2. ASGs BEFORE instances, or the ASG rebuilds what you terminate --------
say "2. Collector auto scaling groups"
ASGS="$("${AWSQ[@]}" autoscaling describe-auto-scaling-groups \
  --query "AutoScalingGroups[?contains(AutoScalingGroupName,'${VPC_ID}')].AutoScalingGroupName" \
  --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d')"
for A in $ASGS; do
  say "    ${A}"
  run "${AWSQ[@]}" autoscaling delete-auto-scaling-group --auto-scaling-group-name "$A" --force-delete
done
[[ -z "$ASGS" ]] && ok "no collector ASGs"

# --- 3. Anything left in the VPC that CloudFormation does not own ------------
say "3. Instances in the VPC not owned by the stack"
STRAY="$("${AWSQ[@]}" ec2 describe-instances \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=instance-state-name,Values=running,stopped,pending" \
  --query 'Reservations[].Instances[?!not_null(Tags[?Key==`aws:cloudformation:stack-name`].Value)].InstanceId' \
  --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d')"
if [[ -n "$STRAY" ]]; then
  for I in $STRAY; do say "    ${I}"; done
  run "${AWSQ[@]}" ec2 terminate-instances --instance-ids $STRAY
  $ASSUME_YES && "${AWSQ[@]}" ec2 wait instance-terminated --instance-ids $STRAY 2>/dev/null
else
  ok "none"
fi

# --- 4. The stack itself ----------------------------------------------------
say "4. CloudFormation stack"
if [[ -z "$STACK_NAME" ]] || ! "${AWSQ[@]}" cloudformation describe-stacks \
     --stack-name "$STACK_NAME" >/dev/null 2>&1; then
  ok "no such stack (already deleted, or none given): nothing to do here"
else
run "${AWSQ[@]}" cloudformation delete-stack --stack-name "$STACK_NAME"
if $ASSUME_YES; then
  say "    waiting for delete to complete (this is where DELETE_FAILED shows up)"
  if ! "${AWSQ[@]}" cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" 2>/dev/null; then
    warn "stack did not delete cleanly. The usual cause is a leftover ENI or a"
    warn "security group created inside the VPC by something other than CFN:"
    warn "  aws ec2 describe-network-interfaces --region ${REGION} --filters Name=vpc-id,Values=${VPC_ID}"
    warn "  aws ec2 describe-security-groups --region ${REGION} --filters Name=vpc-id,Values=${VPC_ID}"
  fi
fi
fi

# --- 5. The bill nobody watches --------------------------------------------
say "5. Orphaned volumes"
VOLS="$("${AWSQ[@]}" ec2 describe-volumes --filters "Name=status,Values=available" \
  --query 'Volumes[].VolumeId' --output text 2>/dev/null | tr '\t' '\n' | sed '/^$/d')"
if [[ -n "$VOLS" ]]; then
  GB="$("${AWSQ[@]}" ec2 describe-volumes --filters "Name=status,Values=available" \
        --query 'sum(Volumes[].Size)' --output text 2>/dev/null)"
  warn "$(echo "$VOLS" | wc -l | tr -d ' ') unattached volume(s), ${GB} GB total."
  warn "These are REGION-WIDE, not just this stack, so review before deleting:"
  for V in $VOLS; do say "    ${V}"; done
  say "    delete with: aws ec2 delete-volume --region ${REGION} --volume-id <id>"
else
  ok "none"
fi

say ""
$ASSUME_YES && say "Teardown finished." || say "DRY RUN complete. Re-run with --yes to delete."
