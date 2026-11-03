variable "region" {
  type = string
}

variable "user" {
  description = "Who created the box: the barn:owner tag (the login user is always ubuntu)."
  type        = string
}

variable "name" {
  description = "The box's full name: SSH alias, hostname and barn:name tag."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,38}[a-z0-9]$", var.name)) && !contains(["workspaces", "plugin-cache"], var.name)
    error_message = "2 to 40 lowercase letters, digits and dashes, starting with a letter; not \"workspaces\" or \"plugin-cache\"."
  }
}

variable "ssh_public_key" {
  description = "The owner's key; first boot writes it into the owner block of authorized_keys."
  type        = string
}

variable "passwordless_sudo" {
  description = "Changing this replaces the instance (it is baked into user data)."
  type        = bool
  default     = true
}

variable "startup_script" {
  description = "What first boot runs after barn's own setup, as root; empty runs barn's default (bootstrap/startup-default.sh). Changing it replaces the instance."
  type        = string
  default     = ""
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
