#!/usr/bin/env bash
# Docker entrypoint that routes commands to the right action
set -euo pipefail

cd /work

case "${1:-deploy}" in
  deploy)
    [[ ! -f customer_input.yaml ]] && { echo "Mount customer_input.yaml: -v ./customer_input.yaml:/work/customer_input.yaml"; exit 1; }
    [[ -z "${AZURE_SUBSCRIPTION_ID:-}" ]] && { echo "Set Azure env vars: AZURE_SUBSCRIPTION_ID, AZURE_TENANT, AZURE_CLIENT_ID, AZURE_SECRET"; exit 1; }

    # Auto-tune forks
    VM_COUNT=$(ansible-inventory -i inventory/azure_rm.yaml --list 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('_meta',{}).get('hostvars',{})))" || echo 0)
    if   (( VM_COUNT <= 50 ));    then FORKS=20
    elif (( VM_COUNT <= 500 ));   then FORKS=50
    elif (( VM_COUNT <= 2000 ));  then FORKS=200
    else                                FORKS=500
    fi
    echo "Deploying to $VM_COUNT VMs with $FORKS forks"

    exec ansible-playbook -i inventory/azure_rm.yaml deploy.yaml \
      -e "@customer_input.yaml" --forks "$FORKS"
    ;;

  inventory)
    exec ansible-inventory -i inventory/azure_rm.yaml --graph
    ;;

  cleanup)
    exec ansible-playbook -i inventory/azure_rm.yaml cleanup.yaml \
      -e "@customer_input.yaml"
    ;;

  shell)
    exec bash
    ;;

  shard)
    shift
    exec bash deploy/shard.sh "$@"
    ;;

  *)
    exec "$@"
    ;;
esac
