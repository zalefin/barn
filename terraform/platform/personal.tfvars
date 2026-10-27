# Personal test account (111122223333). Apply with: terraform apply -var-file=personal.tfvars
region       = "us-east-1"
account_name = "personal"

vpc_mode = "create"
azs      = ["us-east-1a", "us-east-1b"]

developer_role_mode = "create"
trusted_principal_arns = [
  "arn:aws:iam::111122223333:user/sandbox-admin",
  "arn:aws:iam::111122223333:user/zach",
  "arn:aws:iam::111122223333:user/alice",
]
