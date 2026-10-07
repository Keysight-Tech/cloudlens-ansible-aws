#!/usr/bin/env bash
# A customer-shaped EXISTING environment for the brownfield proof.
#
# Builds what a customer already has before CloudLens shows up: a VPC with
# management, data and tool subnets in one zone (plus a second-zone subnet,
# because EKS insists on two), two tagged workloads talking HTTP to each other,
# and an EKS cluster running the same web + loadgen app the sample cluster
# uses. Nothing here is CloudLens. The deploy is then pointed at it with
# --existing-vpc-id / --existing-subnet-id / --existing-data-subnet-id /
# --existing-tool-subnet-id, --eks-cluster and the discovery tag.
#
#   bash scripts/lab/brownfield-fixture.sh create   [--admin-cidr A.B.C.D/32]
#   bash scripts/lab/brownfield-fixture.sh status
#   bash scripts/lab/brownfield-fixture.sh env      # the ids as shell exports
#   bash scripts/lab/brownfield-fixture.sh destroy  # only when the operator says so
#
# Every resource carries cloudlens:lab=<NAME> so status and destroy find it
# by tag, never by remembered id. Idempotent: create reuses what exists.
set -uo pipefail

NAME="${BROWNFIELD_NAME:-brownfield}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ZONE="${BROWNFIELD_ZONE:-${REGION}a}"
ZONE2="${BROWNFIELD_ZONE2:-${REGION}b}"
CIDR="${BROWNFIELD_CIDR:-10.60.0.0/16}"
KEY_NAME="${BROWNFIELD_KEY:-cloudlens-brown}"
TAG_KEY="${BROWNFIELD_TAG_KEY:-cloudlens}"
TAG_VALUE="${BROWNFIELD_TAG_VALUE:-yes}"
ADMIN_CIDR=""
NODE_TYPE="${BROWNFIELD_NODE_TYPE:-t3.medium}"
NODE_COUNT="${BROWNFIELD_NODE_COUNT:-2}"
WORKLOAD_TYPE="${BROWNFIELD_WORKLOAD_TYPE:-t3.micro}"
ACTION="${1:-status}"; shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin-cidr) ADMIN_CIDR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

AWS=(aws --region "$REGION")
LAB_TAG="cloudlens:lab"
say()  { printf '[%s] %s\n' "$1" "$2"; }
ok()   { say ok "$1"; }
note() { say ".." "$1"; }
fail() { say x "$1" >&2; exit "${2:-1}"; }
tags() { # tags NAME -> tag spec fragment for --tag-specifications
  printf 'Tags=[{Key=Name,Value=%s},{Key=%s,Value=%s}]' "$1" "$LAB_TAG" "$NAME"
}

# ---- lookups by tag ---------------------------------------------------
vpc_id()    { "${AWS[@]}" ec2 describe-vpcs --filters "Name=tag:${LAB_TAG},Values=${NAME}" --query 'Vpcs[0].VpcId' --output text 2>/dev/null | grep -v None; }
subnet_id() { "${AWS[@]}" ec2 describe-subnets --filters "Name=tag:${LAB_TAG},Values=${NAME}" "Name=tag:Name,Values=${NAME}-$1" --query 'Subnets[0].SubnetId' --output text 2>/dev/null | grep -v None; }
igw_id()    { "${AWS[@]}" ec2 describe-internet-gateways --filters "Name=tag:${LAB_TAG},Values=${NAME}" --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null | grep -v None; }
rtb_id()    { "${AWS[@]}" ec2 describe-route-tables --filters "Name=tag:${LAB_TAG},Values=${NAME}" --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null | grep -v None; }
sg_id()     { "${AWS[@]}" ec2 describe-security-groups --filters "Name=tag:${LAB_TAG},Values=${NAME}" "Name=group-name,Values=${NAME}-workloads" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v None; }
instance_id() { "${AWS[@]}" ec2 describe-instances --filters "Name=tag:Name,Values=${NAME}-$1" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -v None; }
ubuntu_ami() {
  "${AWS[@]}" ssm get-parameter --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id --query Parameter.Value --output text 2>/dev/null
}

