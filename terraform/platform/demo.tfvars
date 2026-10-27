# Demo account (123456789012), applied by an admin (profile zach).
# Apply with: AWS_PROFILE=zach terraform apply -var-file=demo.tfvars
region       = "us-east-1"
account_name = "demo"

vpc_mode = "create"
azs      = ["us-east-1a", "us-east-1b"]

developer_role_mode = "create"
trusted_principal_arns = [
  "arn:aws:iam::123456789012:user/zach",
]
