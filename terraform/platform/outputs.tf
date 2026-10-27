output "account_file" {
  description = "Contents of accounts/<account_name>.toml; paste it into the repo."
  value = templatefile("${path.module}/account.toml.tftpl", {
    name                       = var.account_name
    region                     = var.region
    state_bucket               = aws_s3_bucket.state.bucket
    developer_role_arn         = var.developer_role_mode == "create" ? aws_iam_role.developer[0].arn : ""
    developer_policy_role_arn  = local.developer_role_arn
    execution_instance_profile = aws_iam_instance_profile.execution.name
    security_group_id          = aws_security_group.box.id
    subnet_ids                 = local.subnet_ids
    ami_ssm_parameter          = var.ami_ssm_parameter
    kms_key_id                 = var.kms_key_id
    extra_tags                 = var.extra_tags
  })
}

output "developer_policy_json" {
  value = module.developer_policy.policy_json
}
