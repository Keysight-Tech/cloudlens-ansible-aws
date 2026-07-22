#!/usr/bin/env bash
# Enable AWS Zone Tapping on an ALREADY-RUNNING KVO instance (brownfield).
#
# Greenfield deployments set EnableZoneTapping=yes on the CloudFormation stack
# and the KVO launches with the instance profile already attached. Use THIS
# script when KVO is already running (deployed before Zone Tapping was enabled,
# or deployed by a principal who could not create IAM at stack time).
#
# It is idempotent: creates the IAM policy, role, and instance profile if they
# are missing, then associates the profile with the KVO instance. Safe to re-run.
#
# The policy is the least-privilege set from the KVO 3.0.1 User Guide, Ch.10:
# KVO may deploy collector Service VMs and create AWS VPC Traffic Mirror
# sessions, every create scoped by the cloudlens:monitored:vpcid tag KVO applies.
#
# Requires: awscli, and a caller with IAM + ec2:AssociateIamInstanceProfile
# rights (a one-time admin action; the base suite deploy does not need this).
set -euo pipefail

POLICY_NAME="CloudLensZoneTap"
ROLE_NAME="cloudlens-kvo-zonetap"
PROFILE_NAME="cloudlens-kvo-zonetap"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLICY_FILE="${HERE}/../deploy/iam/cloudlens-zonetap-policy.json"

usage() {
  echo "Usage: $0 --instance-id <kvo-ec2-id> [--region <region>] [--profile <aws-profile>]" >&2
  echo "   or: $0 --instance-name <Name-tag> [--region <region>] [--profile <aws-profile>]" >&2
  exit 2
}

REGION="${AWS_REGION:-us-east-1}"; INSTANCE_ID=""; INSTANCE_NAME=""; AWS_PROFILE_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --instance-id)   INSTANCE_ID="$2"; shift 2;;
    --instance-name) INSTANCE_NAME="$2"; shift 2;;
    --region)        REGION="$2"; shift 2;;
    --profile)       AWS_PROFILE_ARG="--profile $2"; shift 2;;
    -h|--help)       usage;;
    *) echo "unknown arg: $1" >&2; usage;;
  esac
done
aws() { command aws $AWS_PROFILE_ARG "$@"; }

[ -f "$POLICY_FILE" ] || { echo "policy file not found: $POLICY_FILE" >&2; exit 1; }

# Resolve instance id from Name tag if needed.
if [ -z "$INSTANCE_ID" ] && [ -n "$INSTANCE_NAME" ]; then
  INSTANCE_ID=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
fi
[ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ] || { echo "could not resolve KVO instance" >&2; usage; }
ACCT=$(aws sts get-caller-identity --query Account --output text)
POLICY_ARN="arn:aws:iam::${ACCT}:policy/${POLICY_NAME}"

echo "[zonetap] account=$ACCT region=$REGION kvo=$INSTANCE_ID"

# 1. policy (idempotent)
if ! aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  aws iam create-policy --policy-name "$POLICY_NAME" \
    --policy-document "file://$POLICY_FILE" \
    --description "KVO Zone Tapping least-privilege (KVO 3.0.1 UG Ch.10)" >/dev/null
  echo "[zonetap] created policy $POLICY_NAME"
else
  echo "[zonetap] policy $POLICY_NAME exists"
fi

# 2. role + attach (idempotent)
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --description "Instance role for KVO AWS Zone Tapping" >/dev/null
  echo "[zonetap] created role $ROLE_NAME"
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" >/dev/null 2>&1 || true

# 3. instance profile + role membership (idempotent)
if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
  echo "[zonetap] created instance profile $PROFILE_NAME"
fi
if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" \
     --query 'InstanceProfile.Roles[0].RoleName' --output text 2>/dev/null | grep -q "^${ROLE_NAME}$"; then
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME" >/dev/null
  echo "[zonetap] added role to instance profile"
fi

# 4. associate with the KVO instance (skip if already the same profile)
CURRENT=$(aws ec2 describe-iam-instance-profile-associations --region "$REGION" \
  --filters "Name=instance-id,Values=$INSTANCE_ID" \
  --query "IamInstanceProfileAssociations[?State=='associated'].IamInstanceProfile.Arn | [0]" --output text 2>/dev/null || echo "None")
WANT="arn:aws:iam::${ACCT}:instance-profile/${PROFILE_NAME}"
if [ "$CURRENT" = "$WANT" ]; then
  echo "[zonetap] KVO already has the zone-tap profile; nothing to do"
elif [ "$CURRENT" != "None" ] && [ -n "$CURRENT" ]; then
  echo "[zonetap] KVO has a different profile ($CURRENT). Replace it manually if intended:" >&2
  echo "  aws ec2 replace-iam-instance-profile-association ..." >&2
  exit 3
else
  # IAM propagation can lag; retry the associate briefly.
  for i in 1 2 3 4 5 6; do
    if aws ec2 associate-iam-instance-profile --region "$REGION" \
         --instance-id "$INSTANCE_ID" --iam-instance-profile "Name=$PROFILE_NAME" >/dev/null 2>&1; then
      echo "[zonetap] associated instance profile with KVO"; break
    fi
    echo "[zonetap] associate not ready (attempt $i), waiting..."; sleep 10
  done
fi

echo "[zonetap] done. KVO can now run AWS Zone Tapping."
echo "[zonetap] Note: KVO reads instance-role credentials at startup; if it was"
echo "[zonetap] running before this ran, reboot KVO or restart its services so"
echo "[zonetap] the app picks up the new credentials."
