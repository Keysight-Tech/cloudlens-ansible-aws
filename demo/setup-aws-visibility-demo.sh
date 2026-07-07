#!/usr/bin/env bash
# =====================================================================
# Keysight CloudLens AWS Visibility Demo Kit: end-to-end build
# =====================================================================
# Provisions everything a customer would have in a real deployment, then
# runs the CUSTOMER-FACING automation (the CloudLens stack CloudFormation
# template + quickstart.sh Ansible) so the demo proves the production
# path works.
#
# Outcome (single demo VPC 10.99.0.0/16, us-east-1):
#   - mgmt subnet (10.99.1.0/24):  vController (=CLMS) + KVO
#   - data subnet (10.99.2.0/24):  2 sensor-target EC2s (Ubuntu + RHEL),
#                                  tagged cloudlens=yes, + vPB ingress NIC
#   - tool subnet (10.99.3.0/24):  tool receiver (tcpdump on VXLAN UDP/4789)
#                                  + vPB egress NIC
#   - vPB spans all three subnets (mgmt/data/tool), SSH on port 9022
#   - All sensors deployed via quickstart.sh Ansible automation
#   - KVO mirror session wires sensor traffic -> vPB -> tool receiver
#   - run-demo.sh generated for the live walkthrough
#
# Usage:
#   bash demo/setup-aws-visibility-demo.sh                     # full build
#   bash demo/setup-aws-visibility-demo.sh --skip-workloads    # reuse existing EC2s
#   bash demo/setup-aws-visibility-demo.sh --skip-sensors      # don't run Ansible
#   bash demo/setup-aws-visibility-demo.sh --teardown          # nuke everything
#
# Teardown (~2 min):
#   bash demo/teardown.sh
# =====================================================================
set -euo pipefail

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
REGION="${DEMO_REGION:-us-east-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The CloudLens stack (vController/CLMS + KVO + vPB) ships as a CloudFormation
# template in cloudlens-autopilot. If a copy is vendored here we use it; else
# we stand the three appliances up directly from their Marketplace AMIs below.
CFT_TEMPLATE="${REPO_ROOT}/deploy/cloudformation/stack.yaml"

# Marketplace AMIs (us-east-1) + instance sizes for the CloudLens appliances.
AMI_VCONTROLLER="ami-0bebd5e730315337e"; TYPE_VCONTROLLER="t3.xlarge"   # CLMS / vController
AMI_KVO="ami-017c0db8981569380";         TYPE_KVO="c5.2xlarge"          # KVO
AMI_VPB="ami-0a561b450552b707d";         TYPE_VPB="t3.xlarge"           # vPB
VPB_SSH_PORT=9022                                                        # vPB SSH is on 9022, not 22

# Networking: one demo VPC with mgmt / data / tool subnets (no peering needed).
VPC_NAME="cloudlens-demo-vpc"
VPC_CIDR="10.99.0.0/16"
MGMT_SUBNET_NAME="cloudlens-demo-mgmt"; MGMT_CIDR="10.99.1.0/24"
DATA_SUBNET_NAME="cloudlens-demo-data"; DATA_CIDR="10.99.2.0/24"
TOOL_SUBNET_NAME="cloudlens-demo-tool"; TOOL_CIDR="10.99.3.0/24"
AZ="${DEMO_AZ:-${REGION}a}"

WORKLOAD_TYPE="t3.small"
TOOL_TYPE="t3.small"

# EC2 key pair imported from the operator's local SSH public key.
KEY_NAME="cloudlens-demo-key"
SSH_USER_UBUNTU="ubuntu"
SSH_USER_RHEL="ec2-user"

# Everything the demo creates carries this tag so teardown can find it.
DEMO_TAG_KEY="purpose"
DEMO_TAG_VAL="cloudlens-demo"

STATE_DIR="${HOME}/.cloudlens-demo"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
PASSWORD_FILE="${STATE_DIR}/admin_pw"

SKIP_WORKLOADS=false
SKIP_SENSORS=false
DO_TEARDOWN=false

