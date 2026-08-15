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
# 0 = measure once and report. Give it seconds to keep retrying while the
# collector registers and KVO pushes the device rule: a fresh deploy typically
# needs several minutes before the broker leg carries anything.
WAIT_SECONDS="${CLOUDLENS_PROVE_WAIT:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)      REGION="$2"; shift 2 ;;
    --vpc-id)      VPC_ID="$2"; shift 2 ;;
    --stack-name)  STACK_NAME="$2"; shift 2 ;;
    --key)         KEY_PEM="$2"; shift 2 ;;
    --duration)    DURATION="$2"; shift 2 ;;
    --wait)        WAIT_SECONDS="$2"; shift 2 ;;
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
# SCOPE THIS TO THE VPC. describe-traffic-mirror-sessions is REGION-wide, and a
# bare count happily returns a SIBLING stack's sessions. Measured live: one stack
# had zero of its own while another in the same region had three, so the guard
# reported a false pass and the proof measured a path that had never been cut.
# Sessions carry no VpcId, so resolve THIS VPC's interfaces and count only
# sessions whose SOURCE interface is one of them. Nothing is hardcoded: the
# interface list is derived from --vpc-id at runtime, so a brand new stack works
# with no edits.
VPC_ENI_FILE="$(mktemp)"; trap 'rm -f "$VPC_ENI_FILE"' EXIT
"${AWSQ[@]}" ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null \
  | tr '\t' '\n' | sed '/^$/d' | sort -u > "$VPC_ENI_FILE"
SESSIONS=0
if [[ -s "$VPC_ENI_FILE" ]]; then
  SESSIONS="$("${AWSQ[@]}" ec2 describe-traffic-mirror-sessions \
    --query 'TrafficMirrorSessions[].NetworkInterfaceId' --output text 2>/dev/null \
    | tr '\t' '\n' | sed '/^$/d' | grep -c -F -x -f "$VPC_ENI_FILE" || true)"
fi
say "  mirror sessions ${SESSIONS:-0}"
if [[ "${SESSIONS:-0}" == "0" ]]; then
  head2 "nothing to measure yet"
  fail "This VPC has 0 traffic mirror sessions: no traffic is being copied."
  say "  (Counted per-VPC on purpose. Another stack in ${REGION} may have sessions"
  say "   of its own, and those copy nothing to THIS tool.)"
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

# --- nothing is instant. Wait for the fabric before calling it a failure -----
# Sessions get cut, the collector boots and registers, and KVO pushes the device
# rule, all at their own pace. Measuring the instant a deploy finishes reports a
# healthy build as broken. --wait retries the whole measurement instead of
# guessing a sleep, and reports what it is still waiting for.
WAIT_UNTIL=$(( $(date +%s) + WAIT_SECONDS ))

measure_once() {
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

}

# Measure, and retry while there is still wait budget. A failure on the first
# pass right after a deploy usually means the collector has not registered yet,
# not that anything is wrong: retrying is the honest way to tell those apart.
ATTEMPT=0
while :; do
  ATTEMPT=$((ATTEMPT+1))
  measure_once
  if [[ "${N_COLL:-0}" -gt 0 && "${N_VPB:-0}" -gt 0 ]]; then break; fi
  now=$(date +%s)
  if (( now >= WAIT_UNTIL )); then break; fi
  left=$(( WAIT_UNTIL - now ))
  head2 "attempt ${ATTEMPT}: not both paths yet, ${left}s of wait budget left"
  [[ "${N_COLL:-0}" -eq 0 ]] && say "  waiting on: mirror sessions / collector registering"
  [[ "${N_VPB:-0}"  -eq 0 ]] && say "  waiting on: the vPB to receive and forward (KVO pushes the rule)"
  sleep 30
done

# --- verdict -----------------------------------------------------------------
head2 "verdict"
RC=0
if [[ "${N_COLL:-0}" -gt 0 ]]; then
  pass "mirror path: workload -> mirror session -> collector -> tool"
else
  fail "mirror path: NO packets with key ${GRE_KEY_COLLECTOR} arrived"
  say "       This VPC has ${SESSIONS} session(s), so AWS IS copying traffic and the"
  say "       break is downstream of the mirror. In order of likelihood:"
  say "         1. the collector only just launched. It needs a few minutes to"
  say "            register with the vController before it forwards anything."
  say "         2. the tool's security group does not permit IP protocol 47 (GRE)."
  say "         3. no collector is running in this VPC at all:"
  say "            aws ec2 describe-instances --region ${REGION} \\"
  say "              --filters Name=vpc-id,Values=${VPC_ID} 'Name=tag:Name,Values=*collector*'"
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
