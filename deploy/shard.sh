#!/usr/bin/env bash
# =====================================================================
# Sharded deployment for more than 2,000 VMs
# =====================================================================
# Splits inventory into N chunks, runs them in parallel
# Each shard is independent: failures in one do not affect others
# =====================================================================
set -euo pipefail

VM_COUNT="${1:-1000}"
FORKS_PER_SHARD="${2:-200}"
SHARD_SIZE="${SHARD_SIZE:-500}"
LOG_DIR="${LOG_DIR:-./logs/shards}"

mkdir -p "$LOG_DIR"

# Compute shard count
SHARDS=$(( (VM_COUNT + SHARD_SIZE - 1) / SHARD_SIZE ))
echo "═══════════════════════════════════════════════════════════════"
echo "SHARDED DEPLOYMENT"
echo "═══════════════════════════════════════════════════════════════"
echo "  Total VMs:       $VM_COUNT"
echo "  Shard size:      $SHARD_SIZE VMs per shard"
echo "  Shard count:     $SHARDS parallel shards"
echo "  Forks per shard: $FORKS_PER_SHARD"
echo "  Total parallel:  $(( SHARDS * FORKS_PER_SHARD )) Ansible workers"
echo "  Logs:            $LOG_DIR/"
echo

# Dump full inventory once
ansible-inventory -i inventory/azure_rm.yaml --list > /tmp/full_inventory.json

# Split host list into N shards
python3 <<EOF
import json, math, os
inv = json.load(open('/tmp/full_inventory.json'))
hostvars = inv.get('_meta', {}).get('hostvars', {})
hosts = sorted(hostvars.keys())
n = $SHARDS
size = math.ceil(len(hosts) / n)
for i in range(n):
    chunk = hosts[i*size:(i+1)*size]
    shard = {'all': {'hosts': {h: hostvars[h] for h in chunk}}}
    with open(f'/tmp/shard_{i:03d}.json', 'w') as f:
        json.dump(shard, f)
print(f'Wrote {n} shard files')
EOF

# Launch shards in parallel (background)
pids=()
for i in $(seq -f "%03g" 0 $((SHARDS - 1))); do
  (
    export ANSIBLE_HOST_KEY_CHECKING=False
    ansible-playbook -i /tmp/shard_${i}.json deploy.yaml \
      -e "@customer_input.yaml" --forks "$FORKS_PER_SHARD" \
      > "$LOG_DIR/shard_${i}.log" 2>&1 \
      && echo "[shard $i] ✓ complete" \
      || echo "[shard $i] ✗ failed; see $LOG_DIR/shard_${i}.log"
  ) &
  pids+=($!)
done

echo "Launched $SHARDS shards. Waiting for completion..."
echo

# Progress monitor
while ps -p ${pids[@]} 2>/dev/null | tail -n +2 | grep -q .; do
  running=$(ps -p ${pids[@]} 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')
  echo "  $(date +%H:%M:%S): $running of $SHARDS shards still running"
  sleep 30
done

wait

# Aggregate summary
echo
echo "═══════════════════════════════════════════════════════════════"
echo "SHARD SUMMARY"
echo "═══════════════════════════════════════════════════════════════"
for log in "$LOG_DIR"/shard_*.log; do
  shard=$(basename "$log" .log)
  ok=$(grep -oE 'ok=[0-9]+' "$log" | tail -1 | cut -d= -f2 || echo 0)
  changed=$(grep -oE 'changed=[0-9]+' "$log" | tail -1 | cut -d= -f2 || echo 0)
  failed=$(grep -oE 'failed=[0-9]+' "$log" | tail -1 | cut -d= -f2 || echo 0)
  printf "  %-12s ok=%-4s changed=%-4s failed=%-4s\n" "$shard" "$ok" "$changed" "$failed"
done
