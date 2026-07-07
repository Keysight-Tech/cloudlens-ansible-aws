#!/usr/bin/env bash
# =====================================================================
# Standalone WinRM Bootstrap via AWS Systems Manager (SSM)
# =====================================================================
# Use this to enable WinRM on a single Windows EC2 WITHOUT Ansible.
# For bulk operations, use playbooks/bootstrap_windows_winrm.yaml.
#
# It uses `aws ssm send-command` (NOT WinRM itself) to run PowerShell on
# the box, so the instance needs the SSM Agent running and an instance
# profile that allows SSM (AmazonSSMManagedInstanceCore). Most modern
# Windows AMIs ship the SSM Agent pre-installed.
#
# The instance can be given as an instance-id (i-0abc...) or a Name tag.
# =====================================================================
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <instance-id | Name-tag> [region]"
  echo ""
  echo "Examples:"
  echo "  $0 i-0123456789abcdef0"
  echo "  $0 windowsvm01 us-east-1"
  exit 1
fi

TARGET="$1"
REGION="${2:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"
AWS=(aws --region "$REGION")

# Resolve a Name tag to an instance-id if the caller did not pass i-...
if [[ "$TARGET" == i-* ]]; then
  INSTANCE_ID="$TARGET"
else
  echo "Resolving Name tag '$TARGET' to an instance id in $REGION..."
  INSTANCE_ID=$("${AWS[@]}" ec2 describe-instances \
    --filters "Name=tag:Name,Values=${TARGET}" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]] && { echo "No running instance named '$TARGET' found."; exit 1; }
fi
echo "Target instance: $INSTANCE_ID"

# --- Enable WinRM over SSM ---
echo "Enabling WinRM on $INSTANCE_ID via SSM RunPowerShellScript..."
PS_SCRIPT='winrm quickconfig -force; Enable-PSRemoting -Force; New-NetFirewallRule -DisplayName "WinRM-HTTP" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -ErrorAction SilentlyContinue; Set-Item WSMan:\localhost\Service\Auth\Basic $true -Force; Set-Item WSMan:\localhost\Service\AllowUnencrypted $true -Force; Restart-Service WinRM; "WinRM enabled successfully"'

CMD_ID=$("${AWS[@]}" ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunPowerShellScript" \
  --comment "CloudLens WinRM bootstrap" \
  --parameters "commands=[\"$PS_SCRIPT\"]" \
  --query 'Command.CommandId' --output text)
echo "  SSM command id: $CMD_ID"

echo "  Waiting for command to complete..."
"${AWS[@]}" ssm wait command-executed --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" 2>/dev/null || true
STATUS=$("${AWS[@]}" ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query 'Status' --output text 2>/dev/null || echo "Unknown")
echo "  SSM command status: $STATUS"

# --- Open the security group for WinRM (port 5985) ---
echo ""
echo "Opening security group ingress for port 5985..."
SG_ID=$("${AWS[@]}" ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
if [[ -n "$SG_ID" && "$SG_ID" != "None" ]]; then
  "${AWS[@]}" ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --protocol tcp --port 5985 --cidr 0.0.0.0/0 2>/dev/null \
    && echo "  Added 5985 ingress to $SG_ID" \
    || echo "  (5985 rule may already exist on $SG_ID)"
else
  echo "  Could not resolve a security group for $INSTANCE_ID; open 5985 manually."
fi

echo ""
echo "[ok] WinRM bootstrap complete for $INSTANCE_ID"
echo "     Tip: prefer SSM (windows_connection: ssm) over WinRM where possible;"
echo "     it needs no inbound ports and is the default in customer_input.yaml."