# ---- create -------------------------------------------------------------
create_network() {
  local vpc igw rtb s
  vpc="$(vpc_id)"
  if [[ -z "$vpc" ]]; then
    vpc=$("${AWS[@]}" ec2 create-vpc --cidr-block "$CIDR" \
      --tag-specifications "ResourceType=vpc,$(tags "${NAME}-vpc")" --query Vpc.VpcId --output text) || fail "create-vpc"
    "${AWS[@]}" ec2 modify-vpc-attribute --vpc-id "$vpc" --enable-dns-hostnames >/dev/null
    "${AWS[@]}" ec2 modify-vpc-attribute --vpc-id "$vpc" --enable-dns-support >/dev/null
    ok "VPC ${vpc} (${CIDR})"
  else
    ok "VPC ${vpc} exists"
  fi
  igw="$(igw_id)"
  if [[ -z "$igw" ]]; then
    igw=$("${AWS[@]}" ec2 create-internet-gateway --tag-specifications "ResourceType=internet-gateway,$(tags "${NAME}-igw")" \
          --query InternetGateway.InternetGatewayId --output text) || fail "create-internet-gateway"
    "${AWS[@]}" ec2 attach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc" >/dev/null
    ok "IGW ${igw}"
  fi
  rtb="$(rtb_id)"
  if [[ -z "$rtb" ]]; then
    rtb=$("${AWS[@]}" ec2 create-route-table --vpc-id "$vpc" --tag-specifications "ResourceType=route-table,$(tags "${NAME}-rtb")" \
          --query RouteTable.RouteTableId --output text) || fail "create-route-table"
    "${AWS[@]}" ec2 create-route --route-table-id "$rtb" --destination-cidr-block 0.0.0.0/0 --gateway-id "$igw" >/dev/null
    ok "route table ${rtb} -> ${igw}"
  fi
  # one zone for the three CloudLens-facing subnets, a second zone for EKS
  local spec
  for spec in "mgmt:${CIDR%.*.*}.1.0/24:${ZONE}" "data:${CIDR%.*.*}.2.0/24:${ZONE}" "tool:${CIDR%.*.*}.3.0/24:${ZONE}" "az2:${CIDR%.*.*}.4.0/24:${ZONE2}"; do
    local role="${spec%%:*}" rest="${spec#*:}" cidr az
    cidr="${rest%%:*}"; az="${rest#*:}"
    s="$(subnet_id "$role")"
    if [[ -z "$s" ]]; then
      s=$("${AWS[@]}" ec2 create-subnet --vpc-id "$vpc" --cidr-block "$cidr" --availability-zone "$az" \
          --tag-specifications "ResourceType=subnet,$(tags "${NAME}-${role}")" --query Subnet.SubnetId --output text) || fail "create-subnet ${role}"
      "${AWS[@]}" ec2 associate-route-table --route-table-id "$rtb" --subnet-id "$s" >/dev/null
      # management and the EKS zone need public addresses (no NAT in this lab)
      if [[ "$role" == "mgmt" || "$role" == "az2" ]]; then
        "${AWS[@]}" ec2 modify-subnet-attribute --subnet-id "$s" --map-public-ip-on-launch >/dev/null
      fi
      ok "subnet ${role} ${s} (${cidr}, ${az})"
    fi
  done
}

create_key() {
  if "${AWS[@]}" ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    ok "key pair ${KEY_NAME} exists"
    [[ -f "$HOME/.ssh/${KEY_NAME}.pem" ]] || note "  (its .pem is not in ~/.ssh on this machine)"
    return 0
  fi
  mkdir -p "$HOME/.ssh"
  "${AWS[@]}" ec2 create-key-pair --key-name "$KEY_NAME" --key-type rsa \
    --tag-specifications "ResourceType=key-pair,$(tags "$KEY_NAME")" \
    --query KeyMaterial --output text > "$HOME/.ssh/${KEY_NAME}.pem" || fail "create-key-pair"
  chmod 400 "$HOME/.ssh/${KEY_NAME}.pem"
  ok "key pair ${KEY_NAME} -> ~/.ssh/${KEY_NAME}.pem"
}

