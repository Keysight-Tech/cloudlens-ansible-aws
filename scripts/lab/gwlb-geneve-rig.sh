#!/usr/bin/env bash
# scripts/lab/gwlb-geneve-rig.sh: a small, real Gateway Load Balancer rig so a
# mirrored interface carries GENEVE exactly as a firewall behind a GWLB would.
#
# Why a real GWLB and not a Linux geneve tunnel: the GWLB adds its own GENEVE
# TLV options (flow cookie, endpoint id) that a plain `ip link add type geneve`
# never sends. If the vPB's GENEVE stripping has to cope with those options,
# only a real GWLB proves it.
#
# What it builds, all inside ONE existing VPC, all tagged cloudlens:rig=geneve:
#   appliance   t3.micro, Amazon Linux, answers the GWLB health check on TCP 80,
#               tagged cloudlens=yes so the KVO mirror policy picks its interface.
#               It never forwards anything; the point is what arrives on its ENI.
#   target group (GENEVE 6081, health check HTTP:80) + the GWLB in the appliance subnet
#   endpoint service + a GatewayLoadBalancer VPC endpoint in its own small subnet
#   client      t3.micro in its own subnet whose route table sends 198.51.100.0/24
#               (TEST-NET-2, nothing real) through the endpoint; user-data curls
#               198.51.100.10 every two seconds forever. No access to it is needed.
#
# Usage:
#   scripts/lab/gwlb-geneve-rig.sh create  --region R --vpc vpc-... --appliance-subnet subnet-... \
#        --cidr-endpoint 10.99.250.0/24 --cidr-client 10.99.251.0/24 [--tag-key cloudlens --tag-value yes]
#   scripts/lab/gwlb-geneve-rig.sh destroy --region R --state rig.env
#   scripts/lab/gwlb-geneve-rig.sh status  --region R --state rig.env
# Writes rig.env (ids) in the working directory. Destroy deletes ONLY the ids in
# that file, in reverse order, and refuses anything not carrying the rig tag.
set -euo pipefail
ACTION="${1:-}"; shift || true
REGION=us-east-1; VPC=""; APP_SUBNET=""; CIDR_EP="10.99.250.0/24"; CIDR_CL="10.99.251.0/24"
TAG_KEY=cloudlens; TAG_VALUE=yes; STATE=rig.env
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;; --vpc) VPC="$2"; shift 2 ;; --appliance-subnet) APP_SUBNET="$2"; shift 2 ;;
    --cidr-endpoint) CIDR_EP="$2"; shift 2 ;; --cidr-client) CIDR_CL="$2"; shift 2 ;;
    --tag-key) TAG_KEY="$2"; shift 2 ;; --tag-value) TAG_VALUE="$2"; shift 2 ;; --state) STATE="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
RIG='{Key=cloudlens:rig,Value=geneve}'
aws_() { aws --region "$REGION" --output text "$@"; }
say() { echo "[rig] $*" >&2; }

