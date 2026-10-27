# Second stack in the personal test account exercising both attach modes:
# the VPC from the create-mode stack, and a pre-existing developer role.
# Apply in its own workspace: terraform workspace select -or-create attach
region       = "us-east-1"
account_name = "personal-attach"
name_prefix  = "ap-attach"

vpc_mode   = "attach"
vpc_id     = "vpc-0aaaaaaaaaaaaaa01"
subnet_ids = ["subnet-0aaaaaaaaaaaaaa01"]

developer_role_mode = "attach"
existing_role_name  = "agent-platform-test-existing-developer"

extra_tags = { "cost-center" = "test" }
