# =====================================================================
# CloudLens Ansible for AWS: Deployment Image
# =====================================================================
# Zero-install deployment for any team. Works from any machine with Docker.
#
# Build:
#   docker build -t cloudlens-ansible-aws .
#
# Run (interactive):
#   docker run --rm -it \
#     -v $(pwd)/customer_input.yaml:/work/customer_input.yaml \
#     -v $HOME/.ssh:/root/.ssh:ro \
#     -v $HOME/.aws:/root/.aws:ro \
#     -v $(pwd)/files:/work/files:ro \
#     -e ANSIBLE_WINRM_PASSWORD="${ANSIBLE_WINRM_PASSWORD}" \
#     -e AWS_PROFILE -e AWS_REGION \
#     cloudlens-ansible-aws
#
# CI/CD (single command):
#   docker run --rm \
#     -v $(pwd):/work \
#     -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
#     -e AWS_SESSION_TOKEN -e AWS_REGION \
#     cloudlens-ansible-aws deploy
# =====================================================================

FROM python:3.12-slim AS base

LABEL maintainer="Keysight Technologies"
LABEL description="Automated CloudLens sensor deployment for AWS EC2 (Linux + Windows)"

# Pinned tool versions (override at build time with --build-arg if needed)
ARG TERRAFORM_VERSION=1.9.8

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=False \
    ANSIBLE_RETRY_FILES_ENABLED=False \
    ANSIBLE_FORCE_COLOR=True

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
RUN pip install --no-cache-dir \
    "ansible-core>=2.16,<2.18" \
    pywinrm \
    requests-ntlm \
    "boto3>=1.34" \
    "botocore>=1.34"

# Ansible collections (pinned in requirements.yml: amazon.aws, community.aws,
# ansible.windows, community.windows, community.docker, containers.podman,
# community.general). Copy the file first so this layer caches independently.
COPY requirements.yml /tmp/requirements.yml
RUN ansible-galaxy collection install -r /tmp/requirements.yml --upgrade \
    && rm -f /tmp/requirements.yml

# Install the amazon.aws collection's Python requirements if it ships any
# (extra boto3/botocore pins). Safe no-op when the file is absent.
RUN reqs=/root/.ansible/collections/ansible_collections/amazon/aws/requirements.txt; \
    if [ -f "$reqs" ]; then pip install --no-cache-dir -r "$reqs"; fi

# Copy repo content
WORKDIR /work
COPY . /work/

# Make scripts executable
RUN chmod +x scripts/*.sh deploy/*.sh quickstart.sh 2>/dev/null || true

# Entrypoint
ENTRYPOINT ["/work/scripts/docker-entrypoint.sh"]
CMD ["deploy"]
