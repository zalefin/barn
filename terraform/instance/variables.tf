variable "region" {
  type = string
}

variable "user" {
  description = "Developer name: owner tag and resource names (the box's login user is always ubuntu)."
  type        = string
}

variable "env" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "passwordless_sudo" {
  description = "Changing this replaces the instance (it is baked into user data)."
  type        = bool
  default     = true
}

variable "idle_hours" {
  type    = number
  default = 4
}

variable "instance_type" {
  type    = string
  default = "m7i.xlarge"
}

variable "root_volume_gb" {
  type    = number
  default = 30
}

# From the volume root's outputs.
variable "volume_id" {
  type = string
}

variable "subnet_id" {
  type = string
}

# From the account file.
variable "security_group_id" {
  type = string
}

variable "execution_instance_profile" {
  type = string
}

variable "ami_ssm_parameter" {
  type = string
}

variable "kms_key_id" {
  type    = string
  default = ""
}

variable "extra_tags" {
  type    = map(string)
  default = {}
}
