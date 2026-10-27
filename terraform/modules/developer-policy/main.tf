# Developer IAM policy (PLAN.md section 6.2). Standalone so the platforms team can
# call it from their own Terraform or copy the policy_json output.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

data "aws_partition" "current" {}

locals {
  p   = data.aws_partition.current.partition
  r   = var.region
  a   = var.account_id
  ec2 = "arn:${local.p}:ec2:${local.r}:${local.a}"
  ssm = "arn:${local.p}:ssm:${local.r}"

  tagged = {
    test     = "StringEquals"
    variable = "aws:ResourceTag/agent-platform"
    values   = ["true"]
  }
}

data "aws_iam_policy_document" "this" {
  statement {
    sid       = "Read"
    actions   = ["ec2:Describe*", "sts:GetCallerIdentity", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadAmiParameter"
    actions   = ["ssm:GetParameter"]
    resources = ["${local.ssm}::parameter${var.ami_ssm_parameter}"]
  }

  statement {
    sid     = "CreateTagged"
    actions = ["ec2:RunInstances", "ec2:CreateVolume"]
    resources = [
      "${local.ec2}:instance/*",
      "${local.ec2}:volume/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/agent-platform"
      values   = ["true"]
    }
  }

  # RunInstances also authorizes the resources it uses or creates as a side effect.
  # The network interface is created by RunInstances itself and dies with the
  # instance; Terraform's aws_instance cannot tag it at launch.
  statement {
    sid     = "RunInstancesSupportingResources"
    actions = ["ec2:RunInstances"]
    resources = concat(
      [
        "${local.ec2}:network-interface/*",
        "${local.ec2}:security-group/${var.security_group_id}",
        "arn:${local.p}:ec2:${local.r}::image/*",
      ],
      [for s in var.subnet_ids : "${local.ec2}:subnet/${s}"],
    )
  }

  statement {
    sid     = "TagOnCreate"
    actions = ["ec2:CreateTags"]
    resources = [
      "${local.ec2}:instance/*",
      "${local.ec2}:volume/*",
      "${local.ec2}:network-interface/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateVolume"]
    }
  }

  statement {
    sid = "ManageTagged"
    actions = [
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ec2:ModifyInstanceAttribute",
      "ec2:AttachVolume",
      "ec2:DetachVolume",
      "ec2:DeleteVolume",
      "ec2:GetConsoleOutput",
    ]
    resources = [
      "${local.ec2}:instance/*",
      "${local.ec2}:volume/*",
    ]
    condition {
      test     = local.tagged.test
      variable = local.tagged.variable
      values   = local.tagged.values
    }
  }

  statement {
    sid       = "PassExecutionRole"
    actions   = ["iam:PassRole"]
    resources = [var.execution_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  # SSH is forced: SSM only provides the tunnel. SessionDocumentAccessCheck makes a
  # request without --document-name get checked against the default shell
  # document, which is not allowed here.
  # StartSession reads instance tags through ssm:resourceTag, not aws:ResourceTag.
  statement {
    sid       = "StartSshSessionOnTagged"
    actions   = ["ssm:StartSession"]
    resources = ["${local.ec2}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/agent-platform"
      values   = ["true"]
    }
    condition {
      test     = "BoolIfExists"
      variable = "ssm:SessionDocumentAccessCheck"
      values   = ["true"]
    }
  }

  statement {
    sid       = "StartSshSessionDocument"
    actions   = ["ssm:StartSession"]
    resources = ["${local.ssm}::document/AWS-StartSSHSession"]
    condition {
      test     = "BoolIfExists"
      variable = "ssm:SessionDocumentAccessCheck"
      values   = ["true"]
    }
  }

  statement {
    sid       = "OwnSessions"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession"]
    resources = ["arn:${local.p}:ssm:*:${local.a}:session/$${aws:userid}-*"]
  }

  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:${local.p}:s3:::${var.state_bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["env/*"]
    }
  }

  statement {
    sid       = "StateObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:${local.p}:s3:::${var.state_bucket}/env/*"]
  }

  statement {
    sid       = "Doctor"
    actions   = ["iam:SimulatePrincipalPolicy"]
    resources = [var.developer_role_arn]
  }
}
