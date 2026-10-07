#!/usr/bin/env bash
# deploy/tests/test_collector_sgs.sh: the collector management SG must let KVO
# in on 22 and 9022 from inside the VPC, not only from the admin CIDR.
#
# Why this exists. KVO configures the collector (vHub) over SSH: the KVO 3.1.0
# User Guide lists "TCP 22 Inbound vHub" and "TCP 9022 Inbound vHub" for the
# AWS Cloud Config. ensure_collector_sgs opened both ports to ADMIN_CIDR only.
# With the CIS-correct one-host admin CIDR, KVO (inside the VPC) was refused:
# the collector booted, registered its mirror target and never cut a single
# mirror session, and nothing alerted. Seen live 2026-09-28. The open default
# (0.0.0.0/0) had hidden it on every earlier run.
#
# Hermetic: `probe` and `aws` are stubs that record the authorize calls.
#
# Usage: bash deploy/tests/test_collector_sgs.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

awk '
  /^(ok|warn|fail|step|note|dryrun_say)\(\)/ { print; next }
  /^ensure_collector_sgs\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/helpers.sh"

PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }

grep -q '^ensure_collector_sgs()' "$S/helpers.sh" || { echo "FAIL ensure_collector_sgs missing"; echo; echo "0 PASS, 1 FAIL"; exit 1; }

# The stub answers describe-vpcs with the CIDR, create-security-group with an
# id, and records every authorize-security-group-ingress call verbatim.
run_case() {
  local admin="$1"
  : > "$S/calls"
  ( set -u
    REGION=us-east-1; STACK_NAME=t; STACK_VPC_ID=vpc-1; DRY_RUN=false; ADMIN_CIDR="$admin"
    probe() { "$@"; }
    aws() {
      case "$*" in
        *describe-vpcs*) echo "10.99.0.0/16" ;;
        *create-security-group*) echo "sg-$(echo "$*" | sed -n 's/.*cloudlens-collector-\([a-z]*\)-.*/\1/p')" ;;
        *authorize-security-group-ingress*) printf '%s\n' "$*" >> "$S/calls" ;;
      esac
    }
    ok() { :; }; warn() { :; }
    # shellcheck disable=SC1090
    . "$S/helpers.sh"
    ensure_collector_sgs vpc-1 >/dev/null 2>&1
  )
}

run_case "203.0.113.7/32"
mgmt=$(grep -- '--group-id sg-mgmt' "$S/calls")
for port in 22 9022; do
  if grep -q "FromPort=${port},ToPort=${port},IpRanges=\[{CidrIp=10.99.0.0/16}" <<<"$mgmt"; then
    ok_ "mgmt SG opens $port from the VPC CIDR (KVO to vHub)"
  else
    bad_ "mgmt SG does not open $port from the VPC CIDR: $mgmt"
  fi
  if grep -q "FromPort=${port},ToPort=${port},IpRanges=\[{CidrIp=10.99.0.0/16},{CidrIp=203.0.113.7/32}" <<<"$mgmt"; then
    ok_ "mgmt SG still opens $port to the admin CIDR"
  else
    bad_ "mgmt SG lost the admin CIDR on $port: $mgmt"
  fi
done
if grep -q "FromPort=443,ToPort=443,IpRanges=\[{CidrIp=10.99.0.0/16}\]" <<<"$mgmt" && ! grep -q "FromPort=443,ToPort=443,IpRanges=\[{CidrIp=203.0.113.7" <<<"$mgmt"; then
  ok_ "mgmt SG opens 443 to the VPC only"
else
  bad_ "mgmt SG 443 rule wrong: $mgmt"
fi
ing=$(grep -- '--group-id sg-ingress' "$S/calls")
if grep -q "udp,FromPort=4789" <<<"$ing" && grep -q "IpProtocol=47" <<<"$ing" && ! grep -q "FromPort=22" <<<"$ing"; then
  ok_ "ingress SG: VXLAN + GRE from the VPC, no SSH"
else
  bad_ "ingress SG rules wrong: $ing"
fi

echo; echo "$PASS PASS, $FAIL FAIL"
[[ $FAIL -eq 0 ]]
