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
  description = "Name of the vController EC2 instance. Also used as a prefix for the VPC, subnet, security group, key pair, and Elastic IP."
  type        = string
  default     = "cloudlens-vcontroller"
}

variable "admin_username" {
  description = "OS-level admin username used to build the SSH command output. The CloudLens vController AMI ships with this login."
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
  description = "CloudLens vController (formerly CLMS) Marketplace AMI ID. Requires a one-time Marketplace subscription on the account. Default is the us-east-1 CloudLens vController image."
  type        = string
  default     = "ami-0bebd5e730315337e"
}

variable "instance_type" {
  description = "EC2 instance type for the vController. t3.xlarge (4 vCPU, 16 GB) is the recommended size for the CloudLens vController AMI."
  type        = string
  default     = "t3.xlarge"

  validation {
    condition = contains([
      "t3.xlarge",
      "t3.2xlarge",
      "m5.xlarge",
      "m5.2xlarge"
    ], var.instance_type)
    error_message = "instance_type must be one of t3.xlarge, t3.2xlarge, m5.xlarge, or m5.2xlarge. t3.xlarge is the CloudLens vController default."
  }
}

variable "root_volume_size" {
  description = "Root EBS volume size in GB."
  type        = number
  default     = 128
}

variable "existing_vpc_id" {
  description = "ID of an existing VPC to deploy into. Leave blank to create a new VPC. When set, existing_subnet_id is required and internet routing must already exist."
  type        = string
  default     = ""
}

variable "existing_subnet_id" {
  description = "ID of an existing subnet to launch the instance into. Required when existing_vpc_id is set."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for the new VPC, used only when creating one."
  type        = string
  default     = "10.99.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR block for the new subnet, used only when creating a new VPC."
  type        = string
  default     = "10.99.1.0/24"
}

variable "availability_zone" {
  description = "Availability zone for the new subnet. Leave blank to let AWS choose one in the region."
  type        = string
  default     = ""
}

variable "ssh_ingress_cidrs" {
  description = "CIDR blocks allowed to reach SSH (port 22). Narrow this to your admin network in production."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "https_ingress_cidrs" {
  description = "CIDR blocks allowed to reach the HTTPS console (port 443)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}
