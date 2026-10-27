variable "region" {
  type = string
}

variable "name_prefix" {
  type    = string
  default = "agent-platform"
}

variable "account_name" {
  description = "Name of the account file this root's output becomes (accounts/<name>.toml)."
  type        = string
}

variable "vpc_mode" {
  description = "create: build a minimal VPC. attach: use vpc_id and subnet_ids."
  type        = string
  default     = "create"
  validation {
    condition     = contains(["create", "attach"], var.vpc_mode)
    error_message = "vpc_mode must be create or attach."
  }
}

variable "vpc_id" {
  type    = string
  default = ""
}

variable "subnet_ids" {
  type    = list(string)
  default = []
}

variable "azs" {
  description = "AZs for the created VPC's public subnets (create mode)."
  type        = list(string)
  default     = []
}

variable "developer_role_mode" {
  description = "create: new developer role. attach: attach the policy to existing_role_name."
  type        = string
  default     = "create"
  validation {
    condition     = contains(["create", "attach"], var.developer_role_mode)
    error_message = "developer_role_mode must be create or attach."
  }
}

variable "trusted_principal_arns" {
  description = "Principals allowed to assume the developer role (create mode)."
  type        = list(string)
  default     = []
}

variable "existing_role_name" {
  type    = string
  default = ""
}

variable "ami_ssm_parameter" {
  type    = string
  default = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

variable "kms_key_id" {
  description = "Optional KMS key for EBS encryption; empty means the AWS-managed key."
  type        = string
  default     = ""
}

variable "extra_tags" {
  type    = map(string)
  default = {}
}
