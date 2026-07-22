# =====================================================================
# CloudLens Stack (AWS) - Terraform stack
# =====================================================================
# Mirrors deploy/cloudformation/stack.yaml: a self-contained VPC, a public
# management subnet plus a data subnet for vPB, security groups scoped to the
# admin CIDR, and the CloudLens appliance EC2 instances with Elastic IPs.
# vPB management SSH is on the configurable port (9022 by default), and its
# ingress/egress data NIC counts are honored via count = var.vpb_*_nics.
#
# Toggles arrive as strings ("true"/"false") from deploy-stack.sh; we convert
# them to bools once here.

terraform {
  required_version = ">= 1.3.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  deploy_kvo   = lower(var.deploy_kvo) == "true"
  deploy_vpb   = lower(var.deploy_vpb) == "true"
  zone_tapping = lower(var.deploy_kvo) == "true" && lower(var.enable_zone_tapping) == "true"
  name         = var.stack_name
}

# ---- Networking ------------------------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-igw" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_subnet" "mgmt" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.20.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = data.aws_availability_zones.available.names[0]
  tags                    = { Name = "${local.name}-mgmt-subnet" }
}

resource "aws_subnet" "data" {
  count                   = local.deploy_vpb ? 1 : 0
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.20.2.0/24"
  map_public_ip_on_launch = false
  availability_zone       = data.aws_availability_zones.available.names[0]
  tags                    = { Name = "${local.name}-data-subnet" }
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-rt" }
}

resource "aws_route_table_association" "mgmt" {
  subnet_id      = aws_subnet.mgmt.id
  route_table_id = aws_route_table.this.id
}

# ---- Security groups -------------------------------------------------
resource "aws_security_group" "controller" {
  name        = "${local.name}-controller-sg"
  description = "vController / KVO UI (443) and SSH (22)"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  ingress {
    description = "HTTPS UI"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-controller-sg" }
}

resource "aws_security_group" "vpb" {
  count       = local.deploy_vpb ? 1 : 0
  name        = "${local.name}-vpb-sg"
  description = "vPB management SSH and UI"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "vPB management SSH"
    from_port   = var.vpb_ssh_port
    to_port     = var.vpb_ssh_port
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  ingress {
    description = "HTTPS UI"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-vpb-sg" }
}

resource "aws_security_group" "vpb_data" {
  count       = local.deploy_vpb ? 1 : 0
  name        = "${local.name}-vpb-data-sg"
  description = "vPB data-plane ingress/egress (tunneled monitored traffic)"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.20.0.0/16"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-vpb-data-sg" }
}

# ---- vController -----------------------------------------------------
resource "aws_instance" "controller" {
  ami                    = var.controller_ami
  instance_type          = var.controller_type
  key_name               = var.key_name
  subnet_id              = aws_subnet.mgmt.id
  vpc_security_group_ids = [aws_security_group.controller.id]
  tags = {
    Name           = "${local.name}-vcontroller"
    cloudlens-role = "vcontroller"
  }
}

resource "aws_eip" "controller" {
  domain   = "vpc"
  instance = aws_instance.controller.id
  tags     = { Name = "${local.name}-vcontroller-eip" }
}

# ---- KVO Zone Tapping IAM (optional, enable_zone_tapping="true") ------
# Lets KVO deploy collector Service VMs and create AWS VPC Traffic Mirror
# sessions. Same least-privilege policy as the CloudFormation path, sourced from
# the single JSON file so both stay in sync. Gated so the base suite deploys for
# principals without IAM rights (leave enable_zone_tapping="false").
resource "aws_iam_role" "kvo_zonetap" {
  count = local.zone_tapping ? 1 : 0
  name  = "${local.name}-kvo-zonetap"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "kvo_zonetap" {
  count  = local.zone_tapping ? 1 : 0
  name   = "CloudLensZoneTap"
  role   = aws_iam_role.kvo_zonetap[0].id
  policy = file("${path.module}/../iam/cloudlens-zonetap-policy.json")
}

resource "aws_iam_instance_profile" "kvo_zonetap" {
  count = local.zone_tapping ? 1 : 0
  name  = "${local.name}-kvo-zonetap"
  role  = aws_iam_role.kvo_zonetap[0].name
}

# ---- KVO (optional) --------------------------------------------------
resource "aws_instance" "kvo" {
  count                  = local.deploy_kvo ? 1 : 0
  ami                    = var.kvo_ami
  instance_type          = var.kvo_type
  key_name               = var.key_name
  subnet_id              = aws_subnet.mgmt.id
  vpc_security_group_ids = [aws_security_group.controller.id]
  iam_instance_profile   = local.zone_tapping ? aws_iam_instance_profile.kvo_zonetap[0].name : null
  tags = {
    Name           = "${local.name}-kvo"
    cloudlens-role = "kvo"
  }
}

resource "aws_eip" "kvo" {
  count    = local.deploy_kvo ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.kvo[0].id
  tags     = { Name = "${local.name}-kvo-eip" }
}

# ---- vPB (optional) --------------------------------------------------
# Management ENI is device 0. Extra data NICs (ingress fan-in, egress fan-out)
# scale via count = var.vpb_ingress_nics / var.vpb_egress_nics.
resource "aws_network_interface" "vpb_ingress" {
  count           = local.deploy_vpb ? var.vpb_ingress_nics : 0
  subnet_id       = aws_subnet.data[0].id
  security_groups = [aws_security_group.vpb_data[0].id]
  description     = "${local.name}-vpb-ingress-${count.index}"
  tags            = { Name = "${local.name}-vpb-ingress-${count.index}" }
}

resource "aws_network_interface" "vpb_egress" {
  count           = local.deploy_vpb ? var.vpb_egress_nics : 0
  subnet_id       = aws_subnet.data[0].id
  security_groups = [aws_security_group.vpb_data[0].id]
  description     = "${local.name}-vpb-egress-${count.index}"
  tags            = { Name = "${local.name}-vpb-egress-${count.index}" }
}

resource "aws_instance" "vpb" {
  count                  = local.deploy_vpb ? 1 : 0
  ami                    = var.vpb_ami
  instance_type          = var.vpb_type
  key_name               = var.key_name
  source_dest_check      = false
  subnet_id              = aws_subnet.mgmt.id
  vpc_security_group_ids = [aws_security_group.vpb[0].id]
  tags = {
    Name           = "${local.name}-vpb"
    cloudlens-role = "vpb"
  }
}

# Attach ingress data NICs starting at device index 1, egress after them.
resource "aws_network_interface_attachment" "vpb_ingress" {
  count                = local.deploy_vpb ? var.vpb_ingress_nics : 0
  instance_id          = aws_instance.vpb[0].id
  network_interface_id = aws_network_interface.vpb_ingress[count.index].id
  device_index         = 1 + count.index
}

resource "aws_network_interface_attachment" "vpb_egress" {
  count                = local.deploy_vpb ? var.vpb_egress_nics : 0
  instance_id          = aws_instance.vpb[0].id
  network_interface_id = aws_network_interface.vpb_egress[count.index].id
  device_index         = 1 + var.vpb_ingress_nics + count.index
}

resource "aws_eip" "vpb" {
  count    = local.deploy_vpb ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.vpb[0].id
  tags     = { Name = "${local.name}-vpb-eip" }
}
