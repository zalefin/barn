# Connecting barn to an AWS account

barn does not set up AWS accounts. It assumes the network, the IAM
roles and a Terraform state bucket already exist, and per environment it
creates only:

- one encrypted gp3 **EBS volume** (the box's home directory), and
- one **EC2 instance** with that volume attached,

plus two Terraform state objects in your bucket. This page lists everything
that must exist first, and the account file that tells the CLI where it is.

A minimal Terraform implementation of all of it (VPC, security group, roles,
bucket, developer policy) is kept as a demo outside this repo, in
`~/source/barn-platform-minimal`.

## Checklist

| # | What | Requirements |
|---|---|---|
| 1 | **Region** | Any region with the instance types you want. |
| 2 | **Subnets** (one or more) | Outbound HTTPS to the internet and to the SSM endpoints (below), through an internet gateway (the subnet must then auto-assign public IPs, since the CLI does not request one), a NAT gateway, or VPC endpoints plus a proxy. A volume's AZ comes from the subnet chosen at its first `create`, so the box always relaunches in that subnet. |
| 3 | **Security group** in that VPC | **No inbound rules are needed**: SSH arrives through the SSM tunnel. Egress TCP 443 and 80 (apt uses HTTP), or all. |
| 4 | **Instance profile** (the box's identity) | Its role trusts `ec2.amazonaws.com` and has the AWS-managed `AmazonSSMManagedInstanceCore` policy. Agents on the box use this role, so add whatever AWS access they need here. Developers must not be able to modify it. |
| 5 | **S3 state bucket** | Any bucket the developers can write `env/*` in; versioning recommended. State lives at `env/<user>/<env>/{volume,instance}.tfstate`, and locking uses S3 lockfiles (Terraform >= 1.10, no DynamoDB). |
| 6 | **AMI** | An SSM parameter holding an Ubuntu 24.04 amd64 AMI ID with the `amazon-ssm-agent` snap preinstalled. The default is Canonical's public parameter `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id`. |
| 7 | **Developer credentials** | An AWS CLI profile per developer with the permissions below. If the permissions live on a role, configure the role in `~/.aws/config` (SSO, or `role_arn` + `source_profile`). The CLI, Terraform and the SSH `ProxyCommand` all use that profile as-is. |
| 8 | **KMS key** (optional) | Leave `kms_key_id` empty to use the AWS-managed EBS key. A customer-managed key additionally needs `kms:CreateGrant`, `kms:Decrypt` and `kms:GenerateDataKey*` for developers (untested). |

### Egress the box needs

- SSM: `ssm.<region>.amazonaws.com`, `ssmmessages.<region>.amazonaws.com`,
  `ec2messages.<region>.amazonaws.com`
- First boot: Ubuntu apt mirrors, the Snap Store (SSM agent refresh),
  `download.docker.com`, `cli.kiro.dev`, `claude.ai`, `herdr.dev`, `astral.sh`
- At run time: whatever your agents call (Anthropic, Kiro, git hosts, ...)

`barn doctor <env>` checks the SSM endpoints and the install hosts from the box.

## Developer permissions

[`developer-policy.json`](developer-policy.json) is the exact policy the CLI is
tested with. Replace the placeholders `REGION`, `ACCOUNT_ID`, `STATE_BUCKET`,
`EXECUTION_ROLE_ARN` (the instance profile's role), `SECURITY_GROUP_ID`,
`SUBNET_ID_*` (one entry per subnet) and `DEVELOPER_ROLE_ARN` (the role that
gets this policy). Change `arn:aws` if you are in another partition.

Everything a developer can change is limited to resources tagged
`barn = true`. The CLI tags every volume and instance with that tag,
`barn:owner = <user>`, `barn:env = <env>` and the account
file's `extra_tags`, all at creation.

| Actions | Scope | Why |
|---|---|---|
| `ec2:Describe*`, `ssm:DescribeInstanceInformation`, `sts:GetCallerIdentity` | `*` | Terraform reads, `list`, and waiting for the box to come online in SSM. |
| `ssm:GetParameter` | the AMI parameter only | Resolve the AMI. |
| `ec2:RunInstances`, `ec2:CreateVolume` | instance and volume, only with `aws:RequestTag/barn = true` | Create the box and its home volume, tagged at creation. |
| `ec2:RunInstances` | the subnets and security group, any image, network interfaces | Resources a launch uses. The ENI is created by the launch and deleted with the instance. |
| `ec2:CreateTags` | only when `ec2:CreateAction` is `RunInstances` or `CreateVolume` | Tagging at creation. Developers cannot retag existing resources. |
| `ec2:Start/Stop/TerminateInstances`, `ModifyInstanceAttribute`, `Attach/Detach/DeleteVolume`, `GetConsoleOutput` | tagged resources | Lifecycle, resize, home volume attach, and boot-log diagnostics. |
| `iam:PassRole` | the instance profile's role only, `iam:PassedToService = ec2.amazonaws.com` | Launch the box with its instance profile. |
| `ssm:StartSession` | tagged instances (`ssm:resourceTag/barn`) and only the `AWS-StartSSHSession` document, with `ssm:SessionDocumentAccessCheck` | The SSH tunnel. Shell sessions, port forwarding and `ssm:SendCommand` stay denied, so only the owner's SSH key gets into a box. |
| `ssm:TerminateSession`, `ssm:ResumeSession` | the caller's own sessions | End or resume their own tunnels. |
| `s3:ListBucket` (prefix `env/*`), `s3:GetObject/PutObject/DeleteObject` (`env/*`) | state bucket | Terraform state and lockfiles (releasing a lock deletes it). |
| `iam:SimulatePrincipalPolicy` | the developer role itself | Optional: lets `doctor` check that this policy is still attached. |

A broader role (for example an account admin) works too. `doctor` then warns
that the role can open shell sessions on boxes.

If a central automation owns the developer role (for example SSO permission
sets), add the policy there: a policy attached outside that automation may be
silently removed. `doctor` notices if it is.

## The account file

Each developer saves one file per account at
`~/.config/barn/accounts/<name>.toml` and selects it with
`account = "<name>"` in `config.toml`. The file contains no secrets, so one
copy can be shared with everyone using the account.

```toml
region = "us-east-1"
state_bucket = "my-barn-state"
execution_instance_profile = "barn-execution"   # instance profile name (4)
security_group_id = "sg-0123456789abcdef0"      # (3)
subnet_ids = ["subnet-0123456789abcdef0"]       # (2); new volumes use the first
# Optional:
ami_ssm_parameter = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
# The role doctor simulates the policy on:
developer_role_arn = "arn:aws:iam::123456789012:role/barn-developer"
kms_key_id = ""

[extra_tags]                                    # added to every volume and instance
# "cost-center" = "1234"
```

Unknown or missing keys are an error. [`examples/account.toml`](../examples/account.toml)
is a copy to start from.

## Verify

```bash
barn create dev   # volume + instance; waits for SSM and first boot
barn doctor dev   # credentials, permissions, SSM, bootstrap, egress, tools
```

If `create` times out waiting for SSM, check the subnet's egress (2), the
security group's egress (3) and the instance profile (4). If first boot fails
later, `create` prints the tail of the console output.
