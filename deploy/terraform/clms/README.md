# vController (CLMS) Terraform Module

Terraform module that deploys a single Keysight CloudLens vController (the new name for CLMS) from the AWS Marketplace. The vController is the management plane: it hosts the console, holds project keys, and registers sensors. AWS-native counterpart to the Azure `clms` module, so pipelines that already run the Azure stack get the same variable and output conventions on AWS.

## Prerequisites

- AWS credentials available to Terraform (env vars, `aws sso login`, or an instance role)
- Terraform 1.5.0 or newer
- A one-time CloudLens Marketplace subscription accepted on the account (see the bottom of this file)
- An EC2 key pair (existing `key_name`) or an SSH public key to import (`public_key`)

## Quick start

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set key_name or public_key
terraform init && terraform apply
```

After about 15 minutes the vController console is reachable at the `clms_ui_url` output.

## Customization

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region (Marketplace AMIs here are us-east-1) |
| `profile` | `""` | Named AWS profile, blank uses default credential chain |
| `instance_name` | `cloudlens-vcontroller` | Instance name and resource prefix |
| `admin_username` | `cloudlens` | OS admin user (for the ssh_command output) |
| `key_name` | `""` | Existing EC2 key pair name |
| `public_key` | `""` | SSH public key to import as a new key pair |
| `ami_id` | `ami-0bebd5e730315337e` | CloudLens vController Marketplace AMI |
| `instance_type` | `t3.xlarge` | Instance type (t3.xlarge / t3.2xlarge / m5.xlarge / m5.2xlarge) |
| `root_volume_size` | `128` | Root EBS volume size in GB |
| `existing_vpc_id` | `""` | Existing VPC to deploy into (blank creates a new VPC) |
| `existing_subnet_id` | `""` | Existing subnet (required when existing_vpc_id is set) |
| `vpc_cidr` | `10.99.0.0/16` | New VPC CIDR |
| `subnet_cidr` | `10.99.1.0/24` | New subnet CIDR |
| `availability_zone` | `""` | AZ for the new subnet, blank lets AWS choose |
| `ssh_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to SSH |
| `https_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to reach the console |
| `tags` | `{}` | Tags applied to every resource |

## Instance type

The vController runs comfortably on `t3.xlarge` (4 vCPU, 16 GB), which is the Keysight-recommended size for this AMI. The module validates `instance_type` against a short allow list so a typo cannot silently launch an undersized or wrong-family instance.

## Output

| Output | Meaning |
|---|---|
| `clms_public_ip` | Elastic IP of the vController instance |
| `clms_private_ip` | Private IP of the vController instance |
| `clms_ui_url` | HTTPS URL for the vController console |
| `default_credentials` | `admin / Cl0udLens@dm!n` (change on first login) |
| `next_step` | What to do once it is up (create project, copy key) |
| `ssh_command` | SSH command for OS-level access |
| `instance_id` | EC2 instance ID |

## Accept Marketplace terms (one time per account)

The CloudLens AMIs are Marketplace images. Subscribe once in the AWS Marketplace console, or accept programmatically:

```bash
# Look up the product/offer for the AMI, then subscribe in the Marketplace console:
#   https://console.aws.amazon.com/marketplace
# The AMI ID used by this module (us-east-1):
#   ami-0bebd5e730315337e  (CloudLens vController)
aws ec2 describe-images --image-ids ami-0bebd5e730315337e --region us-east-1
```

Without an active subscription, `terraform apply` fails with an OptInRequired error at instance launch.