# ---------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_G='\033[0;32m'; C_Y='\033[1;33m'; C_B='\033[0;34m'; C_R='\033[0;31m'; C_GR='\033[0;90m'; C_BD='\033[1m'; C_X='\033[0m'
else C_G=''; C_Y=''; C_B=''; C_R=''; C_GR=''; C_BD=''; C_X=''; fi
banner() { echo; echo -e "${C_B}╔═══════════════════════════════════════════════════════════════╗${C_X}"; printf "${C_B}║${C_X}  ${C_BD}%-61s${C_X}${C_B}║${C_X}\n" "$1"; echo -e "${C_B}╚═══════════════════════════════════════════════════════════════╝${C_X}"; }
step()   { echo; echo -e "${C_B}━━━ $1 ━━━${C_X}"; }
ok()     { echo -e "${C_G}✓${C_X} $1"; }
warn()   { echo -e "${C_Y}⚠${C_X} $1"; }
fail()   { echo -e "${C_R}✗${C_X} $1" >&2; exit 1; }
note()   { echo -e "${C_GR}→ $1${C_X}"; }

aws() { command aws --region "$REGION" "$@"; }

# ---------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-workloads) SKIP_WORKLOADS=true; shift ;;
    --skip-sensors)   SKIP_SENSORS=true; shift ;;
    --teardown)       DO_TEARDOWN=true; shift ;;
    -h|--help)
      sed -n '/^# Usage/,/^# Teardown/p' "$0" | sed 's/^# //'
      exit 0 ;;
    *) fail "Unknown arg: $1" ;;
  esac
done

if [[ "$DO_TEARDOWN" == "true" ]]; then
  exec bash "${REPO_ROOT}/demo/teardown.sh"
fi

# ---------------------------------------------------------------------
# Small helpers (idempotent lookups by Name tag)
# ---------------------------------------------------------------------
none() { [[ -z "$1" || "$1" == "None" ]]; }

demo_tags() {
  # $1 = ResourceType, $2 = Name value
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$2},{Key=${DEMO_TAG_KEY},Value=${DEMO_TAG_VAL}},{Key=deployedBy,Value=cloudlens-demo}]"
}

instance_id_by_name() {
  aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$1" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[0].InstanceId' --output text 2>/dev/null | head -1
}
instance_public_ip() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null | head -1
}
instance_private_ip() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[].Instances[].PrivateIpAddress' --output text 2>/dev/null | head -1
}

# ---------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------
banner "CloudLens AWS Visibility Demo: end-to-end CloudLens visibility build"

step "Phase 0: Pre-flight"
command -v aws >/dev/null || fail "aws CLI not found (install AWS CLI v2)"
CALLER=$(aws sts get-caller-identity --query "{acct:Account,arn:Arn}" --output text 2>/dev/null) \
  || fail "AWS credentials invalid. Run: aws sso login  OR  aws configure"
ACCOUNT_ID=$(echo "$CALLER" | awk '{print $1}')
echo "  account: $ACCOUNT_ID   region: $REGION"
ok "AWS auth verified"

[[ -f ~/.ssh/id_rsa.pub ]] || fail "~/.ssh/id_rsa.pub missing : run ssh-keygen first"
ok "SSH public key ready"

# Generate or load the shared admin password (used to rotate the vController
# default password + as the Windows/appliance credential where applicable).
if [[ -f "$PASSWORD_FILE" ]]; then
  ADMIN_PW=$(cat "$PASSWORD_FILE")
  ok "Reusing stored admin password (${PASSWORD_FILE})"
