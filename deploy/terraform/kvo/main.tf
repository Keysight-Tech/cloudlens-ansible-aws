############################################
# Keysight Vision Orchestrator (KVO) Marketplace deployment
#
# Single EC2 instance from the CloudLens AWS Marketplace AMI, with a VPC,
# public subnet, internet gateway, security group, key pair, and Elastic IP.
# AWS-native counterpart to the Azure kvo module.
#
# The KVO Marketplace AMI is qualified on c5.2xlarge ONLY. The instance_type
# variable enforces this with a validation block.
############################################

locals {
  create_new_vpc = var.existing_vpc_id == ""

  vpc_name    = "${var.instance_name}-vpc"
  subnet_name = "${var.instance_name}-subnet"

  key_name = var.public_key != "" ? aws_key_pair.this[0].key_name : var.key_name
}

# ---------------------------------------------------------------------
# Key pair (optional): create only when a public key is supplied
# ---------------------------------------------------------------------
resource "aws_key_pair" "this" {
  count = var.public_key != "" ? 1 : 0

  key_name   = "${var.instance_name}-key"
  public_key = var.public_key
  tags       = var.tags
}

# ---------------------------------------------------------------------
# VPC + public networking (create only when no existing VPC supplied)
# ---------------------------------------------------------------------
resource "aws_vpc" "this" {
  count = local.create_new_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(var.tags, { Name = local.vpc_name })
}

resource "aws_internet_gateway" "this" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id = aws_vpc.this[0].id
  tags   = merge(var.tags, { Name = "${local.vpc_name}-igw" })
}

resource "aws_subnet" "kvo" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.subnet_cidr
  availability_zone       = var.availability_zone != "" ? var.availability_zone : null
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = local.subnet_name })
}

resource "aws_route_table" "public" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = merge(var.tags, { Name = "${local.vpc_name}-public-rt" })
}

resource "aws_route_table_association" "kvo" {
  count = local.create_new_vpc ? 1 : 0

  subnet_id      = aws_subnet.kvo[0].id
  route_table_id = aws_route_table.public[0].id
}

locals {
  vpc_id    = local.create_new_vpc ? aws_vpc.this[0].id : var.existing_vpc_id
  subnet_id = local.create_new_vpc ? aws_subnet.kvo[0].id : var.existing_subnet_id
}

# ---------------------------------------------------------------------
# Security group: SSH (22) + HTTPS console (443)
# ---------------------------------------------------------------------
resource "aws_security_group" "kvo" {
  name        = "${var.instance_name}-sg"
  description = "CloudLens KVO: SSH and HTTPS console"
  vpc_id      = local.vpc_id
  tags        = merge(var.tags, { Name = "${var.instance_name}-sg" })

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.ssh_ingress_cidrs
  }

  ingress {
    description = "HTTPS console"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.https_ingress_cidrs
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------------
# KVO EC2 instance from the CloudLens Marketplace AMI (c5.2xlarge only)
# ---------------------------------------------------------------------
resource "aws_instance" "kvo" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.kvo.id]
  key_name                    = local.key_name != "" ? local.key_name : null
  associate_public_ip_address = false # a dedicated Elastic IP is attached below

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  tags = merge(var.tags, {
    Name      = var.instance_name
    component = "kvo"
  })
}

resource "aws_eip" "kvo" {
  domain   = "vpc"
  instance = aws_instance.kvo.id
  tags     = merge(var.tags, { Name = "${var.instance_name}-eip" })

  depends_on = [aws_internet_gateway.this]
}
