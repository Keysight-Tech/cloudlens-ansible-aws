#!/usr/bin/env bash
# =====================================================================
# CloudLens: deploy 3 test workload EC2s (Ubuntu + RHEL + Windows) for
# end-to-end sensor verification, each generating a little traffic.
# =====================================================================
# Stands up 3 small EC2 instances in the default VPC, each tagged with a
# custom discovery tag so you can verify deploy-stack.sh sensor deployment
# AND prove the dynamic-tag feature works (not hard-coded to cloudlens=yes).
# Each instance runs a tiny curl/ping loop so the sensor has traffic to see.
#
# Usage:
#   bash deploy-test-workload-vms.sh
#   OR with overrides:
#   bash deploy-test-workload-vms.sh \
#        --region us-east-1 \
#        --key-name my-key \
#        --tag-key monitoring --tag-value enabled \
#        --env prod
#
# After this script:
#   curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh \
#     | bash -s -- --discovery-tag-key monitoring --discovery-tag-value enabled
# =====================================================================
set -euo pipefail
{

# ---- Overridable config ----
REGION="${TESTVMS_REGION:-${AWS_REGION:-us-east-1}}"
DISCOVERY_TAG_KEY="${TESTVMS_TAG_KEY:-monitoring}"
DISCOVERY_TAG_VALUE="${TESTVMS_TAG_VALUE:-enabled}"
ENV_TAG="${TESTVMS_ENV:-prod}"
KEY_NAME="${TESTVMS_KEY_NAME:-cloudlens-test-key}"
ASSUME_YES=false
LINUX_TYPE="${TESTVMS_LINUX_TYPE:-t3.small}"
WIN_TYPE="${TESTVMS_WIN_TYPE:-t3.medium}"

# The prefix exists so two stacks can coexist. The names used to be fixed, so a
# second stack's workloads were called test-ubuntu-1 exactly like the first's.
# The aws_ec2 inventory keys hosts by their Name tag, so the duplicates collapse
# onto one another and a sensor run aimed at one stack lands on the other
# stack's machines. Seen live with two stacks in one region.
NAME_PREFIX="${TESTVMS_NAME_PREFIX:-test}"

# ---- args ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)     REGION="$2"; shift 2 ;;
    --key-name)   KEY_NAME="$2"; shift 2 ;;
    --tag-key)    DISCOVERY_TAG_KEY="$2"; shift 2 ;;
    --tag-value)  DISCOVERY_TAG_VALUE="$2"; shift 2 ;;
    --env)        ENV_TAG="$2"; shift 2 ;;
    --vpc-id)     VPC_ID="$2"; shift 2 ;;
    --subnet-id)  SUBNET_ID="$2"; shift 2 ;;
    --name-prefix) NAME_PREFIX="$2"; shift 2 ;;
    -y|--yes)     ASSUME_YES=true; shift ;;
    -h|--help)
      sed -n '1,/^set -e/p' "$0" | head -n -1 | tail -n +2; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Resolved after parsing so --name-prefix applies.
UBUNTU_NAME="${NAME_PREFIX}-ubuntu-1"
RHEL_NAME="${NAME_PREFIX}-rhel-1"
WIN_NAME="${NAME_PREFIX}-windows-1"

AWS=(aws --region "$REGION")

# ---- helpers ----
C_GREEN='\033[0;32m'; C_BLUE='\033[0;34m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_RESET='\033[0m'
ok()   { echo -e "${C_GREEN}[ok]${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}[warn]${C_RESET} $1"; }
fail() { echo -e "${C_RED}[x]${C_RESET} $1" >&2; exit 1; }
step() { echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }

command -v aws >/dev/null 2>&1 || fail "AWS CLI not found. Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
"${AWS[@]}" sts get-caller-identity >/dev/null 2>&1 || fail "AWS auth missing/expired. Run: aws configure  OR  aws sso login"

ACCOUNT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)

# ---- resolve latest AMIs for each OS ----
step "Resolving AMIs in $REGION"
# Ubuntu 22.04 LTS via Canonical's public SSM parameter
UBUNTU_AMI=$("${AWS[@]}" ssm get-parameters \
  --names /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
  --query 'Parameters[0].Value' --output text 2>/dev/null || echo "None")
