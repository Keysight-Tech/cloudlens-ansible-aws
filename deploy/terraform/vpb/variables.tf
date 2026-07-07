variable "region" {
  description = "AWS region for the deployment. CloudLens Marketplace AMIs in this repo are published in us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "profile" {
  description = "Named AWS CLI/SDK profile to authenticate with. Leave blank to use the default credential chain (env vars, SSO, instance role)."
  type        = string
  default     = ""
}

variable "instance_name" {
  description = "Name of the vPB EC2 instance. Also used as a prefix for the VPC, subnets, NICs, security groups, key pair, and Elastic IP."
  type        = string
  default     = "cloudlens-vpb"
}

variable "admin_username" {
  description = "OS-level admin username used to build the SSH command output. vPB SSH listens on port 9022."
  type        = string
  default     = "cloudlens"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair to attach for SSH. Leave blank and set public_key to have Terraform create one instead."
  type        = string
  default     = ""
}

variable "public_key" {
  description = "SSH public key material (ssh-rsa/ssh-ed25519 ...). When set, Terraform creates an EC2 key pair from it. Leave blank to use key_name."
  type        = string
  default     = ""
}

variable "ami_id" {
  description = "vPB Marketplace AMI ID. Requires a one-time Marketplace subscription on the account. Default is the us-east-1 CloudLens vPB image."
  type        = string
  default     = "ami-0a561b450552b707d"
}

variable "instance_type" {
  description = "EC2 instance type for vPB. The vPB Marketplace AMI is qualified on t3.xlarge ONLY, so this is the only permitted value."
  type        = string
  default     = "t3.xlarge"

  validation {
    condition     = var.instance_type == "t3.xlarge"
    error_message = "instance_type must be t3.xlarge. The vPB Marketplace AMI is qualified on t3.xlarge only."
  }
}

variable "root_volume_size" {
  description = "Root EBS volume size in GB."
  type        = number
  default     = 64
}

variable "existing_vpc_id" {
  description = "ID of an existing VPC to deploy into. Leave blank to create a new VPC with three subnets. When set, the three existing subnet IDs are required."
  type        = string
  default     = ""
}

variable "existing_mgmt_subnet_id" {
  description = "Existing management subnet ID. Required when existing_vpc_id is set."
  type        = string
  default     = ""
}

variable "existing_ingress_subnet_id" {
  description = "Existing ingress (mirror in) subnet ID. Required when existing_vpc_id is set."
  type        = string
  default     = ""
}

variable "existing_egress_subnet_id" {
  description = "Existing egress (mirror out) subnet ID. Required when existing_vpc_id is set."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for the new VPC, used only when creating one."
  type        = string
  default     = "10.99.0.0/16"
}

variable "mgmt_subnet_cidr" {
  description = "Management subnet CIDR, used only when creating a new VPC."
  type        = string
  default     = "10.99.2.0/24"
}

variable "ingress_subnet_cidr" {
  description = "Ingress (mirror traffic in) subnet CIDR, used only when creating a new VPC."
  type        = string
  default     = "10.99.3.0/24"
}

variable "egress_subnet_cidr" {
  description = "Egress (mirror traffic out) subnet CIDR, used only when creating a new VPC."
  type        = string
  default     = "10.99.4.0/24"
}

variable "availability_zone" {
  description = "Availability zone for the three vPB subnets. A single instance cannot attach ENIs across AZs, so all three subnets MUST share one AZ. Defaults to us-east-1a to keep them together; change it if you change region."
  type        = string
  default     = "us-east-1a"
}

variable "ingress_nic_count" {
  description = "Number of ingress NICs (receive mirror traffic). Default 1. Use 2-3 for fan-in from multiple sources. Total NICs (1 mgmt + ingress + egress) must be <= 4 on t3.xlarge."
  type        = number
  default     = 1

  validation {
    condition     = var.ingress_nic_count >= 1 && var.ingress_nic_count <= 3
    error_message = "ingress_nic_count must be between 1 and 3."
  }
}

variable "egress_nic_count" {
  description = "Number of egress NICs (forward to monitoring tools). Default 1. Use 2-3 for fan-out to multiple tools. Total NICs (1 mgmt + ingress + egress) must be <= 4 on t3.xlarge."
  type        = number
  default     = 1

  validation {
    condition     = var.egress_nic_count >= 1 && var.egress_nic_count <= 3
    error_message = "egress_nic_count must be between 1 and 3."
  }
}

variable "ssh_ingress_cidrs" {
  description = "CIDR blocks allowed to reach SSH (22) and vPB SSH (9022). Narrow this to your admin network in production."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "https_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the HTTPS console (443)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "vxlan_ingress_cidrs" {
  description = "CIDR blocks allowed to send VXLAN mirror traffic (UDP 4789 and 10800-10801) to the management plane."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "data_plane_ingress_cidrs" {
  description = "CIDR blocks allowed on the ingress/egress data plane ENIs. Defaults to the demo VPC range."
  type        = list(string)
  default     = ["10.99.0.0/16"]
}

variable "enable_auto_bootstrap" {
  description = "Run scripts/bootstrap-vpb.sh automatically at first boot via EC2 user_data. Installs system-wide KUBECONFIG and the sudo vpb wrapper so SSH-in just works. Disable for air-gapped or custom-bootstrap scenarios."
  type        = bool
  default     = true
}

variable "bootstrap_script_url" {
  description = "URL to the bootstrap script fetched by user_data. Override only if you fork the repo."
  type        = string
  default     = "https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/scripts/bootstrap-vpb.sh"
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}