create_workloads() {
  local vpc sg mgmt ami web client web_ip
  vpc="$(vpc_id)"; mgmt="$(subnet_id mgmt)"
  sg="$(sg_id)"
  if [[ -z "$sg" ]]; then
    sg=$("${AWS[@]}" ec2 create-security-group --group-name "${NAME}-workloads" --description "brownfield lab workloads" \
         --vpc-id "$vpc" --tag-specifications "ResourceType=security-group,$(tags "${NAME}-workloads")" \
         --query GroupId --output text) || fail "create-security-group"
    "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions \
      "IpProtocol=-1,IpRanges=[{CidrIp=${CIDR},Description=inside the VPC}]" >/dev/null
    if [[ -n "$ADMIN_CIDR" ]]; then
      "${AWS[@]}" ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions \
        "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${ADMIN_CIDR},Description=admin ssh}]" >/dev/null
    fi
    ok "workload SG ${sg}"
  fi
  ami="$(ubuntu_ami)"; [[ -n "$ami" ]] || fail "no Ubuntu 22.04 AMI from SSM"
  web="$(instance_id web)"
  if [[ -z "$web" ]]; then
    web=$("${AWS[@]}" ec2 run-instances --image-id "$ami" --instance-type "$WORKLOAD_TYPE" --key-name "$KEY_NAME" \
      --subnet-id "$mgmt" --security-group-ids "$sg" --associate-public-ip-address \
      --metadata-options HttpTokens=required \
      --user-data "$(printf '#!/bin/bash\nmkdir -p /srv/www && echo brownfield-web > /srv/www/index.html\ncd /srv/www && nohup python3 -m http.server 80 >/var/log/web.log 2>&1 &\n')" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}-web},{Key=${LAB_TAG},Value=${NAME}},{Key=${TAG_KEY},Value=${TAG_VALUE}},{Key=os,Value=ubuntu}]" \
      --query 'Instances[0].InstanceId' --output text) || fail "run-instances web"
    ok "workload web ${web}"
  fi
  web_ip=$("${AWS[@]}" ec2 describe-instances --instance-ids "$web" --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
  client="$(instance_id client)"
  if [[ -z "$client" ]]; then
    client=$("${AWS[@]}" ec2 run-instances --image-id "$ami" --instance-type "$WORKLOAD_TYPE" --key-name "$KEY_NAME" \
      --subnet-id "$mgmt" --security-group-ids "$sg" --associate-public-ip-address \
      --metadata-options HttpTokens=required \
      --user-data "$(printf '#!/bin/bash\nnohup bash -c "while true; do curl -s -m 2 http://%s/ >/dev/null; sleep 1; done" >/var/log/client.log 2>&1 &\n' "$web_ip")" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}-client},{Key=${LAB_TAG},Value=${NAME}},{Key=${TAG_KEY},Value=${TAG_VALUE}},{Key=os,Value=ubuntu}]" \
      --query 'Instances[0].InstanceId' --output text) || fail "run-instances client"
    ok "workload client ${client} -> http://${web_ip}/ every second"
  fi
}