# Windows Server 2022 via the AWS-published public SSM parameter
WIN_AMI=$("${AWS[@]}" ssm get-parameters \
  --names /aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base \
  --query 'Parameters[0].Value' --output text 2>/dev/null || echo "None")
# RHEL 9 via describe-images (Red Hat owner account 309956199498)
RHEL_AMI=$("${AWS[@]}" ec2 describe-images --owners 309956199498 \
  --filters "Name=name,Values=RHEL-9*_HVM-*-x86_64-*" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null || echo "None")

for pair in "Ubuntu:$UBUNTU_AMI" "RHEL:$RHEL_AMI" "Windows:$WIN_AMI"; do
  label="${pair%%:*}"; ami="${pair#*:}"
  [[ "$ami" == "None" || -z "$ami" ]] && fail "Could not resolve a $label AMI in $REGION."
  ok "$label AMI: $ami"
done

# ---- ensure key pair ----
if ! "${AWS[@]}" ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  PEM="$HOME/.ssh/${KEY_NAME}.pem"
  mkdir -p "$HOME/.ssh"
  "${AWS[@]}" ec2 create-key-pair --key-name "$KEY_NAME" --query 'KeyMaterial' --output text > "$PEM"
  chmod 600 "$PEM"
  ok "Created key pair '$KEY_NAME' -> $PEM"
else
  ok "Key pair '$KEY_NAME' exists"
fi

# ---- VPC + a subnet ----
# The default VPC is only a default, not a requirement. Plenty of accounts have
# none (many organisations delete it on purpose), and this used to fail there
# telling the operator to "set a subnet manually" with no flag to do it.
if [[ -n "${VPC_ID:-}" || -n "${SUBNET_ID:-}" ]]; then
  step "Network (from arguments)"
  if [[ -z "${SUBNET_ID:-}" ]]; then
    SUBNET_ID=$("${AWS[@]}" ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
      --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text)
    [[ "$SUBNET_ID" == "None" || -z "$SUBNET_ID" ]] && fail "No subnet found in ${VPC_ID}."
  fi
  # A subnet knows its own VPC, so --subnet-id alone is enough.
  SUBNET_VPC=$("${AWS[@]}" ec2 describe-subnets --subnet-ids "$SUBNET_ID" \
    --query 'Subnets[0].VpcId' --output text 2>/dev/null || echo "None")
  [[ "$SUBNET_VPC" == "None" || -z "$SUBNET_VPC" ]] && fail "Subnet ${SUBNET_ID} not found in ${REGION}."
  if [[ -n "${VPC_ID:-}" && "$VPC_ID" != "$SUBNET_VPC" ]]; then
    fail "Subnet ${SUBNET_ID} belongs to ${SUBNET_VPC}, not the --vpc-id you gave (${VPC_ID})."
  fi
  VPC_ID="$SUBNET_VPC"
else
  step "Network (default VPC)"
  VPC_ID=$("${AWS[@]}" ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query 'Vpcs[0].VpcId' --output text)
  if [[ "$VPC_ID" == "None" || -z "$VPC_ID" ]]; then
    fail "No default VPC in ${REGION}. Point this at a network you already have:
    --subnet-id subnet-0123456789abcdef0     (the VPC is derived from it)
    --vpc-id vpc-0123456789abcdef0           (picks the first subnet in it)
  List them with:
    aws ec2 describe-subnets --region ${REGION} --query 'Subnets[].[SubnetId,VpcId,AvailabilityZone]' --output table"
  fi
  SUBNET_ID=$("${AWS[@]}" ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text)
fi
ok "VPC $VPC_ID / subnet $SUBNET_ID"

