variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "state_bucket" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "security_group_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "ami_ssm_parameter" {
  type = string
}

variable "developer_role_arn" {
  description = "ARN of the role developers use; it may simulate its own policy (doctor)."
  type        = string
}
