# Deployment Guide

🌐 **Interactive walkthrough:** https://keysight-tech.github.io/cloudlens-ansible-aws/#start-here

The full step-by-step runbooks are [`CloudLens_Stack_Deployment_Runbook.pdf`](CloudLens_Stack_Deployment_Runbook.pdf) (the stack) and [`CloudLens_Ansible_AWS_Customer_Runbook.pdf`](CloudLens_Ansible_AWS_Customer_Runbook.pdf) (the sensors); both also ship as .docx. This file is the executive summary version for SEs and DevOps reviewers.

## Prerequisites (one-time per AWS account)

1. **Subscribe to AWS Marketplace** for all three Keysight products:
   - [Keysight Vision Orchestrator](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+Vision+Orchestrator)
   - [Keysight CloudLens Manager](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Manager)
   - [Keysight CloudLens Virtual Packet Broker](https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens+Virtual+Packet+Broker)
2. **AWS CLI v2** configured with SSO or access keys
3. **EC2 key pair** created in the target region
4. **(Terraform path only)** Terraform v1.5+ installed

## Path 0 - One command (recommended)

```bash
bash deploy/deploy-stack.sh --doctor      # checks this machine and account, deploys nothing
curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh \
  | bash -s -- --region us-east-1 --with-kvo --with-vpb --key-name my-key --admin-cidr 203.0.113.10/32
```

The script runs 18 phase steps: environment and pre-flight checks, the interview (or a replayed `--profile`), an existing-deployment check, the Marketplace subscription check, the stack (CloudFormation by default, `--iac terraform`), the vController wait, the vPB bootstrap over SSH, the project key and a working UI login, the sensor mode, KVO licensing (`--kvo-codes`), adoption plus the Cloud Config, the sensors, EKS pods (`--with-eks` or `--eks-cluster NAME`), vPB adoption and traffic path, the AWS mirror fabric (`--with-mirror`), an end-to-end traffic proof, and the summary. Phases 2 and 3 below are what it does for you. A re-run never starts from zero: `--resume` checks the real system and continues from the first unfinished phase, and `--from` / `--only` take the names `stack wait bootstrap key license adopt sensors eks vpb mirror path prove`. The local console (`cd console && python3 -m cloudlens_console`) drives the same script from a browser and writes a `deploy-profile-<stack>.env` the CLI replays.

## Path A - CloudFormation (infrastructure only)

### Option 1: One-click via AWS Console

[**▶ Deploy from AWS Console**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fstack.yaml&stackName=cloudlens-stack)

The deeplink pre-loads the CFT from this repo. Fill the parameters (key pair, allowed CIDRs) and click **Create Stack**. ~5 minutes.

### Option 2: AWS CLI

```bash
aws cloudformation create-stack \
  --stack-name cloudlens-stack \
  --template-url https://keysight-cloudlens-templates.s3.us-east-1.amazonaws.com/aws/stack.yaml \
  --parameters \
    ParameterKey=KeyPairName,ParameterValue=your-key-pair \
    ParameterKey=AdminIngressCidr,ParameterValue=your.ip/32 \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation wait stack-create-complete --stack-name cloudlens-stack

# Get the URLs
aws cloudformation describe-stacks --stack-name cloudlens-stack \
  --query "Stacks[0].Outputs" --output table
```

Other parameters: `DeployKVO` and `DeployVPB` (default yes), `EnableZoneTapping` (default no), `AssignPublicIp` (default yes), `VcontrollerInstanceType` (t3.xlarge or m5.xlarge), the `DeployTestWorkloadUbuntu/Rhel/Windows` toggles, `DiscoveryTagKey` / `DiscoveryTagValue` (cloudlens / yes). For an existing VPC: `ExistingVpcId`, `ExistingVpcCidr`, `ExistingSubnetId`, `ExistingDataSubnetId`, `ExistingToolSubnetId`, `ExistingSecurityGroupId`.

## Path B - Terraform (SE / DevOps)

