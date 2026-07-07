############################################
# CloudLens Stack: vController + KVO + vPB from one tfvars
# (vController is the new name for what used to be called CLMS)
#
# Design:
#   - When shared_vpc = true, the stack creates one VPC, an internet gateway,
#     a public route table, and all per-component subnets (clms, kvo,
#     vpb-mgmt, vpb-ingress, vpb-egress), then points the child modules at
#     that existing VPC + subnets.
#   - When shared_vpc = false, each child module creates its own VPC.
#   - deploy_kvo = false skips the KVO module entirely.
#   - deploy_vpb = false skips the vPB module entirely.
#   - vcontroller_count / kvo_count / vpb_count fan each component out.
############################################

locals {
  # Subnet names for the shared VPC.
  clms_subnet_name        = "clms-subnet"
  kvo_subnet_name         = "kvo-subnet"
  vpb_mgmt_subnet_name    = "vpb-mgmt"
  vpb_ingress_subnet_name = "vpb-ingress"
  vpb_egress_subnet_name  = "vpb-egress"
}

# ---------------------------------------------------------------------
# Shared VPC + subnets (only when shared_vpc = true)
# ---------------------------------------------------------------------
resource "aws_vpc" "shared" {
  count = var.shared_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(var.tags, { Name = var.vpc_name })
}

resource "aws_internet_gateway" "shared" {
  count = var.shared_vpc ? 1 : 0

  vpc_id = aws_vpc.shared[0].id
  tags   = merge(var.tags, { Name = "${var.vpc_name}-igw" })
}

resource "aws_route_table" "public" {
  count = var.shared_vpc ? 1 : 0

  vpc_id = aws_vpc.shared[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.shared[0].id
  }

  tags = merge(var.tags, { Name = "${var.vpc_name}-public-rt" })
}

# vController subnet (always present in shared mode)
resource "aws_subnet" "clms" {
  count = var.shared_vpc ? 1 : 0

  vpc_id            = aws_vpc.shared[0].id
  cidr_block        = var.clms_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.clms_subnet_name })
}

resource "aws_route_table_association" "clms" {
  count = var.shared_vpc ? 1 : 0

  subnet_id      = aws_subnet.clms[0].id
  route_table_id = aws_route_table.public[0].id
}

# KVO subnet (only when deploy_kvo)
resource "aws_subnet" "kvo" {
  count = var.shared_vpc && var.deploy_kvo ? 1 : 0

  vpc_id            = aws_vpc.shared[0].id
  cidr_block        = var.kvo_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.kvo_subnet_name })
}

resource "aws_route_table_association" "kvo" {
  count = var.shared_vpc && var.deploy_kvo ? 1 : 0

  subnet_id      = aws_subnet.kvo[0].id
  route_table_id = aws_route_table.public[0].id
}

# vPB subnets (only when deploy_vpb): mgmt is public, data planes are private
resource "aws_subnet" "vpb_mgmt" {
  count = var.shared_vpc && var.deploy_vpb ? 1 : 0

  vpc_id            = aws_vpc.shared[0].id
  cidr_block        = var.vpb_mgmt_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.vpb_mgmt_subnet_name })
}

resource "aws_route_table_association" "vpb_mgmt" {
  count = var.shared_vpc && var.deploy_vpb ? 1 : 0

  subnet_id      = aws_subnet.vpb_mgmt[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_subnet" "vpb_ingress" {
  count = var.shared_vpc && var.deploy_vpb ? 1 : 0

  vpc_id            = aws_vpc.shared[0].id
  cidr_block        = var.vpb_ingress_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.vpb_ingress_subnet_name })
}

resource "aws_subnet" "vpb_egress" {
  count = var.shared_vpc && var.deploy_vpb ? 1 : 0

  vpc_id            = aws_vpc.shared[0].id
  cidr_block        = var.vpb_egress_subnet_cidr
  availability_zone = var.availability_zone != "" ? var.availability_zone : null
  tags              = merge(var.tags, { Name = local.vpb_egress_subnet_name })
}