create_eks() {
  local cluster="${NAME}-eks" account mgmt az2 crole nrole
  account=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
  mgmt="$(subnet_id mgmt)"; az2="$(subnet_id az2)"
  crole="${NAME}-eks-cluster-role"; nrole="${NAME}-eks-node-role"
  if ! "${AWS[@]}" iam get-role --role-name "$crole" >/dev/null 2>&1; then
    "${AWS[@]}" iam create-role --role-name "$crole" --tags "Key=${LAB_TAG},Value=${NAME}" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"eks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null || fail "cluster role"
    "${AWS[@]}" iam attach-role-policy --role-name "$crole" --policy-arn arn:aws:iam::aws:policy/AmazonEKSClusterPolicy
  fi
  if ! "${AWS[@]}" iam get-role --role-name "$nrole" >/dev/null 2>&1; then
    "${AWS[@]}" iam create-role --role-name "$nrole" --tags "Key=${LAB_TAG},Value=${NAME}" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null || fail "node role"
    local pol
    for pol in AmazonEKSWorkerNodePolicy AmazonEKS_CNI_Policy AmazonEC2ContainerRegistryReadOnly; do
      "${AWS[@]}" iam attach-role-policy --role-name "$nrole" --policy-arn "arn:aws:iam::aws:policy/${pol}"
    done
    sleep 10
  fi
  if ! "${AWS[@]}" eks describe-cluster --name "$cluster" >/dev/null 2>&1; then
    "${AWS[@]}" eks create-cluster --name "$cluster" --role-arn "arn:aws:iam::${account}:role/${crole}" \
      --resources-vpc-config "subnetIds=${mgmt},${az2}" --tags "${LAB_TAG}=${NAME}" >/dev/null || fail "eks create-cluster"
    ok "EKS ${cluster} creating (control plane 8-12 min)"
  fi
  "${AWS[@]}" eks wait cluster-active --name "$cluster" || fail "cluster never became active"
  ok "EKS ${cluster} active"
  if ! "${AWS[@]}" eks describe-nodegroup --cluster-name "$cluster" --nodegroup-name "${cluster}-nodes" >/dev/null 2>&1; then
    "${AWS[@]}" eks create-nodegroup --cluster-name "$cluster" --nodegroup-name "${cluster}-nodes" \
      --node-role "arn:aws:iam::${account}:role/${nrole}" --subnets "$mgmt" "$az2" \
      --instance-types "$NODE_TYPE" --scaling-config "minSize=1,maxSize=${NODE_COUNT},desiredSize=${NODE_COUNT}" \
      --tags "${LAB_TAG}=${NAME}" >/dev/null || fail "eks create-nodegroup"
  fi
  "${AWS[@]}" eks wait nodegroup-active --cluster-name "$cluster" --nodegroup-name "${cluster}-nodes" || fail "nodegroup never became active"
  ok "nodes ready"
  local kcfg="$HOME/.kube/${NAME}-eks"
  mkdir -p "$HOME/.kube"
  "${AWS[@]}" eks update-kubeconfig --name "$cluster" --kubeconfig "$kcfg" --alias "$cluster" >/dev/null || fail "update-kubeconfig"
  kubectl --kubeconfig "$kcfg" apply -f - <<'APP' >/dev/null || fail "sample app apply"
apiVersion: v1
kind: Namespace
metadata:
  name: cloudlens-demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: cloudlens-demo
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata:
      labels: {app: web}
    spec:
      containers:
        - name: nginx
          image: public.ecr.aws/nginx/nginx:stable
          ports: [{containerPort: 80}]
---
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: cloudlens-demo
spec:
  selector: {app: web}
  ports: [{port: 80}]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: loadgen
  namespace: cloudlens-demo
spec:
  replicas: 1
  selector: {matchLabels: {app: loadgen}}
  template:
    metadata:
      labels: {app: loadgen}
    spec:
      containers:
        - name: loadgen
          image: public.ecr.aws/docker/library/busybox:stable
          command: ["/bin/sh","-c","while true; do wget -q -O /dev/null http://web.cloudlens-demo.svc.cluster.local/; sleep 1; done"]
APP
  kubectl --kubeconfig "$kcfg" -n cloudlens-demo rollout status deploy/web --timeout=180s >/dev/null || note "web pods slow to start"
  kubectl --kubeconfig "$kcfg" -n cloudlens-demo rollout status deploy/loadgen --timeout=180s >/dev/null || note "loadgen slow to start"
  ok "sample app running on ${cluster} (loadgen -> web every second)"
}

# ---- status / env ---------------------------------------------------------
status() {
  local vpc; vpc="$(vpc_id)"
  [[ -n "$vpc" ]] || { note "no ${NAME} VPC in ${REGION}"; return 0; }
  echo "VPC            ${vpc}"
  local r; for r in mgmt data tool az2; do echo "subnet ${r}     $(subnet_id "$r")"; done
  echo "workload SG    $(sg_id)"
  local i; for i in web client; do
    echo "workload ${i}   $(instance_id "$i") $("${AWS[@]}" ec2 describe-instances --instance-ids "$(instance_id "$i")" --query 'Reservations[0].Instances[0].[State.Name,PrivateIpAddress,PublicIpAddress]' --output text 2>/dev/null)"
  done
  echo "EKS            ${NAME}-eks $("${AWS[@]}" eks describe-cluster --name "${NAME}-eks" --query cluster.status --output text 2>/dev/null)  nodegroup $("${AWS[@]}" eks describe-nodegroup --cluster-name "${NAME}-eks" --nodegroup-name "${NAME}-eks-nodes" --query nodegroup.status --output text 2>/dev/null)"
}
env_out() {
  cat <<EOF
export BROWN_VPC=$(vpc_id)
export BROWN_MGMT=$(subnet_id mgmt)
export BROWN_DATA=$(subnet_id data)
export BROWN_TOOL=$(subnet_id tool)
export BROWN_AZ2=$(subnet_id az2)
export BROWN_EKS=${NAME}-eks
export BROWN_KEY=${KEY_NAME}
export BROWN_ZONE=${ZONE}
EOF
}