```bash
git clone https://github.com/Keysight-Tech/cloudlens-ansible-aws.git
cd cloudlens-ansible-aws/deploy/terraform/stack

# Copy and edit the example (customer_input.yaml is for the sensor run, not Terraform):
cp terraform.tfvars.example terraform.tfvars
vim terraform.tfvars   # set region, key_name (or public_key), deploy_kvo, deploy_vpb, ssh_ingress_cidrs, https_ingress_cidrs

terraform init
terraform apply

terraform output         # vcontroller_ui_urls, kvo_ui_urls, vpb_ssh_commands, summary, next_step
```

The Terraform engine builds a new VPC only; for an existing VPC use CloudFormation (`deploy-stack.sh --existing-vpc-id ...` refuses `--iac terraform`).

## Phase 2 - Product Configuration (~15 min)

`deploy-stack.sh` does all of this (phases 8 to 16: `--bootstrap-vpb`, `--kvo-codes`, `--cloud-config`, `--adopt-vpb`, `--wire-vpb-path`, `--with-mirror`; the scripts behind them accept the KVO EULA with `--accept-eula`). Doing it by hand after a Launch Stack or Terraform deploy, the equivalent clicks are:

1. **Accept KVO EULA** - open `https://<kvo-ip>/`, click Agree. _KVO blocks all access including the API until this is done._
2. **Activate licenses** - KVO > Settings > Product Licensing > Activate (vPB Advanced, CloudLens Enterprise, KVO perpetual)
3. **Adopt vController (CLMS) into KVO** - KVO > Inventory > CloudLens Manager > Discover. Use the vController **private** IP (the `VcontrollerPrivateIp` stack output; `10.99.1.x` in a new VPC). The deploy does this in phase 12 with `scripts/kvo_adopt_clms.py`.
4. **Onboard vPB to KVO** - the deploy does this in the bootstrap, adoption and traffic-path phases (8, 14 and 16: `--from bootstrap`, `--from vpb`, `--from path`). `scripts/bootstrap-vpb.sh` runs over SSH (`ssh -i <key>.pem -p 9022 admin@<vpb-ip>`, key-pair login); `scripts/vpb_kvo_adopt.py` enters the `kvo` context on the vPB through `sudo vpb -c` (`ip <kvo-private-ip>`, `port 443`, `enable`, `exit`), waits for the announcement and adopts the device in KVO with control enabled, and KVO applies the vPB licence on adoption; `scripts/vpb_wire_path.py` then syncs the ports, creates the Cloud to Device Link, binds eth1 (ingress) and eth2 (egress, with an address), creates a REMOTE tool and the monitoring policy. By hand: `license server <kvo-private-ip> type advanced` first, then the same `kvo` context on the vPB, then KVO > Inventory > adopt with "Control the adopted device" enabled.
5. **Create AWS Cloud Config** - KVO > Cloud Fabric > Cloud Configs > New > AWS. Paste IAM keys, pick region + VPC + subnets, commit.

## Phase 3 - Sensor Deployment (~5–60 min depending on fleet size)

```bash
# Phase 13 of deploy-stack.sh runs this for you. By hand:
cp customer_input.yaml.example customer_input.yaml
vim customer_input.yaml   # cloudlens.manager_ip_or_fqdn, cloudlens.project_key, aws.regions, aws.ssh_key_path
bash quickstart.sh        # or: bash scripts/deploy.sh customer_input.yaml
```

Sensors are pushed by Ansible over SSH (Linux default), SSM Session Manager (Linux or Windows, no inbound ports: `aws.linux_connection: ssm` / `aws.windows_connection: ssm`) or WinRM (Windows). Target VMs need:
- For the SSM connection modes only: an IAM role with `AmazonSSMManagedInstanceCore` and a running SSM Agent
- The discovery tag, `cloudlens=yes` by default (template `DiscoveryTagKey` / `DiscoveryTagValue`, or `--discovery-tag-key` / `--discovery-tag-value`). An `os=ubuntu|rhel|windows` tag is optional: untagged hosts are classified from AWS platform details and, for Linux, a probe (`playbooks/classify.yaml`)
- Or no tags at all: `aws.tag_filters`, `aws.instance_ids` or `aws.inventory_file` in `customer_input.yaml` drive discovery directly