# ---- security group (SSH, WinRM, RDP) ----
SG_NAME="cloudlens-test-vms-sg"
SG_ID=$("${AWS[@]}" ec2 describe-security-groups \
  --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
  SG_ID=$("${AWS[@]}" ec2 create-security-group --group-name "$SG_NAME" \
    --description "CloudLens test workload VMs" --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)
  for pp in "22" "5985" "3389"; do
    "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --protocol tcp --port "$pp" --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
  done
  ok "Created SG $SG_ID (SSH 22, WinRM 5985, RDP 3389)"
else
  ok "Reusing SG $SG_ID"
fi

echo
step "Test workload EC2s - configuration"
echo "  Account:         $ACCOUNT"
echo "  Region:          $REGION"
echo "  Discovery tag:   $DISCOVERY_TAG_KEY=$DISCOVERY_TAG_VALUE"
echo "  Env tag:         $ENV_TAG"
echo "  Key pair:        $KEY_NAME"
echo "  Instances:       $UBUNTU_NAME (Ubuntu 22.04), $RHEL_NAME (RHEL 9), $WIN_NAME (Win 2022)"
echo
# --yes, or no terminal to ask on. Without this the script read EOF from a
# pipe or a nohup, took it for "no", and exited 0 having created nothing:
# indistinguishable from success in a log, which is the worst way to fail.
if [[ "$ASSUME_YES" == "true" ]]; then
  ok "Proceeding (--yes)"
elif [[ ! -t 0 ]]; then
  fail "No terminal to confirm on, and --yes was not given, so nothing was created.
  Re-run with --yes to skip this prompt when piping or scripting."
else
  read -rp "Proceed? [y/N]: " yn || yn=""
  yn_lc=$(echo "$yn" | tr '[:upper:]' '[:lower:]')
  [[ "$yn_lc" == "y" || "$yn_lc" == "yes" ]] || { warn "Aborted"; exit 0; }
fi

# ---- traffic-generator user data ----
# A small loop so the CloudLens sensor has packets to observe on the demo.
LINUX_USERDATA=$(cat <<'UD'
#!/bin/bash
cat >/usr/local/bin/cloudlens-traffic.sh <<'EOS'
#!/bin/bash
while true; do
  curl -s -o /dev/null https://aws.amazon.com/ || true
  ping -c 2 8.8.8.8 >/dev/null 2>&1 || true
  sleep 20
done
EOS
chmod +x /usr/local/bin/cloudlens-traffic.sh
cat >/etc/systemd/system/cloudlens-traffic.service <<'EOS'
[Unit]
Description=CloudLens demo traffic generator
After=network-online.target
[Service]
ExecStart=/usr/local/bin/cloudlens-traffic.sh
Restart=always
[Install]
WantedBy=multi-user.target
EOS
systemctl daemon-reload && systemctl enable --now cloudlens-traffic.service
UD
)

WIN_USERDATA=$(cat <<'UD'
<powershell>
$s = @'
while ($true) {
  try { Invoke-WebRequest -UseBasicParsing https://aws.amazon.com/ | Out-Null } catch {}
  Test-Connection 8.8.8.8 -Count 2 -ErrorAction SilentlyContinue | Out-Null
  Start-Sleep -Seconds 20
}
'@
Set-Content -Path C:\cloudlens-traffic.ps1 -Value $s
schtasks /Create /SC ONSTART /TN CloudLensTraffic /TR "powershell -File C:\cloudlens-traffic.ps1" /RU SYSTEM /F
Start-Process powershell -ArgumentList "-File C:\cloudlens-traffic.ps1" -WindowStyle Hidden
</powershell>
UD
)

# ---- SSM instance profile ----
# Windows is deployed over AWS SSM by default (see inventory/group_vars/
# os_windows.yaml). SSM only reaches an instance that carries a role with
# AmazonSSMManagedInstanceCore, and this script attached none: the Windows VM
# came up unmanaged, so the sensor playbook could never connect to it. Proven
# live: describe-instance-information returned nothing for the Windows box
# while Ubuntu and RHEL installed fine over SSH.
step "SSM instance profile (so Windows is reachable)"
SSM_ROLE="CloudLensTestVMSSMRole"
SSM_PROFILE_ARG=()
setup_ssm_profile() {
  if ! "${AWS[@]}" iam get-role --role-name "$SSM_ROLE" >/dev/null 2>&1; then
    "${AWS[@]}" iam create-role --role-name "$SSM_ROLE" \
      --description "SSM access for CloudLens test workload VMs" \
      --assume-role-policy-document \
      '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      >/dev/null 2>&1 || return 1
  fi
  "${AWS[@]}" iam attach-role-policy --role-name "$SSM_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null 2>&1 || return 1
  if ! "${AWS[@]}" iam get-instance-profile --instance-profile-name "$SSM_ROLE" >/dev/null 2>&1; then
    "${AWS[@]}" iam create-instance-profile --instance-profile-name "$SSM_ROLE" >/dev/null 2>&1 || return 1
    "${AWS[@]}" iam add-role-to-instance-profile --instance-profile-name "$SSM_ROLE" \
      --role-name "$SSM_ROLE" >/dev/null 2>&1 || return 1
    sleep 10  # IAM is eventually consistent: a brand new profile is not instantly usable by RunInstances
  fi
  return 0
}
if setup_ssm_profile; then
  SSM_PROFILE_ARG=(--iam-instance-profile "Name=${SSM_ROLE}")
  ok "Instance profile ${SSM_ROLE} ready (AmazonSSMManagedInstanceCore)"
else
  warn "Could not create the SSM instance profile (needs iam:CreateRole and iam:PassRole)."
  warn "The Linux VMs are unaffected. Windows will come up unmanaged, and the sensor"
  warn "playbook will not be able to reach it over SSM. Attach a role with"
  warn "AmazonSSMManagedInstanceCore yourself, or use WinRM instead."
fi

# ---- create the 3 instances ----
step "Provisioning 3 instances"

run_instance() {
  local name="$1" ami="$2" itype="$3" os="$4" userdata="$5"
  "${AWS[@]}" ec2 run-instances \
    --image-id "$ami" --instance-type "$itype" --key-name "$KEY_NAME" \
    --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
    --associate-public-ip-address \
    "${SSM_PROFILE_ARG[@]}" \
    --user-data "$userdata" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=${DISCOVERY_TAG_KEY},Value=${DISCOVERY_TAG_VALUE}},{Key=os,Value=${os}},{Key=env,Value=${ENV_TAG}},{Key=workload,Value=test}]" \
    --query 'Instances[0].InstanceId' --output text
}

