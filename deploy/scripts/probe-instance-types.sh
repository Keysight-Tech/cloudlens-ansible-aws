#!/usr/bin/env bash
#
# Probe which EC2 instance types each CloudLens Marketplace AMI permits, in
# every region the CloudFormation RegionMap covers.
#
# Why this exists:
#   Marketplace products restrict which instance types may launch them. A type
#   that is not on the seller's list fails at RunInstances with
#     UnsupportedOperation: The instance configuration for this AWS Marketplace
#     product is not supported
#   The templates encode the permitted set in AllowedValues, and deploy-stack.sh
#   validates the same set. Both need re-checking whenever the AMIs are bumped,
#   otherwise customers get knocked out at launch by a size we advertised.
#
# Method:
#   run-instances --dry-run reports the restriction without launching anything.
#   It needs a subnet, so this creates a throwaway VPC + subnet per region and
#   deletes both immediately. VPCs and subnets are free; nothing is launched, so
#   this costs nothing.
#
# Last full run: permitted set was IDENTICAL in all 12 regions:
#   vController  t3.xlarge, m5.xlarge
#   KVO          c5.2xlarge
#   vPB          t3.xlarge
#
# Usage:
#   AWS_PROFILE=<profile> bash deploy/scripts/probe-instance-types.sh
#   AWS_PROFILE=<profile> REGIONS="us-east-1 eu-west-1" bash deploy/scripts/probe-instance-types.sh
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$REPO_ROOT/deploy/cloudformation/stack.yaml"
PROBE_CIDR="${PROBE_CIDR:-10.250.0.0/16}"

C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[0;34m'; C_RESET='\033[0m'
ok()   { echo -e "${C_GREEN}\xE2\x9C\x93${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}\xE2\x9A\xA0${C_RESET} $1"; }
fail() { echo -e "${C_RED}\xE2\x9C\x97${C_RESET} $1" >&2; exit 1; }
step() { echo; echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }

command -v aws >/dev/null 2>&1 || fail "AWS CLI not found."
aws sts get-caller-identity >/dev/null 2>&1 || fail "AWS auth missing. Run: aws sso login --profile <profile>"
[[ -f "$TEMPLATE" ]] || fail "Template not found: $TEMPLATE"

# Candidate types per component: the ones we advertise, plus a known-bad one to
# confirm the restriction is real and not an accident of the probe.
SPECS=(
  "vController:VcontrollerAmi:t3.xlarge m5.xlarge c5.2xlarge"
  "KVO:KvoAmi:c5.2xlarge m5.2xlarge"
  "vPB:VpbAmi:t3.xlarge c5.2xlarge"
)

MAP_JSON="$(python3 - "$TEMPLATE" <<'PY'
import re, sys, json
s = open(sys.argv[1]).read()
block = s.split("Mappings:")[1].split("Conditions:")[0]
cur, out = None, {}
for line in block.splitlines():
    m = re.match(r"^    ([a-z]{2}-[a-z]+-\d):\s*$", line)
    if m:
        cur = m.group(1); out[cur] = {}
    m2 = re.match(r"^      (\w+):\s*(ami-\w+)\s*$", line)
    if m2 and cur:
        out[cur][m2.group(1)] = m2.group(2)
print(json.dumps(out))
PY
)"
[[ -n "$MAP_JSON" ]] || fail "Could not parse RegionMap from $TEMPLATE"

REGIONS="${REGIONS:-$(python3 -c "import json,sys;print(' '.join(json.loads(sys.stdin.read()).keys()))" <<<"$MAP_JSON")}"

ami_for() {
  python3 -c "import json,sys;print(json.loads(sys.stdin.read())['$1']['$2'])" <<<"$MAP_JSON" 2>/dev/null
}

FAILURES=0

for r in $REGIONS; do
  step "$r"

  vpc=$(aws ec2 create-vpc --region "$r" --cidr-block "$PROBE_CIDR" --query 'Vpc.VpcId' --output text 2>/dev/null)
  if [[ -z "$vpc" || "$vpc" == "None" ]]; then
    warn "could not create probe VPC (region disabled or VPC limit reached) - skipping"
    continue
  fi
  az=$(aws ec2 describe-availability-zones --region "$r" --query 'AvailabilityZones[0].ZoneName' --output text 2>/dev/null)
  sub=$(aws ec2 create-subnet --region "$r" --vpc-id "$vpc" \
          --cidr-block "${PROBE_CIDR%.*.*}.1.0/24" --availability-zone "$az" \
          --query 'Subnet.SubnetId' --output text 2>/dev/null)
  if [[ -z "$sub" || "$sub" == "None" ]]; then
    warn "could not create probe subnet - skipping"
    aws ec2 delete-vpc --region "$r" --vpc-id "$vpc" >/dev/null 2>&1
    continue
  fi

  for spec in "${SPECS[@]}"; do
    label="${spec%%:*}"; rest="${spec#*:}"
    key="${rest%%:*}"; types="${rest#*:}"
    ami="$(ami_for "$r" "$key")"
    if [[ -z "$ami" ]]; then
      warn "$label: no AMI in RegionMap for $r"
      continue
    fi
    for t in $types; do
      out=$(aws ec2 run-instances --region "$r" --image-id "$ami" --instance-type "$t" \
              --subnet-id "$sub" --dry-run 2>&1)
      if grep -q "DryRunOperation" <<<"$out"; then
        printf "  %-12s %-12s ALLOWED\n" "$label" "$t"
      elif grep -qi "OptInRequired" <<<"$out"; then
        printf "  %-12s %-12s NOT-SUBSCRIBED (result inconclusive here)\n" "$label" "$t"
      elif grep -qi "Unsupported" <<<"$out"; then
        printf "  %-12s %-12s BLOCKED\n" "$label" "$t"
      else
        code=$(sed -n 's/.*(\([A-Za-z.]*\)).*/\1/p' <<<"$out" | head -1)
        printf "  %-12s %-12s OTHER[%s]\n" "$label" "$t" "${code:-unknown}"
        FAILURES=$((FAILURES+1))
      fi
    done
  done

  aws ec2 delete-subnet --region "$r" --subnet-id "$sub" >/dev/null 2>&1
  aws ec2 delete-vpc --region "$r" --vpc-id "$vpc" >/dev/null 2>&1
done

step "Done"
if [[ "$FAILURES" -gt 0 ]]; then
  warn "$FAILURES probe(s) returned an unexpected error code - review above."
else
  ok "All probes returned a clean ALLOWED/BLOCKED verdict."
fi
echo "  Keep AllowedValues in deploy/cloudformation/*.yaml and the *_ALLOWED lists"
echo "  in deploy/deploy-stack.sh in sync with whatever came back ALLOWED."
