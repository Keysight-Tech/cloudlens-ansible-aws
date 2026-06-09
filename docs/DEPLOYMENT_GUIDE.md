# Deployment Guide

🌐 **Interactive walkthrough:** https://keysight-tech.github.io/cloudlens-ansible-aws/#start-here

The full step-by-step runbook is in [`CloudLens-AutoPilot-Deployment-Runbook.docx`](CloudLens-AutoPilot-Deployment-Runbook.docx) (1,462 lines). This file is the executive summary version for SEs and DevOps reviewers.

## Prerequisites (one-time per AWS account)

1. **Subscribe to AWS Marketplace** for all three Keysight products:
   - [Keysight Vision One](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+Vision+One)
   - [Keysight CloudLens Manager](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Manager)
   - [Keysight CloudLens Virtual Packet Broker](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Virtual+Packet+Broker)
2. **AWS CLI v2** configured with SSO or access keys
3. **EC2 key pair** created in the target region
4. **(Terraform path only)** Terraform v1.5+ installed

## Path A — CloudFormation (recommended for customers)

### Option 1: One-click via AWS Console

[**▶ Deploy from AWS Console**](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/cloudlens-ansible-aws.yaml&stackName=cloudlens-ansible-aws)

The deeplink pre-loads the CFT from this repo. Fill the parameters (key pair, allowed CIDRs) and click **Create Stack**. ~5 minutes.

### Option 2: AWS CLI

```bash
aws cloudformation create-stack \
  --stack-name cloudlens-ansible-aws \
  --template-url https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/cloudlens-ansible-aws.yaml \
  --parameters \
    ParameterKey=KeyPairName,ParameterValue=your-key-pair \
    ParameterKey=AllowedSshCidrs,ParameterValue=your.ip/32 \
    ParameterKey=AllowedWebCidrs,ParameterValue=your.ip/32 \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation wait stack-create-complete --stack-name cloudlens-ansible-aws

# Get the URLs
aws cloudformation describe-stacks --stack-name cloudlens-ansible-aws \
  --query "Stacks[0].Outputs" --output table
```

## Path B — Terraform (SE / DevOps)

```bash
git clone https://github.com/Keysight-Tech/cloudlens-ansible-aws.git
cd cloudlens-ansible-aws/terraform/environments/demo

# Generate customer_input.yaml from the live site wizard:
# https://keysight-tech.github.io/cloudlens-ansible-aws/#wizard
# Or copy and edit demo.tfvars.example:
cp demo.tfvars.example demo.tfvars
vim demo.tfvars   # set aws_region, prefix, key_pair_name, allowed_*_cidrs

terraform init
terraform apply -var-file=demo.tfvars

terraform output urls    # KVO, CLMS, vPB URLs
```

## Phase 2 — Product Configuration (~15 min)

After Phase 1 completes, configure the products via their web UIs:

1. **Accept KVO EULA** — open `https://<kvo-ip>/`, click Agree. _KVO blocks all access including the API until this is done._
2. **Activate licenses** — KVO > Settings > Product Licensing > Activate (vPB Advanced, CloudLens Enterprise, KVO perpetual)
3. **Adopt CLMS into KVO** — KVO > Inventory > CloudLens Manager > Discover. Use the CLMS **private** IP (`10.99.1.x` from outputs).
4. **Onboard vPB to KVO** — SSH to vPB on port 9022, run `configure terminal / kvo / ip <kvo-ip> / port 443 / enable / monitored / end / write memory`. Then adopt in KVO with "Control the adopted device" enabled.
5. **Create AWS Cloud Config** — KVO > Cloud Fabric > Cloud Configs > New > AWS. Paste IAM keys, pick region + VPC + subnets, commit.

## Phase 3 — Sensor Deployment (~5–60 min depending on fleet size)

```bash
./scripts/deploy-sensors.sh \
  --region us-east-1 \
  --profile autopilot \
  --clms-ip <clms-public-ip> \
  --project-key <clms-project-key>
```

Sensors are pushed via AWS Ansible (SSH/SSM/WinRM) — no SSH, no WinRM. Target VMs must have:
- IAM role with `AmazonSSMManagedInstanceCore` policy
- SSM Agent running
- Tags: `cloudlens=true` + `Platform=linux-docker` (or `linux-podman` or `windows`)

Run `./scripts/prep-targets.sh --auto-fix` to remediate any missing prerequisites.

## Verify

In CLMS UI:
1. Login `admin / Cl0udLens@dm!n`
2. Go to **Sensors**
3. Every tagged EC2 instance should be listed as **Connected**

In KVO UI:
1. Login `admin / admin`
2. Go to **Inventory > Devices** — CLMS + vPB should show **CONNECTED**
3. Go to **Cloud Fabric > Cloud Configs** — AWS config should show **COMMITTED**

## Tear down

```bash
# Remove sensors first
./scripts/remove-sensors.sh --region us-east-1 --profile autopilot

# Then destroy infrastructure
aws cloudformation delete-stack --stack-name cloudlens-ansible-aws --region us-east-1
# OR
terraform destroy -var-file=demo.tfvars
```

---

*See the [full runbook](CloudLens-AutoPilot-Deployment-Runbook.docx) for every UI click and exact parameter detail. See [SCALING.md](SCALING.md) for fleet-size guidance.*
