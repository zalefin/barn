# Per-box instance. Disposable: destroying it leaves the home volume.

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

locals {
  tags = merge(var.extra_tags, {
    "Name"       = "barn-${var.name}"
    "barn"       = "true"
    "barn:name"  = var.name
    "barn:owner" = var.user
  })

  user_data = templatefile("${path.module}/../../bootstrap/user-data.sh.tftpl", {
    hostname          = var.name
    ssh_public_key    = var.ssh_public_key
    passwordless_sudo = var.passwordless_sudo
    idle_hours        = var.idle_hours
    volume_id         = var.volume_id
    idle_check_sh     = file("${path.module}/../../bootstrap/idle-check.sh")
    idle_decide_py    = file("${path.module}/../../bootstrap/idle_decide.py")
    idle_service      = file("${path.module}/../../bootstrap/idle-check.service")
    idle_timer        = file("${path.module}/../../bootstrap/idle-check.timer")
    authorized_keys   = file("${path.module}/../../bootstrap/authorized_keys.py")
    startup_default   = file("${path.module}/../../bootstrap/startup-default.sh")
    startup_script    = var.startup_script
  })
}

data "aws_ssm_parameter" "ami" {
  name = var.ami_ssm_parameter
}

resource "aws_instance" "box" {
  ami                                  = data.aws_ssm_parameter.ami.insecure_value
  instance_type                        = var.instance_type
  subnet_id                            = var.subnet_id
  vpc_security_group_ids               = [var.security_group_id]
  iam_instance_profile                 = var.execution_instance_profile
  instance_initiated_shutdown_behavior = "stop"
  user_data_base64                     = base64gzip(local.user_data)
  user_data_replace_on_change          = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
    kms_key_id  = var.kms_key_id == "" ? null : var.kms_key_id
  }

  tags = local.tags
  # Tagged at launch (the manager policy only allows tagging during create).
  # Same map as the home volume's tags so the two roots never disagree.
  volume_tags = local.tags

  lifecycle {
    # A new Ubuntu release would otherwise replace the box; recreate to pick it up.
    ignore_changes = [ami]
  }
}

resource "aws_volume_attachment" "home" {
  device_name                    = "/dev/sdf"
  volume_id                      = var.volume_id
  instance_id                    = aws_instance.box.id
  stop_instance_before_detaching = true
}

output "instance_id" {
  value = aws_instance.box.id
}
