# vPB Terraform Module

Terraform module that deploys a Keysight CloudLens Virtual Packet Broker (vPB) instance from the AWS Marketplace. Three ENIs (management, ingress, egress), source/dest check disabled on the data plane ENIs (the AWS equivalent of Azure IP forwarding), and a security group opening SSH (22), vPB SSH (9022), HTTPS (443), and VXLAN (UDP 4789 plus Keysight 10800-10801). AWS-native counterpart to the Azure `vpb` module.

## Prerequisites

- AWS credentials available to Terraform (env vars, `aws sso login`, or an instance role)
- Terraform 1.5.0 or newer
- A one-time CloudLens Marketplace subscription accepted on the account
- An EC2 key pair (existing `key_name`) or an SSH public key to import (`public_key`)

## Quick start

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set key_name or public_key
terraform init && terraform apply
```

The vPB needs about 5 to 10 minutes after instance creation before SSH on port 9022 works.

## Instance type is fixed, ENIs are capped

The vPB Marketplace AMI is qualified on **t3.xlarge only**, so `instance_type` defaults to `t3.xlarge` and a validation block rejects anything else. t3.xlarge supports at most 4 ENIs, so the module also runs a precondition that `1 (mgmt) + ingress_nic_count + egress_nic_count <= 4`. Both checks fail fast at `terraform plan` rather than at apply.

## Networking

`aws_network_interface` resources model the three planes:

- **Management**: primary ENI (device index 0), the security group above, and an Elastic IP for a stable public address.
- **Ingress** (1-3): mirror traffic in, `source_dest_check = false`.
- **Egress** (1-3): mirror traffic out, `source_dest_check = false`.

When creating a new VPC the module builds three subnets (mgmt/ingress/egress) inside `10.99.0.0/16` and routes only the management subnet to the internet gateway. Pin `availability_zone` if you need all three planes in a specific AZ (they must share one AZ, which is why a single AZ variable drives all three subnets).

## Customization

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region (Marketplace AMIs here are us-east-1) |
| `profile` | `""` | Named AWS profile, blank uses default credential chain |
| `instance_name` | `cloudlens-vpb` | Instance name and resource prefix |
| `admin_username` | `cloudlens` | OS admin user (for the ssh_command output) |
| `key_name` | `""` | Existing EC2 key pair name |
| `public_key` | `""` | SSH public key to import as a new key pair |
| `ami_id` | `ami-0a561b450552b707d` | vPB Marketplace AMI |
| `instance_type` | `t3.xlarge` | Fixed: t3.xlarge only |
| `ingress_nic_count` | `1` | Ingress NICs (1-3) |
| `egress_nic_count` | `1` | Egress NICs (1-3) |
| `existing_vpc_id` | `""` | Existing VPC (blank creates a new one) |
| `existing_mgmt_subnet_id` | `""` | Existing management subnet |
| `existing_ingress_subnet_id` | `""` | Existing ingress subnet |
| `existing_egress_subnet_id` | `""` | Existing egress subnet |
| `vpc_cidr` | `10.99.0.0/16` | New VPC CIDR |
| `mgmt_subnet_cidr` | `10.99.2.0/24` | New management subnet CIDR |
| `ingress_subnet_cidr` | `10.99.3.0/24` | New ingress subnet CIDR |
| `egress_subnet_cidr` | `10.99.4.0/24` | New egress subnet CIDR |
| `availability_zone` | `us-east-1a` | AZ for all three subnets (must share one AZ) |
| `ssh_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to SSH (22 and 9022) |
| `https_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to reach the console |
| `vxlan_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to send VXLAN mirror traffic |
| `data_plane_ingress_cidrs` | `["10.99.0.0/16"]` | CIDRs allowed on the data plane ENIs |
| `enable_auto_bootstrap` | `true` | Run bootstrap-vpb.sh via user_data at first boot |
| `bootstrap_script_url` | GitHub raw URL | Bootstrap script location |
| `tags` | `{}` | Tags applied to every resource |

## Output

| Output | Meaning |
|---|---|
| `vpb_public_ip` | Elastic IP of the management NIC |
| `vpb_ssh_command` | OS-level SSH command (port 9022) |
| `vpb_cli_access` | How to reach the vPB CLI after auto-bootstrap |
| `next_step` | Configuration guidance |
| `mgmt_private_ip` | Management NIC private IP |
| `ingress_private_ips` | Ingress NIC private IPs (list) |
| `egress_private_ips` | Egress NIC private IPs (list) |
| `ingress_nic_ids` / `egress_nic_ids` | Data plane ENI IDs |
| `instance_id` | EC2 instance ID |

## Accept Marketplace terms (one time per account)

Subscribe once in the AWS Marketplace console before applying:

```bash
#   https://console.aws.amazon.com/marketplace
# The AMI ID used by this module (us-east-1):
#   ami-0a561b450552b707d  (CloudLens Virtual Packet Broker)
aws ec2 describe-images --image-ids ami-0a561b450552b707d --region us-east-1
```

Without an active subscription, `terraform apply` fails with an OptInRequired error at instance launch.
