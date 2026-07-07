############################################
# Keysight CloudLens vController (formerly CLMS) Marketplace deployment
#
# Single EC2 instance from the CloudLens AWS Marketplace AMI, with a VPC,
# public subnet, internet gateway, security group, key pair, and Elastic IP.
# AWS-native counterpart to the Azure clms module.
############################################

locals {
  create_new_vpc = var.existing_vpc_id == ""

  vpc_name    = "${var.instance_name}-vpc"
  subnet_name = "${var.instance_name}-subnet"

  # Use an existing EC2 key pair by name, or create one from a supplied
  # public key. When both are blank the instance launches with no key.
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

resource "aws_subnet" "clms" {
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

resource "aws_route_table_association" "clms" {
  count = local.create_new_vpc ? 1 : 0

  subnet_id      = aws_subnet.clms[0].id
  route_table_id = aws_route_table.public[0].id
}

locals {
  vpc_id    = local.create_new_vpc ? aws_vpc.this[0].id : var.existing_vpc_id
  subnet_id = local.create_new_vpc ? aws_subnet.clms[0].id : var.existing_subnet_id
}

# ---------------------------------------------------------------------
# Security group: SSH (22) + HTTPS console (443)
# ---------------------------------------------------------------------
resource "aws_security_group" "clms" {
  name        = "${var.instance_name}-sg"
  description = "CloudLens vController: SSH and HTTPS console"
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
# vController EC2 instance from the CloudLens Marketplace AMI
# ---------------------------------------------------------------------
resource "aws_instance" "clms" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.clms.id]
  key_name                    = local.key_name != "" ? local.key_name : null
  associate_public_ip_address = false # a dedicated Elastic IP is attached below

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  tags = merge(var.tags, {
    Name      = var.instance_name
    component = "vcontroller"
  })
}

# Stable public address for the console (mirrors the Azure Static public IP).
resource "aws_eip" "clms" {
  domain   = "vpc"
  instance = aws_instance.clms.id
  tags     = merge(var.tags, { Name = "${var.instance_name}-eip" })

  depends_on = [aws_internet_gateway.this]
}