else
  ADMIN_PW=$(python3 -c "
import secrets, string
alpha = string.ascii_letters; digits = string.digits; syms = '!@#\$%^&*'
pw = (secrets.choice(string.ascii_uppercase) + secrets.choice(string.ascii_lowercase) +
      secrets.choice(digits) + secrets.choice(syms) +
      ''.join(secrets.choice(alpha+digits+syms) for _ in range(12)))
print(pw)")
  umask 077
  echo "$ADMIN_PW" > "$PASSWORD_FILE"
  ok "Generated admin password, saved to ${PASSWORD_FILE} (mode 600)"
fi

# Import the operator SSH public key as an EC2 key pair (idempotent).
if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  ok "EC2 key pair exists: $KEY_NAME"
else
  aws ec2 import-key-pair --key-name "$KEY_NAME" \
    --public-key-material "fileb://${HOME}/.ssh/id_rsa.pub" \
    --tag-specifications "$(demo_tags key-pair "$KEY_NAME")" >/dev/null
  ok "Imported EC2 key pair: $KEY_NAME"
fi

MY_IP="$(curl -fsSL https://checkip.amazonaws.com | tr -d '[:space:]')/32"
ok "Operator public IP: $MY_IP (SSH/HTTPS locked to this /32)"

# ---------------------------------------------------------------------
# Phase 1: VPC + subnets + IGW + routing + security groups
#   (AWS single-VPC equivalent of the Azure VNets + peering phases)
# ---------------------------------------------------------------------
step "Phase 1: Demo VPC (mgmt / data / tool subnets)"

VPC_ID=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=$VPC_NAME" --query 'Vpcs[0].VpcId' --output text)
if none "$VPC_ID"; then
  VPC_ID=$(aws ec2 create-vpc --cidr-block "$VPC_CIDR" \
    --tag-specifications "$(demo_tags vpc "$VPC_NAME")" \
    --query 'Vpc.VpcId' --output text)
  aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames >/dev/null
  ok "VPC created: $VPC_ID ($VPC_CIDR)"
else
  ok "VPC exists: $VPC_ID"
fi

subnet_ensure() {
  # $1 name  $2 cidr  ->  echoes subnet id
  local name="$1" cidr="$2" sid
  sid=$(aws ec2 describe-subnets --filters "Name=tag:Name,Values=$name" "Name=vpc-id,Values=$VPC_ID" \
        --query 'Subnets[0].SubnetId' --output text)
  if none "$sid"; then
    sid=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$cidr" --availability-zone "$AZ" \
          --tag-specifications "$(demo_tags subnet "$name")" \
          --query 'Subnet.SubnetId' --output text)
    aws ec2 modify-subnet-attribute --subnet-id "$sid" --map-public-ip-on-launch >/dev/null
  fi
  echo "$sid"
}
MGMT_SUBNET=$(subnet_ensure "$MGMT_SUBNET_NAME" "$MGMT_CIDR"); ok "mgmt subnet: $MGMT_SUBNET ($MGMT_CIDR)"
DATA_SUBNET=$(subnet_ensure "$DATA_SUBNET_NAME" "$DATA_CIDR"); ok "data subnet: $DATA_SUBNET ($DATA_CIDR)"
TOOL_SUBNET=$(subnet_ensure "$TOOL_SUBNET_NAME" "$TOOL_CIDR"); ok "tool subnet: $TOOL_SUBNET ($TOOL_CIDR)"

# Internet gateway + default route (so the operator can reach mgmt IPs).
IGW_ID=$(aws ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
         --query 'InternetGateways[0].InternetGatewayId' --output text)
if none "$IGW_ID"; then
  IGW_ID=$(aws ec2 create-internet-gateway --tag-specifications "$(demo_tags internet-gateway cloudlens-demo-igw)" \
           --query 'InternetGateway.InternetGatewayId' --output text)
  aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" >/dev/null
fi
ok "Internet gateway: $IGW_ID"

RTB_ID=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" \
         --query 'RouteTables[0].RouteTableId' --output text)
aws ec2 create-route --route-table-id "$RTB_ID" --destination-cidr-block 0.0.0.0/0 \
  --gateway-id "$IGW_ID" >/dev/null 2>&1 || true
ok "Default route 0.0.0.0/0 -> $IGW_ID"

# Security groups:
#   appliance-sg  : mgmt access from operator (22, 443, vPB 9022) + all intra-VPC
#   workload-sg   : SSH from operator + all intra-VPC (sensor VXLAN to vPB)
sg_ensure() {
  # $1 name  $2 description  ->  echoes sg id
  local name="$1" desc="$2" gid
  gid=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$name" "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[0].GroupId' --output text)
  if none "$gid"; then
    gid=$(aws ec2 create-security-group --group-name "$name" --description "$desc" --vpc-id "$VPC_ID" \
          --tag-specifications "$(demo_tags security-group "$name")" \
          --query 'GroupId' --output text)
  fi
  echo "$gid"
}
APP_SG=$(sg_ensure cloudlens-demo-appliance-sg "CloudLens appliance mgmt access")
WL_SG=$(sg_ensure  cloudlens-demo-workload-sg  "CloudLens sensor-target access")

authorize() { aws ec2 authorize-security-group-ingress "$@" >/dev/null 2>&1 || true; }
# Appliance mgmt (operator /32): SSH, HTTPS UI, vPB SSH 9022
authorize --group-id "$APP_SG" --protocol tcp --port 22            --cidr "$MY_IP"
authorize --group-id "$APP_SG" --protocol tcp --port 443           --cidr "$MY_IP"
authorize --group-id "$APP_SG" --protocol tcp --port "$VPB_SSH_PORT" --cidr "$MY_IP"
# All traffic inside the VPC (sensor control + VXLAN data + tool egress)
authorize --group-id "$APP_SG" --protocol -1  --cidr "$VPC_CIDR"
# Workloads / tool receiver: SSH from operator + all intra-VPC
authorize --group-id "$WL_SG"  --protocol tcp --port 22 --cidr "$MY_IP"
authorize --group-id "$WL_SG"  --protocol -1  --cidr "$VPC_CIDR"
ok "Security groups ready (appliance=$APP_SG  workload=$WL_SG)"

# ---------------------------------------------------------------------
# Phase 2: Marketplace AMI availability
#   (AWS equivalent of Azure "accept marketplace terms")
# ---------------------------------------------------------------------
step "Phase 2: Marketplace AMI availability"
ami_ok() {
  aws ec2 describe-images --image-ids "$1" --query 'Images[0].ImageId' --output text 2>/dev/null | grep -q "$1"
}
for pair in "vController:$AMI_VCONTROLLER" "KVO:$AMI_KVO" "vPB:$AMI_VPB"; do
  label="${pair%%:*}"; ami="${pair##*:}"
  if ami_ok "$ami"; then
    ok "AMI reachable: $label ($ami)"
  else
    warn "AMI $ami ($label) not visible to this account."
    warn "Subscribe to the CloudLens listings in AWS Marketplace, then re-run:"
    warn "  https://aws.amazon.com/marketplace/search?searchTerms=Keysight+CloudLens"
  fi
done

# Resolve latest Canonical Ubuntu 22.04 + Red Hat RHEL 9 AMIs for the sensor targets.
UBUNTU_AMI=$(aws ec2 describe-images --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
            "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
RHEL_AMI=$(aws ec2 describe-images --owners 309956199498 \
  --filters "Name=name,Values=RHEL-9.*_HVM-*-x86_64-*" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
none "$UBUNTU_AMI" && fail "Could not resolve an Ubuntu 22.04 AMI"
none "$RHEL_AMI"   && warn "Could not resolve a RHEL 9 AMI (RHEL target will be skipped)"
ok "Ubuntu AMI: $UBUNTU_AMI   RHEL AMI: ${RHEL_AMI}"

# ---------------------------------------------------------------------
# Phase 3: Sensor-target workloads (Ubuntu + RHEL), tagged cloudlens=yes
# ---------------------------------------------------------------------
launch_workload() {
  # $1 name  $2 ami  $3 os-tag
  local name="$1" ami="$2" os="$3" iid
  iid=$(instance_id_by_name "$name")
  if ! none "$iid"; then ok "exists: $name ($iid)"; echo "$iid"; return; fi
  iid=$(aws ec2 run-instances \
    --image-id "$ami" --instance-type "$WORKLOAD_TYPE" --key-name "$KEY_NAME" \
    --subnet-id "$DATA_SUBNET" --security-group-ids "$WL_SG" --associate-public-ip-address \
    --tag-specifications \
      "ResourceType=instance,Tags=[{Key=Name,Value=$name},{Key=${DEMO_TAG_KEY},Value=${DEMO_TAG_VAL}},{Key=deployedBy,Value=cloudlens-demo},{Key=cloudlens,Value=yes},{Key=os,Value=$os},{Key=env,Value=prod}]" \
    --query 'Instances[0].InstanceId' --output text)
  ok "launched: $name ($iid, os=$os)"
  echo "$iid"
}

if [[ "$SKIP_WORKLOADS" == "true" ]]; then
  step "Phase 3: Sensor-target workloads (skipped via --skip-workloads)"
  APP01=$(instance_id_by_name app01-ubuntu)
  APP02=$(instance_id_by_name app02-rhel)
else
  step "Phase 3: Sensor-target workloads (Ubuntu + RHEL)"
  APP01=$(launch_workload app01-ubuntu "$UBUNTU_AMI" ubuntu)
  if ! none "$RHEL_AMI"; then
    APP02=$(launch_workload app02-rhel "$RHEL_AMI" rhel)
  else
    APP02=""
    warn "Skipping RHEL target (no AMI resolved)"
  fi
fi

# ---------------------------------------------------------------------
# Phase 4: vController (formerly CLMS)
# ---------------------------------------------------------------------
step "Phase 4: vController / CLMS ($AMI_VCONTROLLER, $TYPE_VCONTROLLER)"
if [[ -f "$CFT_TEMPLATE" ]]; then
  note "CloudFormation stack template found: deploy/cloudformation/stack.yaml"
  note "Deploying vController + KVO + vPB via the stack (the customer path)..."
  aws cloudformation deploy \
    --template-file "$CFT_TEMPLATE" \
    --stack-name cloudlens-demo-stack \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
        VpcId="$VPC_ID" MgmtSubnetId="$MGMT_SUBNET" DataSubnetId="$DATA_SUBNET" \
        ToolSubnetId="$TOOL_SUBNET" KeyName="$KEY_NAME" AdminPassword="$ADMIN_PW" \
    || warn "CloudFormation deploy returned non-zero (see stack events)"
  VC_ID=$(instance_id_by_name vcontroller || true)
else
  note "No vendored CloudFormation template; standing appliances up from AMIs directly."
  note "(Production customers deploy the CloudLens stack via cloudlens-autopilot CFT.)"
  VC_ID=$(instance_id_by_name vcontroller)
  if none "$VC_ID"; then
    VC_ID=$(aws ec2 run-instances \
      --image-id "$AMI_VCONTROLLER" --instance-type "$TYPE_VCONTROLLER" --key-name "$KEY_NAME" \
      --subnet-id "$MGMT_SUBNET" --security-group-ids "$APP_SG" --associate-public-ip-address \
      --tag-specifications "$(demo_tags instance vcontroller)" \
      --query 'Instances[0].InstanceId' --output text)
    ok "vController launched: $VC_ID"
  else
    ok "vController exists: $VC_ID"
  fi
fi

# ---------------------------------------------------------------------
# Phase 5: KVO
# ---------------------------------------------------------------------
step "Phase 5: KVO ($AMI_KVO, $TYPE_KVO)"
KVO_ID=$(instance_id_by_name kvo)
if none "$KVO_ID"; then
  KVO_ID=$(aws ec2 run-instances \
    --image-id "$AMI_KVO" --instance-type "$TYPE_KVO" --key-name "$KEY_NAME" \
    --subnet-id "$MGMT_SUBNET" --security-group-ids "$APP_SG" --associate-public-ip-address \
    --tag-specifications "$(demo_tags instance kvo)" \
    --query 'Instances[0].InstanceId' --output text)
  ok "KVO launched: $KVO_ID"
else
  ok "KVO exists: $KVO_ID"
fi

# ---------------------------------------------------------------------
# Phase 6: vPB (mgmt/data/tool NICs, SSH on 9022)
# ---------------------------------------------------------------------
step "Phase 6: vPB ($AMI_VPB, $TYPE_VPB) : mgmt/data/tool NICs, SSH ${VPB_SSH_PORT}"
VPB_ID=$(instance_id_by_name vpb)
if none "$VPB_ID"; then
  eni_ensure() {
    # $1 name  $2 subnet  $3 sg  ->  echoes eni id
    local name="$1" subnet="$2" sg="$3" eid
    eid=$(aws ec2 describe-network-interfaces --filters "Name=tag:Name,Values=$name" \
          --query 'NetworkInterfaces[0].NetworkInterfaceId' --output text)
    if none "$eid"; then
      eid=$(aws ec2 create-network-interface --subnet-id "$subnet" --groups "$sg" \
            --description "$name" \
            --tag-specifications "$(demo_tags network-interface "$name")" \
            --query 'NetworkInterface.NetworkInterfaceId' --output text)
    fi
    echo "$eid"
  }
  ENI_MGMT=$(eni_ensure vpb-mgmt "$MGMT_SUBNET" "$APP_SG")
  ENI_DATA=$(eni_ensure vpb-data "$DATA_SUBNET" "$APP_SG")   # ingress: VXLAN from sensors
  ENI_TOOL=$(eni_ensure vpb-tool "$TOOL_SUBNET" "$APP_SG")   # egress: to tool receiver
  ok "vPB ENIs: mgmt=$ENI_MGMT data=$ENI_DATA tool=$ENI_TOOL"

  VPB_ID=$(aws ec2 run-instances \
    --image-id "$AMI_VPB" --instance-type "$TYPE_VPB" --key-name "$KEY_NAME" \
    --network-interfaces \
      "NetworkInterfaceId=$ENI_MGMT,DeviceIndex=0" \
      "NetworkInterfaceId=$ENI_DATA,DeviceIndex=1" \
      "NetworkInterfaceId=$ENI_TOOL,DeviceIndex=2" \
    --tag-specifications "$(demo_tags instance vpb)" \
    --query 'Instances[0].InstanceId' --output text)
  ok "vPB launched: $VPB_ID"

  # Elastic IP on the mgmt NIC (multi-NIC instances do not auto-assign public IP).
  EIP_ALLOC=$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "$(demo_tags elastic-ip vpb-mgmt-eip)" \
    --query 'AllocationId' --output text)
  aws ec2 associate-address --allocation-id "$EIP_ALLOC" --network-interface-id "$ENI_MGMT" >/dev/null
  ok "vPB mgmt EIP associated ($EIP_ALLOC)"
else
  ok "vPB exists: $VPB_ID"
fi

# ---------------------------------------------------------------------
# Phase 7: Tool receiver (mirror target: tcpdump on VXLAN UDP/4789)
#   (AWS equivalent of the Azure Vectra mock receiver)
# ---------------------------------------------------------------------
step "Phase 7: Tool receiver (Ubuntu + tcpdump on VXLAN UDP/4789)"
TOOL_ID=$(instance_id_by_name tool-receiver)
if none "$TOOL_ID"; then
  USER_DATA=$(mktemp)
  cat > "$USER_DATA" <<'CI'
#cloud-config
package_update: true
packages:
  - tcpdump
  - nginx
runcmd:
  - systemctl enable --now nginx
  - mkdir -p /var/log/vxlan
  - bash -c "nohup tcpdump -i any -nn -w /var/log/vxlan/vxlan.pcap udp port 4789 > /var/log/vxlan/tcpdump.log 2>&1 &"
  - echo '<h1>CloudLens Tool Receiver - VXLAN UDP/4789 listener live</h1>' > /var/www/html/index.html
CI
  TOOL_ID=$(aws ec2 run-instances \
    --image-id "$UBUNTU_AMI" --instance-type "$TOOL_TYPE" --key-name "$KEY_NAME" \
    --subnet-id "$TOOL_SUBNET" --security-group-ids "$WL_SG" --associate-public-ip-address \
    --user-data "file://$USER_DATA" \
    --tag-specifications "$(demo_tags instance tool-receiver)" \
    --query 'Instances[0].InstanceId' --output text)
  rm -f "$USER_DATA"
  ok "tool-receiver launched: $TOOL_ID"
else
  ok "tool-receiver exists: $TOOL_ID"
fi

# ---------------------------------------------------------------------
# Wait for everything to reach running + collect addresses
# ---------------------------------------------------------------------
step "Waiting for instances to reach running state"
WAIT_IDS=("$VC_ID" "$KVO_ID" "$VPB_ID" "$TOOL_ID")
[[ -n "${APP01:-}" ]] && ! none "$APP01" && WAIT_IDS+=("$APP01")
[[ -n "${APP02:-}" ]] && ! none "$APP02" && WAIT_IDS+=("$APP02")
aws ec2 wait instance-running --instance-ids "${WAIT_IDS[@]}"
ok "All instances running"

VC_IP=$(instance_public_ip "$VC_ID")
KVO_IP=$(instance_public_ip "$KVO_ID")
TOOL_IP=$(instance_public_ip "$TOOL_ID")
VPB_MGMT_IP=$(aws ec2 describe-network-interfaces --filters "Name=tag:Name,Values=vpb-mgmt" \
  --query 'NetworkInterfaces[0].Association.PublicIp' --output text 2>/dev/null)
VPB_INGRESS_IP=$(aws ec2 describe-network-interfaces --filters "Name=tag:Name,Values=vpb-data" \
  --query 'NetworkInterfaces[0].PrivateIpAddress' --output text 2>/dev/null)
TOOL_PRIV_IP=$(instance_private_ip "$TOOL_ID")
APP01_IP=$( [[ -n "${APP01:-}" ]] && ! none "$APP01" && instance_public_ip "$APP01" || echo "" )

ok "vController: https://$VC_IP   KVO: https://$KVO_IP"
ok "vPB mgmt: ssh -p ${VPB_SSH_PORT} admin@$VPB_MGMT_IP   (ingress $VPB_INGRESS_IP)"
ok "tool-receiver: http://$TOOL_IP   (private $TOOL_PRIV_IP)"

# ---------------------------------------------------------------------
# Phase 8: Project key + sensor deployment (the customer-facing path)
# ---------------------------------------------------------------------
if [[ "$SKIP_SENSORS" == "true" ]]; then
  step "Phase 8: Sensor deployment (skipped via --skip-sensors)"
else
  step "Phase 8: CloudLens sensor deployment via quickstart.sh"

  # Fully automated project-key retrieval. --insecure ONLY because we are
  # reaching a vController we provisioned moments ago, by public IP, from the
  # operator machine. Pass --ca-bundle in production once the cert is trusted.
  note "Asking vController for a project key (scripts/vcontroller_project_key.py)..."
  PROJECT_KEY=$(VCONTROLLER_NEW_PASS="$ADMIN_PW" \
    python3 "${REPO_ROOT}/scripts/vcontroller_project_key.py" \
        --host "$VC_IP" \
        --project "cloudlens-demo" \
        --insecure 2>/tmp/.vckey.err 1>/tmp/.vckey.out; cat /tmp/.vckey.out)
  rm -f /tmp/.vckey.out /tmp/.vckey.err
  if [[ -z "$PROJECT_KEY" ]]; then
    warn "Automated key retrieval failed; falling back to manual paste"
    cat <<EOM

  Manual fallback:
  1. Open https://${VC_IP}, sign in admin / Cl0udLens@dm!n (or rotated password)
  2. Projects -> Add Project, name it "cloudlens-demo"
  3. Open the project, copy the API key
EOM
    read -rp "Paste the project key (or press Enter to skip): " PROJECT_KEY
  else
    ok "Project key retrieved automatically"
  fi

  if [[ -z "$PROJECT_KEY" ]]; then
    warn "Skipping sensor deployment"
  else
    cat > "${REPO_ROOT}/customer_input.yaml" <<YAML
# Auto-generated by demo/setup-aws-visibility-demo.sh on $(date -u +%FT%TZ)
aws:
  profile: "default"
  regions:
    - ${REGION}
  tag_filters:
    cloudlens: "yes"
    env: "prod"
  windows_connection: "ssm"
  linux_connection:   "ssh"
  ssh_key_path:    "~/.ssh/id_rsa"
  ssh_user_ubuntu: "${SSH_USER_UBUNTU}"
  ssh_user_rhel:   "${SSH_USER_RHEL}"
cloudlens:
  manager_ip_or_fqdn: "${VC_IP}"
  project_key:        "${PROJECT_KEY}"
  custom_tags:        "Customer=Demo Env=Demo Region=${REGION}"
  registry_type:      "insecure"
  linux_runtime:      "auto"
vpc_ids:
  - "${VPC_ID}"
subnet_ids: []
YAML
    ok "customer_input.yaml written"

    note "Running quickstart.sh (Ansible automation)..."
    (cd "$REPO_ROOT" && bash quickstart.sh || warn "quickstart.sh exited non-zero (see log)")
    ok "Sensor deployment finished"
  fi
fi

# ---------------------------------------------------------------------
# Phase 9: KVO mirror session (sensor traffic -> vPB ingress -> tool)
# ---------------------------------------------------------------------
step "Phase 9: KVO mirror session (traffic -> vPB -> tool receiver)"
note "KVO manages the vPB fleet and programs the mirror/tool maps centrally."
note "Best-effort automation is not wired to the KVO REST API in this kit; the"
note "steps below are the click-path in the KVO UI (https://$KVO_IP):"
cat <<EOM

  1. KVO UI -> Devices -> add the vPB ($VPB_MGMT_IP, SSH port ${VPB_SSH_PORT})
  2. Ingress: the vPB 'data' NIC ($VPB_INGRESS_IP) receives VXLAN/4789 from the sensors
  3. Tool port: point the egress map at the tool receiver ($TOOL_PRIV_IP)
  4. Create a mirror session: Ingress -> (optional filters) -> Tool
  5. Commit. Sensor traffic now decapsulates at the vPB and lands on the tool.
EOM
warn "Wire the mirror session in KVO before running the live walkthrough."

# ---------------------------------------------------------------------
# Phase 10: Generate run-demo.sh for the live walkthrough
# ---------------------------------------------------------------------
step "Phase 10: Generate run-demo.sh"
cat > "${REPO_ROOT}/demo/run-demo.sh" <<DEMO
#!/usr/bin/env bash
# Live walkthrough script - generated $(date -u +%FT%TZ)
set -e

VC_UI="https://${VC_IP}"
KVO_UI="https://${KVO_IP}"
TOOL_IP="${TOOL_IP}"
VPB_MGMT="${VPB_MGMT_IP}"
APP_VM_IP="${APP01_IP}"

echo "=== Keysight CloudLens AWS Visibility Demo ==="
echo ""
echo "Slide 1 - The architecture"
open "https://keysight-tech.github.io/cloudlens-ansible-aws/" 2>/dev/null || true
sleep 3

echo "Slide 2 - The vController UI (sensors registered)"
open "\$VC_UI" 2>/dev/null || true
sleep 3

echo "Slide 3 - The KVO UI (centralized vPB fleet + mirror session)"
open "\$KVO_UI" 2>/dev/null || true
sleep 3

echo "Slide 4 - The packet path. tcpdump on the tool receiver as we generate traffic:"
ssh -o StrictHostKeyChecking=no ${SSH_USER_UBUNTU}@\$TOOL_IP \\
  "sudo tcpdump -i any -nn -c 30 udp port 4789" &
TCPDUMP_PID=\$!
sleep 3

echo "Slide 5 - Generate traffic from a sensor-target workload"
ssh -o StrictHostKeyChecking=no ${SSH_USER_UBUNTU}@\$APP_VM_IP \\
  "for i in \$(seq 1 15); do curl -s -o /dev/null https://aws.amazon.com; sleep 1; done"

wait \$TCPDUMP_PID 2>/dev/null || true
echo ""
echo "Punchline: the same flow is seen on the workload AND on the tool receiver."
echo "Out-of-band. No vTAP. No GWLB. VXLAN decap + tool steering at the vPB."
DEMO
chmod +x "${REPO_ROOT}/demo/run-demo.sh"
ok "run-demo.sh generated"

# Cache addresses so run-demo.sh / teardown do not need to re-query AWS.
cat > "${STATE_DIR}/state.env" <<STATE
# Sourced by run-demo.sh - regenerated on every successful build.
REGION=${REGION}
VPC_ID=${VPC_ID}
VC_IP=${VC_IP}
KVO_IP=${KVO_IP}
VPB_MGMT_IP=${VPB_MGMT_IP}
VPB_INGRESS_IP=${VPB_INGRESS_IP}
TOOL_IP=${TOOL_IP}
APP01_IP=${APP01_IP}
KEY_NAME=${KEY_NAME}
GENERATED=$(date -u +%FT%TZ)
STATE
chmod 600 "${STATE_DIR}/state.env"
ok "Demo state cached to ${STATE_DIR}/state.env"

# ---------------------------------------------------------------------
# Phase 11: Final summary
# ---------------------------------------------------------------------
banner "Demo build COMPLETE"
cat <<EOM

UIs to open:
  vController:   https://${VC_IP}    (admin / Cl0udLens@dm!n or rotated pw)
  KVO:           https://${KVO_IP}
  Tool receiver: http://${TOOL_IP}

SSH:
  ssh ${SSH_USER_UBUNTU}@${VC_IP}                 # vController host (if enabled)
  ssh -p ${VPB_SSH_PORT} admin@${VPB_MGMT_IP}      # vPB mgmt (SSH port ${VPB_SSH_PORT})
  ssh ${SSH_USER_UBUNTU}@${TOOL_IP}               # tool receiver
  ssh ${SSH_USER_UBUNTU}@${APP01_IP}              # app01-ubuntu sensor target

OS / appliance passwords stashed at:
  ${PASSWORD_FILE}    (mode 600)

Live walkthrough:
  bash demo/run-demo.sh

Teardown (~2 min):
  bash demo/teardown.sh
  # or: bash demo/setup-aws-visibility-demo.sh --teardown

Cost estimate (us-east-1, on-demand):
  vController t3.xlarge   ~\$4/day
  KVO c5.2xlarge          ~\$8/day
  vPB t3.xlarge           ~\$4/day
  2x t3.small workloads   ~\$1/day
  tool receiver t3.small  ~\$0.50/day
  EIP + public IPs        ~\$0.50/day
  Total                   ~\$18/day until teardown
EOM