UBUNTU_ID=$(run_instance "$UBUNTU_NAME" "$UBUNTU_AMI" "$LINUX_TYPE" "ubuntu" "$LINUX_USERDATA")
ok "$UBUNTU_NAME -> $UBUNTU_ID"
RHEL_ID=$(run_instance "$RHEL_NAME" "$RHEL_AMI" "$LINUX_TYPE" "rhel" "$LINUX_USERDATA")
ok "$RHEL_NAME -> $RHEL_ID"
WIN_ID=$(run_instance "$WIN_NAME" "$WIN_AMI" "$WIN_TYPE" "windows" "$WIN_USERDATA")
ok "$WIN_NAME -> $WIN_ID"

step "Waiting for instances to reach running"
"${AWS[@]}" ec2 wait instance-running --instance-ids "$UBUNTU_ID" "$RHEL_ID" "$WIN_ID"
for id in "$UBUNTU_ID" "$RHEL_ID" "$WIN_ID"; do
  ip=$("${AWS[@]}" ec2 describe-instances --instance-ids "$id" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  ok "$id ready at $ip"
done

step "All 3 test instances provisioned + tagged"
"${AWS[@]}" ec2 describe-instances \
  --filters "Name=tag:${DISCOVERY_TAG_KEY},Values=${DISCOVERY_TAG_VALUE}" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].{id:InstanceId,name:Tags[?Key==`Name`]|[0].Value,ip:PublicIpAddress}' \
  --output table

step "Next step - run deploy-stack.sh with your custom tag"
echo
echo "  curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh | \\"
echo "    bash -s -- --discovery-tag-key ${DISCOVERY_TAG_KEY} --discovery-tag-value ${DISCOVERY_TAG_VALUE} --region ${REGION}"
echo
echo "  In Phase 9 you should see:"
echo "    Workload EC2s tagged ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}: 3"
echo
echo "Cleanup when done:"
echo "  aws ec2 terminate-instances --region ${REGION} --instance-ids ${UBUNTU_ID} ${RHEL_ID} ${WIN_ID}"
echo

}   # End of brace-wrap for curl|bash safety
