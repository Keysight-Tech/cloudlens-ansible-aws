#!/usr/bin/env bash
# Generate traffic on the tapped workloads and prove it reaches the tool.
#
# This is the answer to "is it actually working?", and it answers it with
# packet counts rather than a dashboard status. Every check here is READ-ONLY
# against the deployment: it makes traffic and reads counters, and changes no
# configuration anywhere. Safe to run on a live stack, as often as you like.
#
# What it measures, in the order the packets travel:
#
#   workload -> AWS mirror session -> collector -> GRE -> tool     (key 64)
#   workload -> ... -> collector -> GRE -> vPB -> egress -> tool   (key 200)
#
# Both land on the SAME host. The GRE key is the only thing that tells them
# apart, which is why they are deliberately different: the collector's raw
# mirror arrives with CLOUDLENS_GRE_KEY, the vPB's processed output with
# CLOUDLENS_EGRESS_GRE_KEY. Seeing both is the end-to-end proof.
#
# Usage:
#   scripts/prove_traffic.sh --vpc-id vpc-0abc --key ~/.ssh/cloudlens-key.pem
#   scripts/prove_traffic.sh --stack-name cloudlens-stack        # finds the VPC
#
set -uo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
VPC_ID=""; STACK_NAME=""; KEY_PEM=""; DURATION=25
GRE_KEY_COLLECTOR="${CLOUDLENS_GRE_KEY:-64}"
GRE_KEY_VPB="${CLOUDLENS_EGRESS_GRE_KEY:-200}"
PING_SIZE=1337   # distinctive on purpose: easy to pick out of real traffic

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)      REGION="$2"; shift 2 ;;
    --vpc-id)      VPC_ID="$2"; shift 2 ;;
    --stack-name)  STACK_NAME="$2"; shift 2 ;;
    --key)         KEY_PEM="$2"; shift 2 ;;
    --duration)    DURATION="$2"; shift 2 ;;
    --gre-key)     GRE_KEY_COLLECTOR="$2"; shift 2 ;;
    --egress-gre-key) GRE_KEY_VPB="$2"; shift 2 ;;
    -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
head2() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }

AWSQ=(aws --region "$REGION")
SSH=(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
     -o ConnectTimeout=15 -o BatchMode=yes -o LogLevel=ERROR)

# --- can we talk to AWS at all? ----------------------------------------------
# Ask BEFORE any describe-* call. Without this, an expired SSO token makes every
# query return nothing and the script blames the stack: "no capture host in this
# VPC" for a deployment that is running perfectly. Seen exactly that, and it
# sends you looking for a host that was never missing.
head2 "preflight"
if ! CALLER="$(aws sts get-caller-identity --query Arn --output text 2>&1)"; then
  fail "AWS is not usable, so nothing below could be measured."
  say "  $(printf '%s' "$CALLER" | tail -1)"
  say ""
  say "  This says nothing about your deployment. Refresh credentials and re-run:"
  say "    aws sso login --profile \${AWS_PROFILE:-<your-profile>}"
  say "  Then confirm with:  aws sts get-caller-identity"
  exit 2
fi
say "  aws identity   $CALLER"

# --- locate the pieces -------------------------------------------------------
if [[ -z "$VPC_ID" && -n "$STACK_NAME" ]]; then
  VPC_ID="$("${AWSQ[@]}" ec2 describe-instances \
    --filters "Name=tag:Name,Values=${STACK_NAME}-vpb" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].VpcId' --output text 2>/dev/null)"
fi
if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
  echo "Need --vpc-id (or a --stack-name whose vPB is running) to know which stack to test." >&2
  exit 2
fi

inst() {  # inst <filter-name> <filter-values> <field>
  "${AWSQ[@]}" ec2 describe-instances \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=instance-state-name,Values=running" "Name=$1,Values=$2" \
    --query "Reservations[0].Instances[0].$3" --output text 2>/dev/null
}

TOOL_PUB="$(inst tag:cloudlens-role tool-receiver PublicIpAddress)"
TOOL_PRIV="$(inst tag:cloudlens-role tool-receiver PrivateIpAddress)"
VPB_PUB="$(inst tag:Name '*vpb*' PublicIpAddress)"