create() {
  [[ -n "$VPC" && -n "$APP_SUBNET" ]] || { echo "create needs --vpc and --appliance-subnet" >&2; exit 2; }
  local az ami sg app tg tglb lb lbarn svc ep sub_ep sub_cl rt cl
  az=$(aws_ ec2 describe-subnets --subnet-ids "$APP_SUBNET" --query 'Subnets[0].AvailabilityZone')
  ami=$(aws_ ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value)
  say "VPC $VPC, appliance subnet $APP_SUBNET ($az), AMI $ami"
  sg=$(aws_ ec2 create-security-group --group-name "cloudlens-geneve-rig" --description "GENEVE rig: health check and GENEVE from the VPC" --vpc-id "$VPC" \
        --tag-specifications "ResourceType=security-group,Tags=[$RIG]" --query GroupId)
  local vpccidr; vpccidr=$(aws_ ec2 describe-vpcs --vpc-ids "$VPC" --query 'Vpcs[0].CidrBlock')
  aws_ ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions \
    "IpProtocol=udp,FromPort=6081,ToPort=6081,IpRanges=[{CidrIp=$vpccidr}]" "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=$vpccidr}]" >/dev/null
  app=$(aws_ ec2 run-instances --image-id "$ami" --instance-type t3.micro --subnet-id "$APP_SUBNET" --security-group-ids "$sg" --no-associate-public-ip-address \
        --user-data '#!/bin/bash
mkdir -p /srv/hc && echo ok > /srv/hc/index.html && cd /srv/hc && nohup python3 -m http.server 80 >/var/log/hc.log 2>&1 &' \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=geneve-appliance},{Key=$TAG_KEY,Value=$TAG_VALUE},$RIG]" --query 'Instances[0].InstanceId')
  say "appliance $app (tagged $TAG_KEY=$TAG_VALUE: the mirror policy will pick it)"
  tg=$(aws_ elbv2 create-target-group --name cloudlens-geneve-rig --protocol GENEVE --port 6081 --vpc-id "$VPC" --target-type instance \
        --health-check-protocol HTTP --health-check-port 80 --health-check-path / --tags "Key=cloudlens:rig,Value=geneve" --query 'TargetGroups[0].TargetGroupArn')
  lbarn=$(aws_ elbv2 create-load-balancer --name cloudlens-geneve-rig --type gateway --subnets "$APP_SUBNET" --tags "Key=cloudlens:rig,Value=geneve" --query 'LoadBalancers[0].LoadBalancerArn')
  aws_ elbv2 create-listener --load-balancer-arn "$lbarn" --default-actions "Type=forward,TargetGroupArn=$tg" --query 'Listeners[0].ListenerArn' >/dev/null
  aws_ ec2 wait instance-running --instance-ids "$app"
  aws_ elbv2 register-targets --target-group-arn "$tg" --targets "Id=$app"
  say "GWLB $lbarn, target group $tg; waiting for it to become active"
  aws_ elbv2 wait load-balancer-available --load-balancer-arns "$lbarn"
  svc=$(aws_ ec2 create-vpc-endpoint-service-configuration --gateway-load-balancer-arns "$lbarn" --no-acceptance-required \
        --tag-specifications "ResourceType=vpc-endpoint-service,Tags=[$RIG]" --query 'ServiceConfiguration.ServiceName')
  sub_ep=$(aws_ ec2 create-subnet --vpc-id "$VPC" --cidr-block "$CIDR_EP" --availability-zone "$az" --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=rig-endpoint},$RIG]" --query Subnet.SubnetId)
  sub_cl=$(aws_ ec2 create-subnet --vpc-id "$VPC" --cidr-block "$CIDR_CL" --availability-zone "$az" --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=rig-client},$RIG]" --query Subnet.SubnetId)
  ep=$(aws_ ec2 create-vpc-endpoint --vpc-endpoint-type GatewayLoadBalancer --vpc-id "$VPC" --service-name "$svc" --subnet-ids "$sub_ep" \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[$RIG]" --query 'VpcEndpoint.VpcEndpointId')
  say "endpoint service $svc, endpoint $ep; waiting for it to become available"
  for _ in $(seq 1 40); do
    [[ "$(aws_ ec2 describe-vpc-endpoints --vpc-endpoint-ids "$ep" --query 'VpcEndpoints[0].State')" == "available" ]] && break; sleep 10
  done
  rt=$(aws_ ec2 create-route-table --vpc-id "$VPC" --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=rig-client},$RIG]" --query RouteTable.RouteTableId)
  aws_ ec2 create-route --route-table-id "$rt" --destination-cidr-block 198.51.100.0/24 --vpc-endpoint-id "$ep" >/dev/null
  aws_ ec2 associate-route-table --route-table-id "$rt" --subnet-id "$sub_cl" --query AssociationId >/dev/null
  cl=$(aws_ ec2 run-instances --image-id "$ami" --instance-type t3.micro --subnet-id "$sub_cl" --security-group-ids "$sg" --no-associate-public-ip-address \
        --user-data '#!/bin/bash
nohup bash -c "while true; do curl -s -m 2 http://198.51.100.10/ >/dev/null 2>&1; echo x | timeout 2 nc -u -w1 198.51.100.10 53 >/dev/null 2>&1; sleep 2; done" >/var/log/gen.log 2>&1 &' \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=geneve-client},$RIG]" --query 'Instances[0].InstanceId')
  say "client $cl sends to 198.51.100.10 every 2 s through the endpoint"
  cat > "$STATE" <<EOF
RIG_REGION=$REGION
RIG_VPC=$VPC
RIG_SG=$sg
RIG_APPLIANCE=$app
RIG_CLIENT=$cl
RIG_TG=$tg
RIG_LB=$lbarn
RIG_SVC=$svc
RIG_EP=$ep
RIG_SUB_EP=$sub_ep
RIG_SUB_CL=$sub_cl
RIG_RT=$rt
EOF
  say "state written to $STATE"
  status
}