# ---- destroy ---------------------------------------------------------------
destroy() {
  local cluster="${NAME}-eks" ids s r
  if "${AWS[@]}" eks describe-cluster --name "$cluster" >/dev/null 2>&1; then
    "${AWS[@]}" eks delete-nodegroup --cluster-name "$cluster" --nodegroup-name "${cluster}-nodes" >/dev/null 2>&1 || true
    note "nodegroup deleting"; "${AWS[@]}" eks wait nodegroup-deleted --cluster-name "$cluster" --nodegroup-name "${cluster}-nodes" 2>/dev/null || true
    "${AWS[@]}" eks delete-cluster --name "$cluster" >/dev/null 2>&1 || true
    note "cluster deleting"; "${AWS[@]}" eks wait cluster-deleted --name "$cluster" 2>/dev/null || true
    ok "EKS ${cluster} gone"
  fi
  for r in "${NAME}-eks-cluster-role" "${NAME}-eks-node-role"; do
    for p in $("${AWS[@]}" iam list-attached-role-policies --role-name "$r" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
      "${AWS[@]}" iam detach-role-policy --role-name "$r" --policy-arn "$p" 2>/dev/null || true
    done
    "${AWS[@]}" iam delete-role --role-name "$r" 2>/dev/null && ok "role ${r} gone"
  done
  ids=$("${AWS[@]}" ec2 describe-instances --filters "Name=tag:${LAB_TAG},Values=${NAME}" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)
  if [[ -n "$ids" ]]; then
    "${AWS[@]}" ec2 terminate-instances --instance-ids $ids >/dev/null; note "terminating ${ids}"
    "${AWS[@]}" ec2 wait instance-terminated --instance-ids $ids; ok "workloads gone"
  fi
  # EKS leaves ENIs and its own security groups behind for a while
  local vpc; vpc="$(vpc_id)"; [[ -n "$vpc" ]] || return 0
  for s in $("${AWS[@]}" ec2 describe-network-interfaces --filters "Name=vpc-id,Values=${vpc}" --query 'NetworkInterfaces[?Status==`available`].NetworkInterfaceId' --output text); do
    "${AWS[@]}" ec2 delete-network-interface --network-interface-id "$s" 2>/dev/null || true
  done
  for s in $("${AWS[@]}" ec2 describe-security-groups --filters "Name=vpc-id,Values=${vpc}" --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text); do
    "${AWS[@]}" ec2 delete-security-group --group-id "$s" 2>/dev/null || true
  done
  for s in $("${AWS[@]}" ec2 describe-subnets --filters "Name=vpc-id,Values=${vpc}" --query 'Subnets[].SubnetId' --output text); do
    "${AWS[@]}" ec2 delete-subnet --subnet-id "$s" 2>/dev/null || note "subnet ${s} still in use"
  done
  r="$(rtb_id)"; [[ -n "$r" ]] && "${AWS[@]}" ec2 delete-route-table --route-table-id "$r" 2>/dev/null
  s="$(igw_id)"
  if [[ -n "$s" ]]; then
    "${AWS[@]}" ec2 detach-internet-gateway --internet-gateway-id "$s" --vpc-id "$vpc" 2>/dev/null
    "${AWS[@]}" ec2 delete-internet-gateway --internet-gateway-id "$s" 2>/dev/null
  fi
  "${AWS[@]}" ec2 delete-vpc --vpc-id "$vpc" 2>/dev/null && ok "VPC ${vpc} gone" || note "VPC ${vpc} still has dependencies; re-run destroy in a minute"
}

case "$ACTION" in
  create)  create_network; create_key; create_workloads; create_eks; status ;;
  status)  status ;;
  env)     env_out ;;
  destroy) destroy ;;
  *) echo "usage: $0 create|status|env|destroy [--admin-cidr CIDR]" >&2; exit 2 ;;
esac
