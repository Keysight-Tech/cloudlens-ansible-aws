###############################################################################
# CloudLens Stack module: shared flat variable surface
#
# These variables are forwarded to the nested clms, kvo, and vpb modules.
# The goal is "one tfvars file, the whole stack."
###############################################################################

variable "region" {
  description = "AWS region for the whole stack. CloudLens Marketplace AMIs in this repo are published in us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "profile" {
  description = "Named AWS CLI/SDK profile. Leave blank to use the default credential chain (env vars, SSO, instance role)."
  type        = string
  default     = ""
}

variable "admin_username" {
  description = "OS-level admin username used across all components (for the ssh_command outputs)."
  type        = string
  default     = "cloudlens"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair to attach to every instance. Leave blank and set public_key to have Terraform create one instead."
  type        = string
  default     = ""
}

variable "public_key" {
  description = "SSH public key material shared by all components. When set, each child module imports it as a key pair. Leave blank to use key_name."
  type        = string
  default     = ""
}

###############################################################################
# vController (formerly CLMS) knobs
###############################################################################

variable "clms_instance_name" {
  description = "Base name of the vController instances. A -N index is appended per instance."
  type        = string
  default     = "cloudlens-vcontroller"
}

variable "clms_ami_id" {
  description = "vController Marketplace AMI ID (us-east-1)."
  type        = string
  default     = "ami-0bebd5e730315337e"
}

variable "clms_instance_type" {
  description = "Instance type for the vController. Allowed: t3.xlarge, t3.2xlarge, m5.xlarge, m5.2xlarge."
  type        = string
  default     = "t3.xlarge"
}

variable "vcontroller_count" {
  description = "Number of vController instances (1-3). Default 1. Use 2 for HA, 3 for multi-region."
  type        = number
  default     = 1

  validation {
    condition     = var.vcontroller_count >= 1 && var.vcontroller_count <= 3
    error_message = "vcontroller_count must be between 1 and 3."
  }
}

###############################################################################
# KVO knobs (Keysight Vision Orchestrator)
###############################################################################

variable "deploy_kvo" {
  description = "Toggle for KVO. Set true to deploy Keysight Vision Orchestrator alongside the vController."
  type        = bool
  default     = false
}

variable "kvo_instance_name" {
  description = "Base name of the KVO instances. A -N index is appended per instance."
  type        = string
  default     = "cloudlens-kvo"
}

variable "kvo_ami_id" {
  description = "KVO Marketplace AMI ID (us-east-1)."
  type        = string
  default     = "ami-017c0db8981569380"
}

variable "kvo_instance_type" {
  description = "Instance type for KVO. The KVO AMI is qualified on c5.2xlarge only; the child module rejects anything else."
  type        = string
  default     = "c5.2xlarge"
}

variable "kvo_count" {
  description = "Number of KVO instances (1-2). Default 1. Use 2 for an HA pair."
  type        = number
  default     = 1

  validation {
    condition     = var.kvo_count >= 1 && var.kvo_count <= 2
    error_message = "kvo_count must be between 1 and 2."
  }
}

###############################################################################
# vPB knobs
###############################################################################

variable "deploy_vpb" {
  description = "Toggle for vPB. Set false to deploy the vController (and optional KVO) only."
  type        = bool
  default     = true
}

variable "vpb_instance_name" {
  description = "Base name of the vPB instances. A -N index is appended per instance."
  type        = string
  default     = "cloudlens-vpb"
}

variable "vpb_ami_id" {
  description = "vPB Marketplace AMI ID (us-east-1)."
  type        = string
  default     = "ami-0a561b450552b707d"
}

variable "vpb_instance_type" {
  description = "Instance type for vPB. The vPB AMI is qualified on t3.xlarge only; the child module rejects anything else."
  type        = string
  default     = "t3.xlarge"
}

variable "vpb_count" {
  description = "Number of vPB instances (1-5). Default 1. Use 2-5 for data-plane scale-out."
  type        = number
  default     = 1

  validation {
    condition     = var.vpb_count >= 1 && var.vpb_count <= 5
    error_message = "vpb_count must be between 1 and 5."
  }
}

variable "vpb_ingress_nic_count" {
  description = "Number of vPB ingress NICs (1-3). Total NICs (1 mgmt + ingress + egress) must be <= 4 on t3.xlarge."
  type        = number
  default     = 1

  validation {
    condition     = var.vpb_ingress_nic_count >= 1 && var.vpb_ingress_nic_count <= 3
    error_message = "vpb_ingress_nic_count must be between 1 and 3."
  }
}

variable "vpb_egress_nic_count" {
  description = "Number of vPB egress NICs (1-3). Total NICs (1 mgmt + ingress + egress) must be <= 4 on t3.xlarge."
  type        = number
  default     = 1

  validation {
    condition     = var.vpb_egress_nic_count >= 1 && var.vpb_egress_nic_count <= 3
    error_message = "vpb_egress_nic_count must be between 1 and 3."
  }
}

variable "vpb_enable_auto_bootstrap" {
  description = "Run scripts/bootstrap-vpb.sh automatically at first boot via user_data. Installs KUBECONFIG system-wide and the sudo vpb wrapper so SSH-in just works. Disable for air-gapped scenarios."
  type        = bool
  default     = true
}

###############################################################################
# Networking
###############################################################################

variable "shared_vpc" {
  description = "If true, all components share one VPC with distinct subnets. If false, each component gets its own VPC. Defaults to true for the simplest one-shot stack."
  type        = bool
  default     = true
}

variable "vpc_name" {
  description = "Name tag for the shared VPC (used when shared_vpc = true)."
  type        = string
  default     = "cloudlens-stack-vpc"
}

variable "vpc_cidr" {
  description = "VPC CIDR. Used for the shared VPC, or forwarded to each child VPC when shared_vpc is false."
  type        = string
  default     = "10.99.0.0/16"
}

variable "availability_zone" {
  description = "Availability zone for the shared subnets. A single vPB instance cannot attach ENIs across AZs, so its mgmt/ingress/egress subnets MUST share one AZ. Defaults to us-east-1a; change it if you change region."
  type        = string
  default     = "us-east-1a"
}

variable "clms_subnet_cidr" {
  description = "Subnet CIDR for the vController (formerly CLMS)."
  type        = string
  default     = "10.99.1.0/24"
}

variable "kvo_subnet_cidr" {
  description = "Subnet CIDR for KVO (used when deploy_kvo is true)."
  type        = string
  default     = "10.99.5.0/24"
}

variable "vpb_mgmt_subnet_cidr" {
  description = "vPB management subnet CIDR (used when deploy_vpb is true)."
  type        = string
  default     = "10.99.2.0/24"
}

variable "vpb_ingress_subnet_cidr" {
  description = "vPB ingress (mirror in) subnet CIDR."
  type        = string
  default     = "10.99.3.0/24"
}

variable "vpb_egress_subnet_cidr" {
  description = "vPB egress (mirror out) subnet CIDR."
  type        = string
  default     = "10.99.4.0/24"
}

variable "ssh_ingress_cidrs" {
  description = "CIDR blocks allowed to reach SSH (22) and vPB SSH (9022) across the stack. Narrow this in production."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "https_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the HTTPS consoles (443) across the stack."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags applied to every created resource."
  type        = map(string)
  default = {
    project = "cloudlens"
    stack   = "vcontroller-kvo-vpb"
  }
}