status() {
  # shellcheck disable=SC1090
  . "$STATE"
  echo "appliance $RIG_APPLIANCE: $(aws_ ec2 describe-instances --instance-ids "$RIG_APPLIANCE" --query 'Reservations[0].Instances[0].State.Name')"
  echo "target health: $(aws_ elbv2 describe-target-health --target-group-arn "$RIG_TG" --query 'TargetHealthDescriptions[0].TargetHealth.State')"
  echo "endpoint $RIG_EP: $(aws_ ec2 describe-vpc-endpoints --vpc-endpoint-ids "$RIG_EP" --query 'VpcEndpoints[0].State')"
  echo "appliance ENI: $(aws_ ec2 describe-instances --instance-ids "$RIG_APPLIANCE" --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId')"
  echo "mirror sessions on it: $(aws_ ec2 describe-traffic-mirror-sessions --filters "Name=network-interface-id,Values=$(aws_ ec2 describe-instances --instance-ids "$RIG_APPLIANCE" --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId')" --query 'length(TrafficMirrorSessions)')"
}

destroy() {
  # shellcheck disable=SC1090
  . "$STATE"
  local check
  check=$(aws_ ec2 describe-instances --instance-ids "$RIG_APPLIANCE" "$RIG_CLIENT" --query 'Reservations[].Instances[].Tags[?Key==`cloudlens:rig`].Value' | tr '\t' ' ')
  [[ "$check" == *geneve* ]] || { echo "refusing: instances in $STATE do not carry the rig tag" >&2; exit 3; }
  aws_ ec2 terminate-instances --instance-ids "$RIG_APPLIANCE" "$RIG_CLIENT" >/dev/null; say "terminating instances"
  aws_ ec2 delete-route-table --route-table-id "$RIG_RT" 2>/dev/null || { aws_ ec2 disassociate-route-table --association-id "$(aws_ ec2 describe-route-tables --route-table-ids "$RIG_RT" --query 'RouteTables[0].Associations[0].RouteTableAssociationId')" >/dev/null; aws_ ec2 delete-route-table --route-table-id "$RIG_RT"; }
  aws_ ec2 delete-vpc-endpoints --vpc-endpoint-ids "$RIG_EP" >/dev/null; say "endpoint deleting"
  for _ in $(seq 1 30); do aws_ ec2 describe-vpc-endpoints --vpc-endpoint-ids "$RIG_EP" >/dev/null 2>&1 || break; sleep 10; done
  aws_ ec2 delete-vpc-endpoint-service-configurations --service-ids "$(aws_ ec2 describe-vpc-endpoint-service-configurations --filters "Name=service-name,Values=$RIG_SVC" --query 'ServiceConfigurations[0].ServiceId')" >/dev/null
  aws_ elbv2 delete-load-balancer --load-balancer-arn "$RIG_LB"; sleep 20
  aws_ elbv2 delete-target-group --target-group-arn "$RIG_TG"
  aws_ ec2 wait instance-terminated --instance-ids "$RIG_APPLIANCE" "$RIG_CLIENT"
  aws_ ec2 delete-subnet --subnet-id "$RIG_SUB_EP"; aws_ ec2 delete-subnet --subnet-id "$RIG_SUB_CL"
  aws_ ec2 delete-security-group --group-id "$RIG_SG"
  say "rig removed; remaining with the rig tag: $(aws_ ec2 describe-instances --filters Name=tag:cloudlens:rig,Values=geneve Name=instance-state-name,Values=running,pending,stopped --query 'length(Reservations[].Instances[])')"
}

case "$ACTION" in
  create) create ;; status) status ;; destroy) destroy ;;
  *) echo "usage: $0 create|status|destroy [options]" >&2; exit 2 ;;
esac
