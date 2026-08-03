#!/usr/bin/env bash
# =====================================================================
# kvo_collector_bootstrap_fix.sh
#
# Work around a KVO Zone-Tapping defect: KVO deploys the AWS collector
# Service VMs (the ASG it creates for a cloud collection) with EMPTY
# launch-template user-data. With no bootstrap the SVM comes up with
#   * /var/lib/kcos/apps/packetstack/metadata.json  capabilities.KVO=false
#     -> the KVO control API (gunicorn :8444) exits immediately, and
#   * vpb.db `kvo` table  host=NULL enable=0
#     -> the collector never announces to KVO,
# so KVO never adopts the collector and never creates the VPC Traffic
# Mirror SESSIONS (you get a target + filter but sessions stays 0).
#
# This injects a self-bootstrap cloud-config into the collector launch
# template so every collector KVO launches sets both and restarts the
# vpbsystem pod, then (optionally) replaces the running collector so the
# fix takes effect now. Idempotent; safe to re-run.
#
# Usage:
#   scripts/kvo_collector_bootstrap_fix.sh --kvo-ip 10.99.1.63 --vpc vpc-0f19... \
#       [--region us-east-1] [--replace]
# =====================================================================
set -euo pipefail

REGION="us-east-1"; KVO_IP=""; VPC=""; REPLACE="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --kvo-ip) KVO_IP="$2"; shift 2;;
    --vpc) VPC="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --replace) REPLACE="yes"; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$KVO_IP" ] && [ -n "$VPC" ] || { echo "need --kvo-ip and --vpc" >&2; exit 2; }

ASG="cloudlens.collector.${VPC}.${REGION}a"
LT=$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names "$ASG" \
  --query 'AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateId' \
  --output text)
[ -n "$LT" ] && [ "$LT" != "None" ] || { echo "no collector ASG/LT for $ASG" >&2; exit 3; }
echo "[fix] ASG=$ASG LT=$LT KVO_IP=$KVO_IP"

UD=$(cat <<CLOUDCONFIG | base64 | tr -d '\n'
#cloud-config
write_files:
  - path: /usr/local/bin/kvo-collector-bootstrap.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      set -u
      KVO_IP="${KVO_IP}"
      MF=/var/lib/kcos/apps/packetstack/metadata.json
      DB=/var/lib/kcos/apps/packetstack/config/vpb.db
      for i in \$(seq 1 90); do [ -f "\$MF" ] && [ -f "\$DB" ] && break; sleep 10; done
      [ -f "\$MF" ] || exit 0
      changed=0
      if [ "\$(python3 -c "import json;print(json.load(open('\$MF'))['capabilities'].get('KVO'))" 2>/dev/null)" != "True" ]; then
        python3 -c "import json;d=json.load(open('\$MF'));d['capabilities']['KVO']=True;json.dump(d,open('\$MF','w'),indent=2)"; changed=1
      fi
      if [ -f "\$DB" ]; then
        cur=\$(python3 -c "import sqlite3;print(list(sqlite3.connect('\$DB').execute('select host,enable from kvo')))" 2>/dev/null)
        if ! echo "\$cur" | grep -q "'\$KVO_IP', 1"; then
          python3 -c "import sqlite3;c=sqlite3.connect('\$DB');c.execute(\"update kvo set host='\$KVO_IP',port=443,enable=1 where id=1\");c.commit()"; changed=1
        fi
      fi
      if [ "\$changed" = "1" ]; then
        POD=\$(crictl ps --name vpbsystem -q 2>/dev/null | head -1)
        [ -n "\$POD" ] && crictl stop "\$POD"
      fi
  - path: /etc/systemd/system/kvo-collector-bootstrap.service
    permissions: '0644'
    content: |
      [Unit]
      Description=CloudLens collector self-bootstrap into KVO
      After=network-online.target
      Wants=network-online.target
      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/kvo-collector-bootstrap.sh
      RemainAfterExit=yes
      [Install]
      WantedBy=multi-user.target
runcmd:
  - systemctl daemon-reload
  - systemctl enable --now kvo-collector-bootstrap.service
CLOUDCONFIG
)

VER=$(aws ec2 create-launch-template-version --region "$REGION" \
  --launch-template-id "$LT" --source-version '$Default' \
  --launch-template-data "{\"UserData\":\"$UD\"}" \
  --query 'LaunchTemplateVersion.VersionNumber' --output text)
aws ec2 modify-launch-template --region "$REGION" --launch-template-id "$LT" \
  --default-version "$VER" >/dev/null
aws autoscaling update-auto-scaling-group --region "$REGION" --auto-scaling-group-name "$ASG" \
  --mixed-instances-policy "{\"LaunchTemplate\":{\"LaunchTemplateSpecification\":{\"LaunchTemplateId\":\"$LT\",\"Version\":\"\$Default\"}}}"
echo "[fix] launch template v$VER set as default; ASG pinned to \$Default"

if [ "$REPLACE" = "yes" ]; then
  IID=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=cloudlens.collector.${VPC}" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  if [ -n "$IID" ] && [ "$IID" != "None" ]; then
    aws autoscaling terminate-instance-in-auto-scaling-group --region "$REGION" \
      --instance-id "$IID" --no-should-decrement-desired-capacity >/dev/null
    echo "[fix] replaced running collector $IID; ASG will relaunch it self-bootstrapped"
  fi
fi
echo "[fix] done. New collectors self-register to KVO; sessions appear once adopted."
