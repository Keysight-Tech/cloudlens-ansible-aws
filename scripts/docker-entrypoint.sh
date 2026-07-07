#!/usr/bin/env bash
# Docker entrypoint that routes commands to the right action
set -euo pipefail

cd /work

case "${1:-deploy}" in
  deploy)
    [[ ! -f customer_input.yaml ]] && { echo "Mount customer_input.yaml: -v ./customer_input.yaml:/work/customer_input.yaml"; exit 1; }
    # AWS auth for the amazon.aws.aws_ec2 inventory. Accept any of:
    #   1. AWS_PROFILE       (with a mounted -v $HOME/.aws:/root/.aws:ro)
    #   2. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (env keys, optional token)
    #   3. A mounted ~/.aws/credentials with a [default] profile
    if [[ -z "${AWS_PROFILE:-}" \
       && ( -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ) \
       && ! -f /root/.aws/credentials ]]; then
      echo "Set AWS credentials for the aws_ec2 inventory. Use one of:"
      echo "  - AWS_PROFILE (mount creds: -v \$HOME/.aws:/root/.aws:ro)"
      echo "  - AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ optional AWS_SESSION_TOKEN)"
      echo "  - a mounted ~/.aws/credentials with a [default] profile"
      exit 1
    fi

    # Auto-tune forks
    VM_COUNT=$(ansible-inventory -i inventory/aws_ec2.yaml --list 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('_meta',{}).get('hostvars',{})))" || echo 0)
    if   (( VM_COUNT <= 50 ));    then FORKS=20
    elif (( VM_COUNT <= 500 ));   then FORKS=50
    elif (( VM_COUNT <= 2000 ));  then FORKS=200
    else                                FORKS=500
    fi
    echo "Deploying to $VM_COUNT VMs with $FORKS forks"

    exec ansible-playbook -i inventory/aws_ec2.yaml deploy.yaml \
      -e "@customer_input.yaml" --forks "$FORKS"
    ;;

  inventory)
    exec ansible-inventory -i inventory/aws_ec2.yaml --graph
    ;;

  cleanup)
    exec ansible-playbook -i inventory/aws_ec2.yaml cleanup.yaml \
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
