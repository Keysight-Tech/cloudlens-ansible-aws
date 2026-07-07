# =====================================================================
# CloudLens Stack (AWS) - Terraform variables
# =====================================================================
# The Terraform stack is the alternative to deploy/cloudformation/stack.yaml.
# Unlike the CloudFormation template (which resolves AMIs from an in-template
# RegionMap), this stack takes AMI ids directly, so it works in any region
# where you have subscribed to the CloudLens Marketplace AMIs. deploy-stack.sh
# passes every variable below via -var when invoked with --iac terraform.

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "stack_name" {
  description = "Name prefix applied to every resource (Name tags)."
  type        = string
  default     = "cloudlens-stack"
}

variable "key_name" {
  description = "Existing EC2 key pair for SSH into the appliances."
  type        = string
}

variable "deploy_kvo" {
  description = "Deploy KVO alongside vController (true/false as string)."
  type        = string
  default     = "false"
}

variable "deploy_vpb" {
  description = "Deploy vPB alongside vController (true/false as string)."
  type        = string
  default     = "false"
}

variable "controller_ami" {
  description = "vController / CLMS Marketplace AMI id."
  type        = string
  default     = "ami-0bebd5e730315337e"
}
variable "controller_type" {
  type    = string
  default = "t3.xlarge"
}

variable "kvo_ami" {
  description = "KVO Marketplace AMI id."
  type        = string
  default     = "ami-017c0db8981569380"
}
variable "kvo_type" {
  type    = string
  default = "c5.2xlarge"
}

variable "vpb_ami" {
  description = "vPB Marketplace AMI id."
  type        = string
  default     = "ami-0a561b450552b707d"
}
variable "vpb_type" {
  type    = string
  default = "t3.xlarge"
}

variable "vpb_ssh_port" {
  description = "vPB management SSH port (9022 on v3.15+)."
  type        = number
  default     = 9022
}

variable "vpb_ingress_nics" {
  description = "Number of vPB ingress (fan-in) data NICs."
  type        = number
  default     = 1
}

variable "vpb_egress_nics" {
  description = "Number of vPB egress (fan-out) data NICs."
  type        = number
  default     = 1
}

variable "admin_cidr" {
  description = "Source CIDR allowed to reach UI (443) and SSH ports."
  type        = string
  default     = "0.0.0.0/0"
}
