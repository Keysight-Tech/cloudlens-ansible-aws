# KVO Terraform Module

Terraform module that deploys a single Keysight Vision Orchestrator (KVO) instance from the AWS Marketplace. KVO is the centralized orchestration layer that manages a fleet of vControllers and vPBs. AWS-native counterpart to the Azure `kvo` module.

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

After about 15 minutes the KVO console is reachable at the `kvo_ui_url` output.

## Instance type is fixed

The KVO Marketplace AMI is qualified on **c5.2xlarge only**. The `instance_type` variable defaults to `c5.2xlarge` and a validation block rejects any other value, so an accidental change fails fast at `terraform plan` rather than launching an unsupported instance.

## Customization

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region (Marketplace AMIs here are us-east-1) |
| `profile` | `""` | Named AWS profile, blank uses default credential chain |
| `instance_name` | `cloudlens-kvo` | Instance name and resource prefix |
| `admin_username` | `cloudlens` | OS admin user (for the ssh_command output) |
| `key_name` | `""` | Existing EC2 key pair name |
| `public_key` | `""` | SSH public key to import as a new key pair |
| `ami_id` | `ami-017c0db8981569380` | KVO Marketplace AMI |
| `instance_type` | `c5.2xlarge` | Fixed: c5.2xlarge only |
| `root_volume_size` | `128` | Root EBS volume size in GB |
| `existing_vpc_id` | `""` | Existing VPC to deploy into (blank creates a new VPC) |
| `existing_subnet_id` | `""` | Existing subnet (required when existing_vpc_id is set) |
| `vpc_cidr` | `10.99.0.0/16` | New VPC CIDR |
| `subnet_cidr` | `10.99.5.0/24` | New subnet CIDR |
| `availability_zone` | `""` | AZ for the new subnet, blank lets AWS choose |
| `ssh_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to SSH |
| `https_ingress_cidrs` | `["0.0.0.0/0"]` | CIDRs allowed to reach the console |
| `tags` | `{}` | Tags applied to every resource |

## Output

| Output | Meaning |
|---|---|
| `kvo_public_ip` | Elastic IP of the KVO instance |
| `kvo_private_ip` | Private IP of the KVO instance |
| `kvo_ui_url` | HTTPS URL for the KVO console |
| `default_credentials` | See Keysight KVO documentation |
| `next_step` | What to do once KVO is up |
| `ssh_command` | SSH command for OS-level access |
| `instance_id` | EC2 instance ID |

## Accept Marketplace terms (one time per account)

Subscribe once in the AWS Marketplace console before applying:

```bash
#   https://console.aws.amazon.com/marketplace
# The AMI ID used by this module (us-east-1):
#   ami-017c0db8981569380  (Keysight Vision Orchestrator)
aws ec2 describe-images --image-ids ami-017c0db8981569380 --region us-east-1
```

Without an active subscription, `terraform apply` fails with an OptInRequired error at instance launch.
