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
  default_tags {
    tags = merge({ "agent-platform" = "true" }, var.extra_tags)
  }
}
