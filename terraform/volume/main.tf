# Per-environment home volume. Survives instance destroy/recreate.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
}

variable "region" {
  type = string
}

variable "user" {
  type = string
}

variable "env" {
  type = string
}

variable "subnet_id" {
  description = "Fixes the volume's AZ; the instance always launches in this subnet."
  type        = string
}

variable "size_gb" {
  type    = number
  default = 100
}

variable "kms_key_id" {
  type    = string
  default = ""
}

variable "extra_tags" {
  type    = map(string)
  default = {}
}

data "aws_subnet" "this" {
  id = var.subnet_id
}

resource "aws_ebs_volume" "home" {
  availability_zone = data.aws_subnet.this.availability_zone
  size              = var.size_gb
  type              = "gp3"
  encrypted         = true
  kms_key_id        = var.kms_key_id == "" ? null : var.kms_key_id

  # Must match the instance root's volume_tags exactly, or the two roots fight.
  tags = merge(var.extra_tags, {
    "Name"                 = "agent-platform-${var.user}-${var.env}"
    "agent-platform"       = "true"
    "agent-platform:owner" = var.user
    "agent-platform:env"   = var.env
  })
}

output "volume_id" {
  value = aws_ebs_volume.home.id
}

output "subnet_id" {
  value = var.subnet_id
}

output "availability_zone" {
  value = aws_ebs_volume.home.availability_zone
}
