############################################
# Keysight CloudLens Virtual Packet Broker (vPB) Marketplace deployment
#
# Single EC2 instance from the CloudLens AWS Marketplace AMI with three
# planes of ENIs: management (public, primary), ingress (mirror in), and
# egress (mirror out). Data plane ENIs disable source/dest check, the AWS
# equivalent of Azure IP forwarding. AWS-native counterpart to the Azure
# vpb module.
#
# The vPB Marketplace AMI is qualified on t3.xlarge ONLY, enforced via a
# validation block. vPB SSH listens on port 9022.
############################################

locals {
  create_new_vpc = var.existing_vpc_id == ""

  vpc_name            = "${var.instance_name}-vpc"
  mgmt_subnet_name    = "${var.instance_name}-mgmt"
  ingress_subnet_name = "${var.instance_name}-ingress"
  egress_subnet_name  = "${var.instance_name}-egress"

  key_name = var.public_key != "" ? aws_key_pair.this[0].key_name : var.key_name

  # Auto-bootstrap runs at first boot on the management plane, which has the
  # internet route. Mirrors the Azure CustomScript extension.
  bootstrap_user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    curl -fsSL ${var.bootstrap_script_url} -o /tmp/bootstrap-vpb.sh
    bash /tmp/bootstrap-vpb.sh > /var/log/cloudlens-bootstrap.log 2>&1
  EOT
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
# VPC + three subnets (create only when no existing VPC supplied)
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

resource "aws_subnet" "mgmt" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.mgmt_subnet_cidr
  availability_zone       = var.availability_zone != "" ? var.availability_zone : null
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = local.mgmt_subnet_name })
}

resource "aws_subnet" "ingress" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id            = aws_vpc.this[0].id
  cidr_block        = var.ingress_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.ingress_subnet_name })
}

resource "aws_subnet" "egress" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id            = aws_vpc.this[0].id
  cidr_block        = var.egress_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.egress_subnet_name })
}

# Public route table for the management subnet (internet via the IGW).
resource "aws_route_table" "public" {
  count = local.create_new_vpc ? 1 : 0

  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = merge(var.tags, { Name = "${local.vpc_name}-public-rt" })
}

resource "aws_route_table_association" "mgmt" {
  count = local.create_new_vpc ? 1 : 0

  subnet_id      = aws_subnet.mgmt[0].id
  route_table_id = aws_route_table.public[0].id
}

locals {
  vpc_id            = local.create_new_vpc ? aws_vpc.this[0].id : var.existing_vpc_id
  mgmt_subnet_id    = local.create_new_vpc ? aws_subnet.mgmt[0].id : var.existing_mgmt_subnet_id
  ingress_subnet_id = local.create_new_vpc ? aws_subnet.ingress[0].id : var.existing_ingress_subnet_id
  egress_subnet_id  = local.create_new_vpc ? aws_subnet.egress[0].id : var.existing_egress_subnet_id
}

