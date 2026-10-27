data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  vpc_id     = var.vpc_mode == "create" ? aws_vpc.this[0].id : var.vpc_id
  subnet_ids = var.vpc_mode == "create" ? aws_subnet.public[*].id : var.subnet_ids

  developer_role_arn = (var.developer_role_mode == "create"
    ? aws_iam_role.developer[0].arn
  : data.aws_iam_role.existing[0].arn)
}

# Looked up rather than built so roles with a path (SSO roles) resolve correctly.
data "aws_iam_role" "existing" {
  count = var.developer_role_mode == "attach" ? 1 : 0
  name  = var.existing_role_name
}

# --- Execution role (the box's identity) ---

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-execution"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "execution_ssm" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "execution" {
  name = "${var.name_prefix}-execution"
  role = aws_iam_role.execution.name
}

# --- Terraform state bucket ---

resource "aws_s3_bucket" "state" {
  bucket = "${var.name_prefix}-state-${local.account_id}-${var.region}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --- Network ---

resource "aws_vpc" "this" {
  count                = var.vpc_mode == "create" ? 1 : 0
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name_prefix }
}

resource "aws_internet_gateway" "this" {
  count  = var.vpc_mode == "create" ? 1 : 0
  vpc_id = aws_vpc.this[0].id
  tags   = { Name = var.name_prefix }
}

resource "aws_subnet" "public" {
  count                   = var.vpc_mode == "create" ? length(var.azs) : 0
  vpc_id                  = aws_vpc.this[0].id
  availability_zone       = var.azs[count.index]
  cidr_block              = cidrsubnet("10.42.0.0/16", 8, count.index)
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name_prefix}-${var.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  count  = var.vpc_mode == "create" ? 1 : 0
  vpc_id = aws_vpc.this[0].id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }
  tags = { Name = var.name_prefix }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_security_group" "box" {
  name        = "${var.name_prefix}-box"
  description = "agent-platform dev boxes: no inbound; SSH arrives through SSM"
  vpc_id      = local.vpc_id
  tags        = { Name = "${var.name_prefix}-box" }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.box.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- Developer permissions ---

module "developer_policy" {
  source             = "../modules/developer-policy"
  region             = var.region
  account_id         = local.account_id
  state_bucket       = aws_s3_bucket.state.bucket
  execution_role_arn = aws_iam_role.execution.arn
  security_group_id  = aws_security_group.box.id
  subnet_ids         = local.subnet_ids
  ami_ssm_parameter  = var.ami_ssm_parameter
  developer_role_arn = local.developer_role_arn
}

data "aws_iam_policy_document" "developer_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = var.trusted_principal_arns
    }
  }
}

resource "aws_iam_role" "developer" {
  count              = var.developer_role_mode == "create" ? 1 : 0
  name               = "${var.name_prefix}-developer"
  assume_role_policy = data.aws_iam_policy_document.developer_trust.json
}

resource "aws_iam_policy" "developer" {
  name   = "${var.name_prefix}-developer"
  policy = module.developer_policy.policy_json
}

resource "aws_iam_role_policy_attachment" "developer" {
  role       = var.developer_role_mode == "create" ? aws_iam_role.developer[0].name : var.existing_role_name
  policy_arn = aws_iam_policy.developer.arn
}
