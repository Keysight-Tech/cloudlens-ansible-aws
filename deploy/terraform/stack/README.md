# CloudLens Stack Terraform Module

One tfvars, the whole stack. This module wraps the `clms` (vController), `kvo`, and `vpb` modules into a single deployable stack with a shared VPC. AWS-native counterpart to the Azure `stack` module, with the same flat variable surface and list-per-instance outputs.

## Quick start

```bash
cd deploy/terraform/stack
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set key_name or public_key
terraform init
terraform apply
```

Outputs include the vController console URL, the vPB management IP (SSH on port 9022), and the default console credentials.

## Components and toggles

| Toggle | Default | Effect |
|---|---|---|
| `vcontroller_count` | `1` | Number of vController instances (1-3) |
| `deploy_kvo` | `false` | Add Keysight Vision Orchestrator |
| `kvo_count` | `1` | Number of KVO instances (1-2), when deploy_kvo |
| `deploy_vpb` | `true` | Add Virtual Packet Broker(s) |
| `vpb_count` | `1` | Number of vPB instances (1-5), when deploy_vpb |
| `shared_vpc` | `true` | One VPC for all, or per-component VPCs |

vController-only: set `deploy_vpb = false`. Add orchestration: set `deploy_kvo = true`.

## Marketplace AMIs (us-east-1)

The stack pins the CloudLens Marketplace AMIs. A one-time Marketplace subscription per AMI is required on the account before apply (subscribe in the AWS Marketplace console).

| Component | AMI | Instance type |
|---|---|---|
| vController (CLMS) | `ami-0bebd5e730315337e` | `t3.xlarge` |
| KVO | `ami-017c0db8981569380` | `c5.2xlarge` (enforced) |
| vPB | `ami-0a561b450552b707d` | `t3.xlarge` (enforced) |
| Collector SVM (v6.13.0) | `ami-0c22ade3667f8d35a` | reference, not deployed by this stack |

The KVO and vPB instance types are locked by validation blocks in their child modules, so an override to an unsupported type fails at `terraform plan`.

## How shared_vpc works

- `shared_vpc = true` (default): the stack creates one VPC (`vpc_cidr`, default `10.99.0.0/16`), an internet gateway, a public route table, and one subnet per component (clms, kvo, vpb-mgmt, vpb-ingress, vpb-egress). The clms/kvo/vpb-mgmt subnets are routed to the internet gateway; the vPB ingress/egress data planes stay private. Each child module is handed the existing VPC ID and its subnet ID, so it only creates its security group, ENIs, Elastic IP, and instance. Multiple instances of a component share that component's subnet and each get their own private IP and Elastic IP.
- `shared_vpc = false`: the stack creates no network. Each child module builds its own VPC (with its own IGW, route table, and subnets) from the CIDR variables. Component VPCs are isolated from one another.

## What gets created (defaults, shared_vpc = true)

| Resource | Count | Notes |
|---|---|---|
| VPC | 1 | `cloudlens-stack-vpc` 10.99.0.0/16 |
| Internet gateway | 1 | shared |
| Route table | 1 | public, associated to clms/kvo/vpb-mgmt subnets |
| Subnets | 3+ | clms, vpb-mgmt, vpb-ingress, vpb-egress (+ kvo when enabled) |
| Security groups | 3 | vController, vPB mgmt, vPB data plane |
| Elastic IPs | 2 | vController + vPB management |
| ENIs | 3 | vPB mgmt / ingress / egress (vController uses the instance ENI) |
| Instances | 2 | vController t3.xlarge, vPB t3.xlarge |

## Outputs

Every component exposes list outputs (one entry per instance): `vcontroller_public_ips`, `vcontroller_ui_urls`, `kvo_public_ips`, `vpb_public_ips`, `vpb_ssh_commands`, `vpb_private_ips`, plus `summary` and `next_step`. Single-instance back-compat aliases `clms_public_ip` and `clms_ui_url` return the first vController.

## Cleanup

```bash
terraform destroy
```