# ---------------------------------------------------------------------
# Security groups
#   mgmt: SSH 22, vPB SSH 9022, HTTPS 443, VXLAN (4789 + 10800-10801)
#   data: permissive within the VPC for mirrored packets
# ---------------------------------------------------------------------
resource "aws_security_group" "vpb_mgmt" {
  name        = "${var.instance_name}-mgmt-sg"
  description = "CloudLens vPB management: SSH, vPB SSH 9022, HTTPS, VXLAN"
  vpc_id      = local.vpc_id
  tags        = merge(var.tags, { Name = "${var.instance_name}-mgmt-sg" })

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.ssh_ingress_cidrs
  }

  ingress {
    description = "vPB SSH (KCOS)"
    from_port   = 9022
    to_port     = 9022
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

  ingress {
    description = "VXLAN standard"
    from_port   = 4789
    to_port     = 4789
    protocol    = "udp"
    cidr_blocks = var.vxlan_ingress_cidrs
  }

  ingress {
    description = "VXLAN Keysight"
    from_port   = 10800
    to_port     = 10801
    protocol    = "udp"
    cidr_blocks = var.vxlan_ingress_cidrs
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "vpb_data" {
  name        = "${var.instance_name}-data-sg"
  description = "CloudLens vPB data plane: mirrored traffic within the VPC"
  vpc_id      = local.vpc_id
  tags        = merge(var.tags, { Name = "${var.instance_name}-data-sg" })

  ingress {
    description = "All traffic from within the VPC (mirror in/out)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = var.data_plane_ingress_cidrs
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
# Network interfaces
#   mgmt: primary, public IP via EIP, regular networking
#   ingress/egress (1-3 each): source/dest check disabled (IP forwarding)
# ---------------------------------------------------------------------
resource "aws_network_interface" "mgmt" {
  subnet_id       = local.mgmt_subnet_id
  security_groups = [aws_security_group.vpb_mgmt.id]
  tags            = merge(var.tags, { Name = "${var.instance_name}-mgmt-nic" })
}

resource "aws_network_interface" "ingress" {
  count = var.ingress_nic_count

  subnet_id         = local.ingress_subnet_id
  security_groups   = [aws_security_group.vpb_data.id]
  source_dest_check = false
  tags              = merge(var.tags, { Name = "${var.instance_name}-ingress-${count.index + 1}" })
}

resource "aws_network_interface" "egress" {
  count = var.egress_nic_count

  subnet_id         = local.egress_subnet_id
  security_groups   = [aws_security_group.vpb_data.id]
  source_dest_check = false
  tags              = merge(var.tags, { Name = "${var.instance_name}-egress-${count.index + 1}" })
}

# Stable public address on the management ENI (mirrors Azure Static public IP).
resource "aws_eip" "vpb_mgmt" {
  domain            = "vpc"
  network_interface = aws_network_interface.mgmt.id
  tags              = merge(var.tags, { Name = "${var.instance_name}-mgmt-eip" })

  depends_on = [aws_internet_gateway.this]
}

# ---------------------------------------------------------------------
# vPB EC2 instance from the CloudLens Marketplace AMI (t3.xlarge only)
#
# Device index order: management (0), then all ingress NICs, then all
# egress NICs. Total ENIs = 1 + ingress_nic_count + egress_nic_count and
# must fit the t3.xlarge limit of 4 ENIs (so ingress + egress <= 3).
# ---------------------------------------------------------------------
resource "aws_instance" "vpb" {
  ami           = var.ami_id
  instance_type = var.instance_type
  key_name      = local.key_name != "" ? local.key_name : null
  user_data     = var.enable_auto_bootstrap ? local.bootstrap_user_data : null

  network_interface {
    network_interface_id = aws_network_interface.mgmt.id
    device_index         = 0
  }

  dynamic "network_interface" {
    for_each = {
      for idx, ni in aws_network_interface.ingress :
      tostring(idx) => { id = ni.id, device_index = idx + 1 }
    }
    content {
      network_interface_id = network_interface.value.id
      device_index         = network_interface.value.device_index
    }
  }

  dynamic "network_interface" {
    for_each = {
      for idx, ni in aws_network_interface.egress :
      tostring(idx) => { id = ni.id, device_index = idx + 1 + var.ingress_nic_count }
    }
    content {
      network_interface_id = network_interface.value.id
      device_index         = network_interface.value.device_index
    }
  }

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
    encrypted   = true
  }

  tags = merge(var.tags, {
    Name      = var.instance_name
    component = "vpb"
  })

  # t3.xlarge supports at most 4 ENIs. Enforce 1 mgmt + ingress + egress <= 4
  # here because it spans two variables and cannot live in a single validation.
  lifecycle {
    precondition {
      condition     = (1 + var.ingress_nic_count + var.egress_nic_count) <= 4
      error_message = "Total ENIs (1 management + ingress_nic_count + egress_nic_count) must be 4 or fewer, the t3.xlarge limit. Reduce ingress_nic_count and/or egress_nic_count."
    }
  }
}
