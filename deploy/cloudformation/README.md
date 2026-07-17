# CloudLens on AWS - CloudFormation

One-click AWS CloudFormation templates for the Keysight CloudLens visibility
stack: **vController** (formerly CLMS, the control plane), **KVO** (Keysight
Vision Orchestrator), and **vPB** (Virtual Packet Broker, the data plane). These
mirror the Azure ARM templates in the sibling `cloudlens-ansible-azure` repo.

**Multi-region.** Each template ships a `RegionMap` with the CloudLens
Marketplace AMIs for 12 regions: us-east-1, us-east-2, us-west-1, us-west-2,
ca-central-1, eu-west-1, eu-west-2, eu-central-1, ap-southeast-1,
ap-southeast-2, ap-northeast-1, ap-south-1. All regions pin the same product
versions (vController CLMS 6.12.1-32, KVO 2.13.0, vPB 3.13.0-7 security-update-1),
so the stack deploys identical software everywhere. The template resolves the
right AMIs from `AWS::Region`, so just launch it in your target region (the docs
site has a region picker, or change `region=` in the console URL). Subscribe to
the Marketplace AMIs in each region before launching there.

## Where the Launch Stack buttons fetch templates from

CloudFormation's `?templateURL=` deeplink only accepts S3 URLs. GitHub raw
URLs (`raw.githubusercontent.com`) are silently rejected with `TemplateURL
must be a supported URL`. The docs site's Launch buttons point at a public
S3 mirror of this directory:

```
https://keysight-cloudlens-templates.s3.us-east-1.amazonaws.com/aws/<name>.yaml
```

After changing any `*.yaml` here, re-sync so the buttons deploy the current
template:

```bash
AWS_PROFILE=AdministratorAccess-466778915280 \
  bash deploy/scripts/sync-cfn-templates-to-s3.sh
```

The script lints (cfn-lint), uploads, verifies public HTTPS reads, and calls
`aws cloudformation validate-template` on the S3 URL to prove the button
will work. See `.github/workflows/sync-cfn-templates.yml` for the CI path
(opt-in via `AWS_ROLE_ARN` secret).

## Prerequisites

1. **Subscribe to the CloudLens Marketplace AMIs** in your AWS account before
   launching. Instances fail to start without an active subscription.
2. An existing **EC2 key pair** in us-east-1 for SSH access.
3. Optionally, an existing VPC and subnet if you do not want the template to
   create one.

## AMIs and instance types (us-east-1)

| Component | AMI | Instance type |
| --- | --- | --- |
| vController (CLMS) | `ami-0bebd5e730315337e` | `t3.xlarge` |
| KVO | `ami-017c0db8981569380` | `c5.2xlarge` (only supported size) |
| vPB | `ami-0a561b450552b707d` | `t3.xlarge` (only supported size) |

The vPB exposes OS-level SSH on **port 9022** (not 22). Security groups open
SSH (22), vPB SSH (9022), HTTPS (443), and, for vPB, VXLAN (4789, 10800-10801),
all scoped to the `AdminIngressCidr` parameter.

## Launch Stack (quick-create console URLs)

These open the CloudFormation "Create stack / review" page in us-east-1,
pre-loaded with the template from the `main` branch of this repo.

### Full stack (vController + KVO + vPB)

[**Launch Stack**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fstack.yaml&stackName=cloudlens-stack)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fstack.yaml&stackName=cloudlens-stack
```

The stack template has `DeployKVO` and `DeployVPB` toggles (`yes`/`no`) so you
can compose exactly the components you want in one shared VPC with mgmt, data,
and tool subnets.

### Individual components

vController (CLMS):

[**Launch vController**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fclms.yaml&stackName=cloudlens-vcontroller)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fclms.yaml&stackName=cloudlens-vcontroller
```

KVO:

[**Launch KVO**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fkvo.yaml&stackName=cloudlens-kvo)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fkvo.yaml&stackName=cloudlens-kvo
```

vPB:

[**Launch vPB**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fvpb.yaml&stackName=cloudlens-vpb)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fkeysight-cloudlens-templates.s3.us-east-1.amazonaws.com%2Faws%2Fvpb.yaml&stackName=cloudlens-vpb
```

## Deploy from the CLI

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name cloudlens-stack \
  --template-file stack.yaml \
  --parameter-overrides \
      KeyPairName=my-keypair \
      AdminIngressCidr=203.0.113.0/24 \
      DeployKVO=yes \
      DeployVPB=yes
```

Swap `stack.yaml` for `clms.yaml`, `kvo.yaml`, or `vpb.yaml` to deploy a single
component.

## Parameters (common)

| Parameter | Purpose |
| --- | --- |
| `KeyPairName` | Existing EC2 key pair for SSH. |
| `InstanceType` | Fixed to the size the AMI is qualified on (single allowed value per component). |
| `AdminIngressCidr` | Source CIDR allowed to reach SSH, vPB SSH, HTTPS, and VXLAN. Narrow this. Ignored when you supply your own security group. |
| `ExistingVpcId` / `ExistingSubnetId` | Deploy into a VPC you already own. Leave blank to create a new one. |
| `ExistingDataSubnetId` | (stack only) Separate subnet for the vPB data plane. Blank = vPB reuses the subnet above. |
| `ExistingSecurityGroupId` | Attach a pre-approved security group instead of creating one. Blank = template creates one from `AdminIngressCidr`. |
| `AssignPublicIp` | `yes` (default) = public Elastic IPs. `no` = private IPs only, no EIPs. |
| `VpcCidr` | CIDR for the new VPC (default `10.99.0.0/16`) when not using an existing VPC. |

## Deploy into existing infrastructure (any infra)

Every template deploys greenfield (new VPC) by default, or into whatever the
customer already runs. Mix and match these to fit the target environment:

**Existing VPC + subnet.** Set `ExistingVpcId` and `ExistingSubnetId`. The
template creates no VPC, IGW, subnets, or routes: instances land in the subnet
you name. On the `stack` template, optionally set `ExistingDataSubnetId` to keep
the vPB data plane in a separate subnet from the control plane.

**Private subnet / no public IPs.** Set `AssignPublicIp` to `no`. No Elastic IPs
are attached; the stack outputs private IPs and expects you to reach the UIs over
VPN, Direct Connect, or VPC peering. The chosen subnet still needs outbound
internet (NAT gateway or VPC endpoints) so the Marketplace AMIs can activate and
the vPB first-boot bootstrap can run. Without egress, instances launch but never
finish initializing.

**Customer-managed security group.** Set `ExistingSecurityGroupId` to attach a
security group your org already approved. The template then skips creating one,
and `AdminIngressCidr` is ignored. You are responsible for opening SSH (22),
vPB SSH (9022), HTTPS (443), and VXLAN (4789, 10800-10801).

All four combinations are backward compatible: leave the new parameters at their
defaults and the deploy behaves exactly as before (new VPC, public EIPs,
template-managed security group).

## After deploy

1. Open the vController UI (see stack outputs), log in with
   `admin / Cl0udLens@dm!n`, and change the password.
2. Create a Project and copy the project key.
3. Register KVO and vPB fleets, then install CloudLens sensors on your workloads.

Stack outputs include public IP, private IP, console URL, and the SSH command
for each deployed instance.
