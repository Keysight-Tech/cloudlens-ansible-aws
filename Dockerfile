# =====================================================================
# CloudLens Ansible for AWS: Deployment Image
# =====================================================================
# Zero-install deployment for any team. Works from any machine with Docker.
#
# Build:
#   docker build -t cloudlens-ansible-aws .
#
# Run (interactive), from the folder that holds customer_input.yaml:
#   docker run --rm -it --platform linux/amd64 \
#     -v "$(pwd)/customer_input.yaml:/work/customer_input.yaml:ro" \
#     -v "$(pwd)/files:/work/files:ro" \
#     -v "$HOME/.ssh/id_rsa:/root/.ssh/id_rsa:ro" \
#     -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
#     -e AWS_DEFAULT_REGION \
#     ghcr.io/keysight-tech/cloudlens-ansible-aws:latest
#
# CI/CD: the same command without -it (a CI runner has no TTY), with the image
# pinned to a commit tag (:main-<sha>). Never mount the whole working directory
# over /work: that hides the image's own playbooks and entrypoint.
# =====================================================================

FROM python:3.12-slim AS base

LABEL maintainer="Keysight Technologies"
LABEL description="Automated CloudLens sensor deployment for AWS EC2 (Linux + Windows)"

# Pinned tool versions (override at build time with --build-arg if needed)
ARG TERRAFORM_VERSION=1.9.8

# ANSIBLE_COLLECTIONS_PATH is where the collections below are installed and
# where Ansible must look for them. ansible.cfg pins collections_path to
# ./collections so a laptop run only ever loads what quickstart.sh installed
# beside the playbooks; inside the image that directory does not exist, and
# without this variable every command failed with "unknown plugin
# amazon.aws.aws_ec2". It is world-readable so a CI runner that starts the
# image as a non-root user works too.
# ANSIBLE_HOME / ANSIBLE_CACHE_PLUGIN_CONNECTION: Ansible's scratch and fact
#   cache under /tmp, writable by any user, instead of ~/.ansible and /work.
# ANSIBLE_INVENTORY_UNPARSED_FAILED: an inventory that cannot be read (bad
#   credentials, no access) is an error. By default Ansible only warns, runs
#   every play against no hosts and exits 0.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=False \
    ANSIBLE_RETRY_FILES_ENABLED=False \
    ANSIBLE_FORCE_COLOR=True \
    ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections \
    ANSIBLE_HOME=/tmp/.ansible \
    ANSIBLE_CACHE_PLUGIN_CONNECTION=/tmp/.ansible_facts_cache \
    ANSIBLE_INVENTORY_UNPARSED_FAILED=True

# System deps (unzip is needed to unpack the AWS CLI v2 and Terraform archives)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    ca-certificates \
    sshpass \
    sudo \
    unzip \
    gnupg lsb-release \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (official installer). linux/amd64 only, matching the CI platform.
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws \
    && aws --version

# Terraform (pinned; used by deploy/terraform IaC and the shard tooling)
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o /tmp/terraform.zip \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin \
    && rm -f /tmp/terraform.zip \
    && terraform version

# Python deps: Ansible, WinRM (Basic + NTLM transports), and the AWS SDK
# (boto3 / botocore) that the amazon.aws.aws_ec2 dynamic inventory requires.
# Note: requests-kerberos is intentionally excluded. It needs C build deps
# (libkrb5-dev, gcc) not present in python:3.12-slim, and our playbooks
# use Basic / NTLM transport for WinRM. Add it back here and install
# libkrb5-dev + gcc above if a customer ever needs Kerberos transport.
#
# ansible-core stays on 2.16: it is the last release that can manage Python
# 3.6 targets, which is what RHEL 7 and RHEL 8 ship, and the docs support
# RHEL 7/8/9. 2.17 dropped Python 3.6, so every RHEL 8 instance would fail.
RUN pip install --no-cache-dir \
    "ansible-core>=2.16,<2.17" \
    "pywinrm>=0.4,<0.6" \
    "requests-ntlm>=1.1,<2" \
    "boto3>=1.34" \
    "botocore>=1.34"

# Ansible collections (pinned in requirements.yml: amazon.aws, community.aws,
# ansible.windows, community.windows, community.docker, containers.podman,
# community.general). Copy the file first so this layer caches independently.
COPY requirements.yml /tmp/requirements.yml
# Upstream test trees are removed: they hold fixture private keys that set off
# every customer's secret scanner and nothing at runtime reads them. galaxy
# creates $ANSIBLE_HOME as root with mode 700; left in the image, a non-root
# user could not create its temp dir and every command failed.
RUN ansible-galaxy collection install -r /tmp/requirements.yml --upgrade \
        -p "$ANSIBLE_COLLECTIONS_PATH" \
    && find "$ANSIBLE_COLLECTIONS_PATH"/ansible_collections/*/* -maxdepth 1 -type d -name tests -exec rm -rf {} + \
    && rm -f /tmp/requirements.yml \
    && rm -rf "$ANSIBLE_HOME"

# Session Manager plugin: Windows instances are reached over SSM by default
# (community.aws.aws_ssm), which shells out to this binary. The image never
# had it, so every Windows instance failed in Docker.
RUN curl -fsSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o /tmp/smp.deb \
    && dpkg -i /tmp/smp.deb \
    && rm -f /tmp/smp.deb \
    && session-manager-plugin --version

# Install the amazon.aws collection's Python requirements if it ships any
# (extra boto3/botocore pins). Safe no-op when the file is absent.
RUN reqs="$ANSIBLE_COLLECTIONS_PATH/ansible_collections/amazon/aws/requirements.txt"; \
    if [ -f "$reqs" ]; then pip install --no-cache-dir -r "$reqs"; fi

# Copy repo content
WORKDIR /work
COPY . /work/

# Make scripts executable
RUN chmod +x scripts/*.sh deploy/*.sh quickstart.sh 2>/dev/null || true

# Entrypoint
ENTRYPOINT ["/work/scripts/docker-entrypoint.sh"]
CMD ["deploy"]
