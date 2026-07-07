#!/usr/bin/env bash
# =====================================================================
# CloudLens AWS Visibility Demo full teardown
# =====================================================================
# Nukes everything created by setup-aws-visibility-demo.sh. Discovery is by
# the tag purpose=cloudlens-demo, so nothing outside the demo is touched.
# Deletes in dependency order: instances -> ENIs -> EIPs -> security groups
# -> subnets -> route associations -> internet gateway -> VPC -> key pair.
# =====================================================================
set -euo pipefail

REGION="${DEMO_REGION:-us-east-1}"
DEMO_TAG="purpose=cloudlens-demo"
VPC_NAME="cloudlens-demo-vpc"
KEY_NAME="cloudlens-demo-key"
CFT_STACK="cloudlens-demo-stack"
STATE_DIR="${HOME}/.cloudlens-demo"

QUIET=false
KEEP_KEY=false
for arg in "$@"; do
  case "$arg" in
    --quiet)    QUIET=true ;;
    --keep-key) KEEP_KEY=true ;;
    -h|--help)
      sed -n '/^# Nukes/,/^# =====/p' "$0" | sed 's/^# //; /^=====/d'
      exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

aws() { command aws --region "$REGION" "$@"; }
none() { [[ -z "$1" || "$1" == "None" ]]; }

echo "Region: $REGION   Filter: tag ${DEMO_TAG}"

# Discover demo instances up front so we can show what we're about to remove.
INSTANCE_IDS=$(aws ec2 describe-instances \
  --filters "Name=tag:${DEMO_TAG%%=*},Values=${DEMO_TAG##*=}" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$VPC_NAME" \
  --query 'Vpcs[0].VpcId' --output text)

if [[ "$QUIET" != "true" ]]; then
  echo "About to delete:"
  echo "  instances : ${INSTANCE_IDS:-<none>}"
  echo "  vpc       : ${VPC_ID:-<none>}"
  echo "  cfn stack : ${CFT_STACK} (if present)"
  echo "  key pair  : $([[ "$KEEP_KEY" == true ]] && echo '(kept)' || echo "$KEY_NAME")"
  read -rp "Proceed? [y/N] " yn
  [[ "${yn,,}" == "y" || "${yn,,}" == "yes" ]] || { echo "Cancelled."; exit 0; }
fi

# 1. CloudFormation stack (if the appliances came from the stack template).
if aws cloudformation describe-stacks --stack-name "$CFT_STACK" >/dev/null 2>&1; then
  echo "→ Deleting CloudFormation stack $CFT_STACK ..."
  aws cloudformation delete-stack --stack-name "$CFT_STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$CFT_STACK" 2>/dev/null || true
  echo "✓ stack deleted"
fi

# 2. Terminate demo instances.
if ! none "$INSTANCE_IDS"; then
  echo "→ Terminating instances: $INSTANCE_IDS"
  aws ec2 terminate-instances --instance-ids $INSTANCE_IDS >/dev/null
  aws ec2 wait instance-terminated --instance-ids $INSTANCE_IDS 2>/dev/null || true
  echo "✓ instances terminated"
else
  echo "  no demo instances found"
fi

# 3. Release demo Elastic IPs.
for alloc in $(aws ec2 describe-addresses \
    --filters "Name=tag:${DEMO_TAG%%=*},Values=${DEMO_TAG##*=}" \
    --query 'Addresses[].AllocationId' --output text); do
  aws ec2 release-address --allocation-id "$alloc" 2>/dev/null \
    && echo "✓ released EIP $alloc" || true
done

# 4. Delete leftover demo ENIs (detached ones the terminate did not reap).
for eni in $(aws ec2 describe-network-interfaces \
    --filters "Name=tag:${DEMO_TAG%%=*},Values=${DEMO_TAG##*=}" \
    --query 'NetworkInterfaces[?Status==`available`].NetworkInterfaceId' --output text); do
  aws ec2 delete-network-interface --network-interface-id "$eni" 2>/dev/null \
    && echo "✓ deleted ENI $eni" || true
done

if ! none "$VPC_ID"; then
  # 5. Security groups (skip the default group).
  for sg in $(aws ec2 describe-security-groups \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:${DEMO_TAG%%=*},Values=${DEMO_TAG##*=}" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
    aws ec2 delete-security-group --group-id "$sg" 2>/dev/null \
      && echo "✓ deleted SG $sg" || echo "  SG $sg still in use (retrying below)"
  done

  # 6. Subnets.
  for sub in $(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'Subnets[].SubnetId' --output text); do
    aws ec2 delete-subnet --subnet-id "$sub" 2>/dev/null \
      && echo "✓ deleted subnet $sub" || true
  done

  # 7. Internet gateway: detach + delete.
  for igw in $(aws ec2 describe-internet-gateways \
      --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
      --query 'InternetGateways[].InternetGatewayId' --output text); do
    aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$VPC_ID" 2>/dev/null || true
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw" 2>/dev/null \
      && echo "✓ deleted IGW $igw" || true
  done

  # 8. Non-main route tables.
  for rtb in $(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text); do
    aws ec2 delete-route-table --route-table-id "$rtb" 2>/dev/null \
      && echo "✓ deleted route table $rtb" || true
  done

  # 9. Retry any security groups that were still in use before ENIs cleared.
  for sg in $(aws ec2 describe-security-groups \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:${DEMO_TAG%%=*},Values=${DEMO_TAG##*=}" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
    aws ec2 delete-security-group --group-id "$sg" 2>/dev/null \
      && echo "✓ deleted SG $sg (retry)" || true
  done

  # 10. The VPC itself.
  aws ec2 delete-vpc --vpc-id "$VPC_ID" 2>/dev/null \
    && echo "✓ deleted VPC $VPC_ID" \
    || echo "⚠ VPC $VPC_ID not deleted (dependencies may still be draining; re-run in ~30s)"
else
  echo "  no demo VPC found"
fi

# 11. EC2 key pair.
if [[ "$KEEP_KEY" != "true" ]]; then
  aws ec2 delete-key-pair --key-name "$KEY_NAME" 2>/dev/null \
    && echo "✓ deleted key pair $KEY_NAME" || true
fi

# 12. Cached state (leave the admin password file for the operator's records).
rm -f "${STATE_DIR}/state.env"
echo ""
echo "Teardown complete. The admin password file (${STATE_DIR}/admin_pw) was left in place."
echo "Verify nothing remains with:"
echo "  aws ec2 describe-instances --region $REGION --filters Name=tag:${DEMO_TAG%%=*},Values=${DEMO_TAG##*=} Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text"
