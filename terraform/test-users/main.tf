# Personal test account only: IAM users that stand in for developers. Apply this
# before terraform/platform and list user_arns in its trusted_principal_arns.
# Access keys land in this root's local state (gitignored); keep it that way.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type = string
}

variable "user_names" {
  type    = list(string)
  default = ["zach", "alice"]
}

variable "developer_role_name" {
  type    = string
  default = "agent-platform-developer"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_iam_user" "dev" {
  for_each = toset(var.user_names)
  name     = each.key
  tags     = { "agent-platform" = "true" }
}

locals {
  role_arn_prefix = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role"
}

resource "aws_iam_user_policy" "assume_developer" {
  for_each = aws_iam_user.dev
  name     = "assume-agent-platform-developer"
  user     = each.value.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Resource = [
        "${local.role_arn_prefix}/${var.developer_role_name}",
        aws_iam_role.existing_developer.arn,
      ]
    }]
  })
}

# Stands in for an existing (e.g. SSO) developer role, to test the platform's
# developer_role_mode = "attach". It has no permissions of its own.
resource "aws_iam_role" "existing_developer" {
  name = "agent-platform-test-existing-developer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = [for u in aws_iam_user.dev : u.arn] }
    }]
  })
}

resource "aws_iam_access_key" "dev" {
  for_each = aws_iam_user.dev
  user     = each.value.name
}

output "existing_developer_role_name" {
  value = aws_iam_role.existing_developer.name
}

output "user_arns" {
  value = [for u in aws_iam_user.dev : u.arn]
}

output "access_keys" {
  sensitive = true
  value     = { for k, v in aws_iam_access_key.dev : k => { id = v.id, secret = v.secret } }
}