# Tapped workloads: the same tag the mirror sessions are cut from.
# Read with a while loop, NOT mapfile: macOS ships bash 3.2, which has no
# mapfile, and a Mac is exactly where this gets run.
WORKLOADS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && WORKLOADS+=("$line")
done < <("${AWSQ[@]}" ec2 describe-instances \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=instance-state-name,Values=running" \
            "Name=tag:cloudlens,Values=yes" \
  --query 'Reservations[].Instances[].[PrivateIpAddress,PublicIpAddress,Tags[?Key==`Name`]|[0].Value,Platform]' \
  --output text 2>/dev/null)

[[ -z "$KEY_PEM" ]] && KEY_PEM="$(inst tag:cloudlens-role tool-receiver KeyName)" \
                    && KEY_PEM="$HOME/.ssh/${KEY_PEM}.pem"

head2 "stack under test"
say "  region         $REGION"
say "  vpc            $VPC_ID"
say "  tool (capture) ${TOOL_PRIV:-?}   public ${TOOL_PUB:-none}"
say "  vPB            ${VPB_PUB:-none}"
say "  ssh key        $KEY_PEM"
say "  tapped         ${#WORKLOADS[@]} workload(s) tagged cloudlens=yes"

if [[ ! -r "$KEY_PEM" ]]; then
  fail "cannot read $KEY_PEM"
  say "  Pass the right one with --key. AWS never returns a private key, so if it"
  say "  is not on this machine there is no way to recover it: redeploy with a new"
  say "  key pair, or run this from the machine that created the stack."
  exit 3
fi
if [[ -z "$TOOL_PUB" || "$TOOL_PUB" == "None" ]]; then
  fail "no capture host (tag cloudlens-role=tool-receiver) with a public IP in this VPC"
  say "  Without a tool there is nowhere for packets to land and nothing to prove."
  exit 3
fi

# --- are there mirror sessions at all? ---------------------------------------
# Check FIRST, because with zero sessions nothing is being copied and every
# measurement below is guaranteed to read zero. That is not a broken vPB and it
# is not a broken deployment: AWS cuts no session until the Cloud Collection is
# re-committed in KVO after the collector registers its mirror target. Saying
# "no traffic" without saying that sends people debugging the wrong thing.
SESSIONS="$("${AWSQ[@]}" ec2 describe-traffic-mirror-sessions \
  --query 'length(TrafficMirrorSessions)' --output text 2>/dev/null || echo 0)"
say "  mirror sessions ${SESSIONS:-0}"
if [[ "${SESSIONS:-0}" == "0" ]]; then
  head2 "nothing to measure yet"
  fail "AWS has 0 traffic mirror sessions: no traffic is being copied."
  say ""
  say "  This is expected right after a deploy, and it is the one step that is"
  say "  manual. In KVO, edit the Cloud Collection and re-commit it as ONE change"
  say "  request. AWS cuts one session per tagged workload within about a minute."
  say ""
  say "  Watch them appear:"
  say "    aws ec2 describe-traffic-mirror-sessions --region ${REGION} --query 'length(TrafficMirrorSessions)'"
  say ""
  say "  Then run this again. It changes no configuration, so repeat it freely."
  exit 5
fi

# --- vPB counters BEFORE -----------------------------------------------------
vpb_counters() {
  [[ -z "$VPB_PUB" || "$VPB_PUB" == "None" ]] && return 1
  # Columns: Name | Precedence | Inspected Packets | Inspected Bytes |
  #          Passed Packets | Passed Bytes | Denied Packets | Denied Bytes
  # Fields 3 and 5 are PACKETS. Reading 4 and 6 gives bytes, which look like
  # plausible counters and are silently the wrong number.
  "${SSH[@]}" -i "$KEY_PEM" -p 9022 "admin@${VPB_PUB}" \
    "sudo vpb -c 'show traffic-rule-packet-counters'" 2>/dev/null \
    | awk -F'|' '/^TR/ {gsub(/ /,"",$3); gsub(/ /,"",$5); print $3, $5}' | tail -1
}
BEFORE="$(vpb_counters)"

# --- generate traffic, capture at the tool, at the same time -----------------
head2 "generating traffic for ${DURATION}s"

# The capture must be running BEFORE the traffic starts, or the first packets
# are missed and a working path looks half broken.
"${SSH[@]}" -i "$KEY_PEM" "ubuntu@${TOOL_PUB}" \
  "sudo rm -f /tmp/prove.pcap; nohup sudo timeout $((DURATION+5)) tcpdump -i any -nn 'proto 47' -w /tmp/prove.pcap >/dev/null 2>&1 &" \
  || { fail "could not start tcpdump on the tool"; exit 4; }
sleep 3

GEN=0
for w in "${WORKLOADS[@]}"; do
  read -r priv pub name platform <<<"$w"
  [[ -z "$pub" || "$pub" == "None" ]] && continue
  # Windows is tapped agentlessly and has no SSH, so it cannot be driven from
  # here. Its traffic still appears if the instance is doing anything at all.
  if [[ "$platform" == "windows" ]]; then
    say "  $name ($priv) windows: tapped agentlessly, not driven from here"
    continue
  fi
  for u in ubuntu ec2-user rhel admin; do
    if "${SSH[@]}" -i "$KEY_PEM" "${u}@${pub}" true 2>/dev/null; then
      "${SSH[@]}" -i "$KEY_PEM" "${u}@${pub}" \
        "nohup sh -c 'ping -c ${DURATION} -s ${PING_SIZE} 8.8.8.8; for i in \$(seq 1 20); do curl -s -o /dev/null https://aws.amazon.com; done' >/dev/null 2>&1 &" 2>/dev/null
      say "  $name ($priv) as $u: ping -s ${PING_SIZE} + https"
      GEN=$((GEN+1)); break
    fi
  done
done
[[ $GEN -eq 0 ]] && say "  (drove no workload directly; measuring whatever traffic exists)"

sleep "$((DURATION+4))"

# --- what arrived ------------------------------------------------------------
# The GRE key sits at ip[24:4] once the key-present flag is set. It is the only
# field distinguishing the collector's copy from the vPB's output here.
read -r N_COLL N_VPB N_TOTAL <<<"$("${SSH[@]}" -i "$KEY_PEM" "ubuntu@${TOOL_PUB}" "
  c=\$(sudo tcpdump -r /tmp/prove.pcap -nn 'ip[24:4] = ${GRE_KEY_COLLECTOR}' 2>/dev/null | wc -l)
  v=\$(sudo tcpdump -r /tmp/prove.pcap -nn 'ip[24:4] = ${GRE_KEY_VPB}' 2>/dev/null | wc -l)
  t=\$(sudo tcpdump -r /tmp/prove.pcap -nn 2>/dev/null | wc -l)
  echo \$c \$v \$t" 2>/dev/null)"

head2 "what arrived at the tool ${TOOL_PRIV}"
say "  total GRE packets              ${N_TOTAL:-0}"
say "  collector mirror  (key ${GRE_KEY_COLLECTOR})       ${N_COLL:-0}"
say "  vPB egress        (key ${GRE_KEY_VPB})      ${N_VPB:-0}"

say ""
say "  tapped sources seen inside the vPB stream:"
"${SSH[@]}" -i "$KEY_PEM" "ubuntu@${TOOL_PUB}" \
  "sudo tcpdump -r /tmp/prove.pcap -nn 'ip[24:4] = ${GRE_KEY_VPB}' 2>/dev/null \
     | grep -oE ': IP [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | awk '{print \$3}' | sort | uniq -c | sort -rn | head" 2>/dev/null \
  | sed 's/^/    /'

say ""
say "  one decapsulated sample (outer = vPB -> tool, inner = the tapped packet):"
"${SSH[@]}" -i "$KEY_PEM" "ubuntu@${TOOL_PUB}" \
  "sudo tcpdump -r /tmp/prove.pcap -nn 'ip[24:4] = ${GRE_KEY_VPB}' -c 1 2>/dev/null" 2>/dev/null | sed 's/^/    /'

# --- vPB counters AFTER ------------------------------------------------------
AFTER="$(vpb_counters)"
if [[ -n "$BEFORE" && -n "$AFTER" ]]; then
  read -r i0 p0 <<<"$BEFORE"; read -r i1 p1 <<<"$AFTER"
  head2 "vPB traffic rule (the device's own count, not KVO's opinion)"
  say "  inspected  ${i0} -> ${i1}   (+$((i1-i0)))"
  say "  passed     ${p0} -> ${p1}   (+$((p1-p0)))"
fi

# --- verdict -----------------------------------------------------------------
head2 "verdict"
RC=0
if [[ "${N_COLL:-0}" -gt 0 ]]; then
  pass "mirror path: workload -> mirror session -> collector -> tool"
else
  fail "mirror path: NO packets with key ${GRE_KEY_COLLECTOR} arrived"
  say "       Usually zero mirror sessions. Check with:"
  say "         aws ec2 describe-traffic-mirror-sessions --region ${REGION}"
  say "       One session per tagged workload is what you want. If there are none,"
  say "       re-commit the Cloud Collection in KVO: that is what cuts them."
  RC=1
fi
if [[ "${N_VPB:-0}" -gt 0 ]]; then
  pass "broker path: ... -> vPB -> egress -> same tool"
else
  fail "broker path: NO packets with key ${GRE_KEY_VPB} arrived"
  say "       The vPB is inspecting but not delivering. Ask the DEVICE, never the"
  say "       dashboard, and in this order:"
  say "         sudo vpb -c 'show traffic-rule-packet-counters'   Passed must exceed 0"
  say "         sudo vpb -c 'show tunnel-status'                  gre_eth2 UP, a local IP"
  say "       Passed 0 with no tunnel means the egress tool is LOCAL or the egress"
  say "       port has no IP. It must be REMOTE / reachableFrom DEVICE_CONFIG with"
  say "       an IP on the port: scripts/vpb_wire_path.py does this."
  RC=1
fi
[[ $RC -eq 0 ]] && say "" && say "  Both paths delivering to ${TOOL_PRIV}. End to end."
exit $RC