# ---------------------------------------------------------------------
# vController (formerly CLMS): N instances - cloudlens-vcontroller-1, -2, -3
# ---------------------------------------------------------------------
module "clms" {
  count  = var.vcontroller_count
  source = "../clms"

  region  = var.region
  profile = var.profile

  instance_name  = "${var.clms_instance_name}-${count.index + 1}"
  admin_username = var.admin_username
  key_name       = var.key_name
  public_key     = var.public_key
  ami_id         = var.clms_ami_id
  instance_type  = var.clms_instance_type

  # In shared mode every instance shares the one clms subnet; AWS gives each
  # its own private IP and the child module attaches a dedicated Elastic IP.
  existing_vpc_id    = var.shared_vpc ? aws_vpc.shared[0].id : ""
  existing_subnet_id = var.shared_vpc ? aws_subnet.clms[0].id : ""

  vpc_cidr    = var.vpc_cidr
  subnet_cidr = var.clms_subnet_cidr

  ssh_ingress_cidrs   = var.ssh_ingress_cidrs
  https_ingress_cidrs = var.https_ingress_cidrs

  tags = var.tags
}

# ---------------------------------------------------------------------
# KVO: N instances - cloudlens-kvo-1, -2. Skipped when deploy_kvo = false.
# ---------------------------------------------------------------------
module "kvo" {
  count  = var.deploy_kvo ? var.kvo_count : 0
  source = "../kvo"

  region  = var.region
  profile = var.profile

  instance_name  = "${var.kvo_instance_name}-${count.index + 1}"
  admin_username = var.admin_username
  key_name       = var.key_name
  public_key     = var.public_key
  ami_id         = var.kvo_ami_id
  instance_type  = var.kvo_instance_type

  existing_vpc_id    = var.shared_vpc ? aws_vpc.shared[0].id : ""
  existing_subnet_id = var.shared_vpc ? aws_subnet.kvo[0].id : ""

  vpc_cidr    = var.vpc_cidr
  subnet_cidr = var.kvo_subnet_cidr

  ssh_ingress_cidrs   = var.ssh_ingress_cidrs
  https_ingress_cidrs = var.https_ingress_cidrs

  tags = var.tags
}

# ---------------------------------------------------------------------
# vPB: N instances - cloudlens-vpb-1, -2, ...
# Each vPB has its own mgmt + ingress + egress ENIs (multi-NIC honored).
# ---------------------------------------------------------------------
module "vpb" {
  count  = var.deploy_vpb ? var.vpb_count : 0
  source = "../vpb"

  region  = var.region
  profile = var.profile

  instance_name  = "${var.vpb_instance_name}-${count.index + 1}"
  admin_username = var.admin_username
  key_name       = var.key_name
  public_key     = var.public_key
  ami_id         = var.vpb_ami_id
  instance_type  = var.vpb_instance_type

  existing_vpc_id            = var.shared_vpc ? aws_vpc.shared[0].id : ""
  existing_mgmt_subnet_id    = var.shared_vpc ? aws_subnet.vpb_mgmt[0].id : ""
  existing_ingress_subnet_id = var.shared_vpc ? aws_subnet.vpb_ingress[0].id : ""
  existing_egress_subnet_id  = var.shared_vpc ? aws_subnet.vpb_egress[0].id : ""

  vpc_cidr            = var.vpc_cidr
  mgmt_subnet_cidr    = var.vpb_mgmt_subnet_cidr
  ingress_subnet_cidr = var.vpb_ingress_subnet_cidr
  egress_subnet_cidr  = var.vpb_egress_subnet_cidr

  ingress_nic_count     = var.vpb_ingress_nic_count
  egress_nic_count      = var.vpb_egress_nic_count
  enable_auto_bootstrap = var.vpb_enable_auto_bootstrap

  ssh_ingress_cidrs   = var.ssh_ingress_cidrs
  https_ingress_cidrs = var.https_ingress_cidrs

  tags = var.tags
}