Run `bash deploy/deploy-stack.sh --doctor` first: it checks the deploying machine and account (credentials, region, Marketplace subscriptions, quotas, key pair, tooling, network) and prints the exact fix beside each gap, deploying nothing. `scripts/bootstrap_winrm.sh` prepares a Windows target for the WinRM mode.

## Existing VPC, EKS and discovery

- **Existing VPC**: `--existing-vpc-id` and `--existing-subnet-id`, plus `--existing-data-subnet-id` and `--existing-tool-subnet-id` for the vPB data plane (all three subnets in the same AZ) and `--existing-sg-id` for a pre-approved group. The script fills the template's `ExistingVpcCidr` from the VPC so the in-VPC ports open to the right range. CloudFormation engine only. Sensors register to the vController's private address (`CLOUDLENS_SENSOR_MANAGER_ADDR` overrides). Proven live 2026-10-07 with an existing EKS cluster and pre-existing tagged workloads: both vPBs adopted and wired (one per rail), the sensor DaemonSet on every node, and the traffic proof passing on the mirror path and the vPB path.
- **EKS**: `--eks-cluster NAME` taps an existing cluster, `--eks-sample` creates a test one; `--eks-mode daemonset` (default) or `sidecar`; `--eks-pod-selector REGEX`. The Kubernetes presence is created first and the DaemonSet registers with its key; an in-VPC cluster uses the vController's private address.
- **Discovery**: `--discover` (with `--discover-regions`, `--discover-accounts organization`, `--discover-role`) scans for the discovery tag, proposes collector placement per VPC and writes a replayable profile. Read-only.
- **Rehearsal**: `bash scripts/lab/brownfield-fixture.sh create` (then `status`, `env`, `destroy`) builds a customer-shaped VPC with tagged workloads and an EKS cluster to deploy against.

## Verify

In CLMS UI:
1. Login as `admin` with the password phase 9 of the deploy set (printed at the end of the run, kept in `cloudlens-deploy-summary.txt` and the mode-600 creds file). `Cl0udLens@dm!n` is the factory password and is only valid until the first login
2. Go to **Sensors**
3. Every tagged EC2 instance should be listed as **Connected**

In KVO UI:
1. Login `admin / admin`
2. Go to **Inventory > Devices** - CLMS + vPB should show **CONNECTED**
3. Go to **Cloud Fabric > Cloud Configs** - AWS config should show **COMMITTED**

## Tear down

```bash
# Remove the sensors from the workloads first if they should not keep running
bash scripts/cleanup.sh customer_input.yaml

# Then remove everything the deployment created. It audits first, offers to
# release the KVO's licences while the KVO is alive, asks before deleting,
# terminates the rail vPBs from --vpb-rails and their Elastic IPs before the
# stack delete, then sweeps what CloudFormation leaves behind (unattached
# volumes, non-stack security groups, the collector Auto Scaling Group,
# mirror targets and the deploy-stamped resources inside a customer VPC; in
# a pre-existing VPC only the VPC itself and resources the deploy did not
# stamp are left alone). A bare delete-stack leaves all of that billing.
bash deploy/teardown-stack.sh --stack-name cloudlens-stack --region us-east-1
bash deploy/teardown-stack.sh --stack-name cloudlens-stack --orphans   # read-only audit
bash deploy/teardown-stack.sh --stack-name cloudlens-stack --yes --release-licences   # no terminal

# Terraform path
cd deploy/terraform/stack && terraform destroy
```

---

*See the [stack runbook](CloudLens_Stack_Deployment_Runbook.pdf) and the [sensor runbook](CloudLens_Ansible_AWS_Customer_Runbook.pdf) for every UI click and exact parameter detail. See [SCALING.md](SCALING.md) for fleet-size guidance.*
