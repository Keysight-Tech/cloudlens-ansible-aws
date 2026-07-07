# CloudLens on AWS - CloudFormation

One-click AWS CloudFormation templates for the Keysight CloudLens visibility
stack: **vController** (formerly CLMS, the control plane), **KVO** (Keysight
Vision Orchestrator), and **vPB** (Virtual Packet Broker, the data plane). These
mirror the Azure ARM templates in the sibling `cloudlens-ansible-azure` repo.

Region: **us-east-1**. Each template uses a `RegionMap` for the CloudLens
Marketplace AMIs, so extend the mapping to add more regions.

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

[**Launch Stack**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fstack.yaml&stackName=cloudlens-stack)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fstack.yaml&stackName=cloudlens-stack
```

The stack template has `DeployKVO` and `DeployVPB` toggles (`yes`/`no`) so you
can compose exactly the components you want in one shared VPC with mgmt, data,
and tool subnets.

### Individual components

vController (CLMS):

[**Launch vController**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fclms.yaml&stackName=cloudlens-vcontroller)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fclms.yaml&stackName=cloudlens-vcontroller
```

KVO:

[**Launch KVO**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fkvo.yaml&stackName=cloudlens-kvo)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fkvo.yaml&stackName=cloudlens-kvo
```

vPB:

[**Launch vPB**](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fvpb.yaml&stackName=cloudlens-vpb)

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https%3A%2F%2Fraw.githubusercontent.com%2FKeysight-Tech%2Fcloudlens-ansible-aws%2Fmain%2Fdeploy%2Fcloudformation%2Fvpb.yaml&stackName=cloudlens-vpb
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
| `AdminIngressCidr` | Source CIDR allowed to reach SSH, vPB SSH, HTTPS, and VXLAN. Narrow this. |
| `ExistingVpcId` / `ExistingSubnetId` | Deploy into a VPC you already own. Leave blank to create a new one. |
| `VpcCidr` | CIDR for the new VPC (default `10.99.0.0/16`) when not using an existing VPC. |

## After deploy

1. Open the vController UI (see stack outputs), log in with
   `admin / Cl0udLens@dm!n`, and change the password.
2. Create a Project and copy the project key.
3. Register KVO and vPB fleets, then install CloudLens sensors on your workloads.

Stack outputs include public IP, private IP, console URL, and the SSH command
for each deployed instance.
