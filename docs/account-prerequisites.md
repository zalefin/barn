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
| 2 | **Subnets** (one or more) | Outbound HTTP/HTTPS to the internet and to the SSM endpoints (below), through an internet gateway (the subnet must then auto-assign public IPs, since the CLI does not request one) or a NAT gateway. The bootstrap configures no proxy, so only a transparent proxy works. SSM over VPC endpoints needs private DNS on the endpoints, DNS support and hostnames on the VPC, and an endpoint security group that allows 443 from the box's group. A volume's AZ comes from the first subnet in the account file at its first `create`, and its box always relaunches in that subnet: keep the subnet while volumes live in it, and make sure your instance types are offered in its AZ. |
| 3 | **Security group** in that VPC | **No inbound rules are needed**: SSH arrives through the SSM tunnel. Egress TCP 443 and 80 (apt uses HTTP), or all. |
| 4 | **Instance profile** (the box's identity) | Its role trusts `ec2.amazonaws.com` and has the AWS-managed `AmazonSSMManagedInstanceCore` policy. Agents on the box use this role, so add whatever AWS access they need here, but note that **every developer effectively holds it**: anyone who can launch a box can run any code with its credentials. Developers must not be able to modify it. The account file names the *instance profile*; the policy needs its *role* ARN (with any path). |
| 5 | **S3 state bucket** | In `region` and the same account; versioning recommended. State lives at `env/<user>/<env>/{volume,instance}.tfstate`, and locking uses S3 lockfiles (Terraform >= 1.10, no DynamoDB). Default SSE-S3 needs nothing more; SSE-KMS with a customer-managed key also needs `kms:GenerateDataKey` and `kms:Decrypt` on it. |
| 6 | **AMI** | An SSM parameter holding an Ubuntu 24.04 amd64 AMI ID with the `amazon-ssm-agent` snap preinstalled. The default is Canonical's public parameter `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id`. Your own parameter's ARN includes your account ID; change the policy's `ReadAmiParameter` resource to match. |
| 7 | **Developer credentials** | An AWS CLI profile per developer with the permissions below. If the permissions live on a role, configure the role in `~/.aws/config` (SSO, or `role_arn` + `source_profile`). The CLI, Terraform and the SSH `ProxyCommand` all use that profile as-is. Permission boundaries and SCPs must allow the same actions. Developer machines also need `registry.terraform.io` and `releases.hashicorp.com` for the first `terraform init`. |
| 8 | **KMS key** (optional) | With `kms_key_id` empty, EBS uses the account's *default* EBS key (`aws ec2 get-ebs-default-kms-key-id`). If that is the AWS-managed `alias/aws/ebs`, nothing more is needed. If it is, or `kms_key_id` names, a customer-managed key (give its ARN), developers need `kms:CreateGrant` (with `kms:GrantIsForAWSResource`), `kms:GenerateDataKeyWithoutPlaintext`, `kms:Decrypt`, `kms:ReEncrypt*` and `kms:DescribeKey` on it, with `kms:ViaService = ec2.<region>.amazonaws.com`. That case is untested. |

### Egress the box needs

- SSM: `ssm.<region>.amazonaws.com`, `ssmmessages.<region>.amazonaws.com`,
  `ec2messages.<region>.amazonaws.com`
- First boot: Ubuntu apt mirrors (`<region>.ec2.archive.ubuntu.com`,
  `security.ubuntu.com`, over HTTP), the Snap Store (SSM agent refresh),
  `download.docker.com`, `github.com` (Neovim), and the installers at
  `cli.kiro.dev`, `claude.ai`, `herdr.dev` and `astral.sh`, which download
  from further hosts (GitHub release storage among them). Allowlisting hosts is not supported yet:
  allow HTTP/HTTPS egress.
- At run time: whatever your agents call (Anthropic, Kiro, git hosts, ...)

`barn doctor <env>` checks the SSM endpoints and the install hosts from the box.

## Developer permissions

[`developer-policy.json`](developer-policy.json) is the exact policy the CLI is
tested with, attached to an existing role that developers assume. How it was
derived and verified is under [How this policy was verified](#how-this-policy-was-verified).

Replace the placeholders:

- `REGION`, `ACCOUNT_ID`, `STATE_BUCKET`, `SECURITY_GROUP_ID`
- `SUBNET_ID_*`: one entry per subnet, in both statements that list them
- `EXECUTION_ROLE_ARN`: the instance profile's *role*, with its path
- `DEVELOPER_ROLE_ARN`: the IAM role that gets this policy, with its path. For
  an SSO permission set that is
  `arn:aws:iam::<account>:role/aws-reserved/sso.amazonaws.com/[<region>/]AWSReservedSSO_<set>_<suffix>`,
  not the `assumed-role` ARN `doctor` prints. Because the policy holds
  per-account values, attach it to a permission set as a *customer-managed
  policy reference* (same name, created in each account), and use a wildcard
  such as `.../AWSReservedSSO_<set>_*` here.

Change `arn:aws` in another partition.

Everything a developer can change is limited to resources tagged
`barn = true`. The CLI tags every volume and instance with that tag,
`barn:owner = <user>`, `barn:env = <env>` and the account file's
`extra_tags`, all at creation.

| Actions | Scope | Why |
|---|---|---|
| `ec2:DescribeImages`, `DescribeInstanceAttribute`, `DescribeInstanceCreditSpecifications`, `DescribeInstanceTypes`, `DescribeInstances`, `DescribeSubnets`, `DescribeTags`, `DescribeVolumes`, `DescribeVolumesModifications`, `DescribeVpcs`; `ssm:DescribeInstanceInformation` | `*` (no resource-level permissions) | Exactly the reads Terraform, `list` and the SSM wait make. |
| `ssm:GetParameter` | the AMI parameter only | Resolve the AMI. |
| `ec2:RunInstances`, `ec2:CreateVolume` | instance and volume, only with `aws:RequestTag/barn = true` | Create the box and its home volume, tagged at creation. |
| `ec2:RunInstances` | the subnets and security group, any image; network interfaces only in those subnets | Resources a launch uses. The ENI is created by the launch and deleted with the instance. |
| `ec2:CreateTags` | only when `ec2:CreateAction` is `RunInstances` or `CreateVolume` | Tagging at creation; both calls fail without it. |
| `ec2:CreateTags`, `ec2:DeleteTags` | tagged resources, never the keys `barn`, `barn:*` or `Name` | Applying a changed `extra_tags`. barn's own tags cannot be changed. |
| `ec2:Start/Stop/TerminateInstances`, `Attach/Detach/Delete/ModifyVolume`, `GetConsoleOutput` | tagged resources | Lifecycle, home volume attach and growth, boot-log diagnostics. |
| `ec2:ModifyInstanceAttribute` | tagged instances, only the attributes `InstanceType` and `InstanceInitiatedShutdownBehavior` | Resize, and stop (not terminate) on shutdown. Changing user data, which would run code as root on the next boot, stays denied. |
| `iam:PassRole` | the instance profile's role only, `iam:PassedToService = ec2.amazonaws.com` | Launch the box with its instance profile. |
| `ssm:StartSession` | tagged instances (`ssm:resourceTag/barn`) and only the `AWS-StartSSHSession` document, with `ssm:SessionDocumentAccessCheck` | The SSH tunnel. Shell sessions, `AWS-StartPortForwardingSession` and `ssm:SendCommand` stay denied. |
| `s3:ListBucket` (prefix `env/*`) | state bucket | `terraform init` lists `env/workspaces/`. |
| `s3:GetObject/PutObject/DeleteObject` (`env/*`) | state bucket | State and lockfiles; releasing a lock deletes it. |
| `iam:SimulatePrincipalPolicy` | the developer role itself | Optional: lets `doctor` check that this policy is still attached. |

Not needed: `sts:GetCallerIdentity` (always allowed), `ssm:TerminateSession` and
`ssm:ResumeSession`, and any KMS permission with the AWS-managed EBS key (EC2
calls KMS on the developer's behalf and the key policy allows it; see row 8).

### What this policy does not protect

Developers in one account must trust each other. The policy scopes everything
to the `barn` tag, not to `barn:owner`, so any developer can:

- stop, delete or resize anyone's box and volume;
- stop someone's box, detach their home volume and attach it to their own box,
  reading their files and tokens (`~/.barn/env`) and SSH host keys;
- open a tunnel to any port on anyone's box: `AWS-StartSSHSession` takes a free
  `portNumber`, so "SSH only" means only that the tunnel is a port forward.

Two developers who set the same `user` in `config.toml` also share state and
boxes. Binding `barn:owner` to the caller's identity (an IdP session tag or
`aws:SourceIdentity` compared in the policy) would close the first two, and a
custom session document with a fixed port the third. Neither is implemented.

The policy also sets no cost limits. Conditions on `ec2:InstanceType`,
`ec2:VolumeSize`, `ec2:VolumeType` and `ec2:Tenancy` can be added if you need them.

A broader role (for example an account admin) works too. `doctor` then warns
that it can open shell sessions or change user data on any box.

If a central automation owns the developer role (for example SSO permission
sets), add the policy there: a policy attached outside that automation may be
silently removed. `doctor` notices if it is.

### How this policy was verified

In a sandbox account (2026-10-29), the policy was attached to a stand-in
"existing" role whose only trusted principal was an IAM user with no other
permissions, mirroring an SSO role. As that user, through the role:

1. The whole lifecycle passed: `list`, `create`, `sync`, `ssh`, `hold`,
   `doctor`, `stop`, `start`, resize, `destroy`, recreate (files and SSH host
   key kept), and `destroy --volume`.
2. CloudTrail event history listed every API call the role made. The reads
   above are exactly the ones used, and nothing was denied.
3. Permissions that CloudTrail cannot show were proven by denying each one and
   watching the lifecycle fail: `ec2:CreateTags` (both `CreateVolume` and
   `RunInstances`), `iam:PassRole`, `s3:ListBucket` and `s3:DeleteObject`.
4. Denied, as intended: shell and port-forwarding-document sessions,
   `ssm:SendCommand`, changing a box's user data or termination protection,
   setting or removing `barn`, `barn:*` or `Name` tags, tagging untagged
   resources, untagged volumes, state writes outside `env/`, reading the
   instance role, and attaching policies to the developer role.
5. Changing `extra_tags` (added and removed) and growing `volume_gb` on an
   existing environment passed.
6. An independent review of the policy against the raw CloudTrail events and
   the code found the gaps fixed above and the limits listed under
   [What this policy does not protect](#what-this-policy-does-not-protect).
7. With the final policy, the whole lifecycle passed again with no denials.
   (`doctor` reports Claude and Kiro as not signed in because the test boxes
   had no tokens.)

Only one developer and one subnet were used.

The read list depends on the AWS provider version pinned in the roots' lock
files. If a provider upgrade adds a read, `create` fails with
`UnauthorizedOperation` naming the action; add it.

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

Unknown or missing keys are an error. `barn init` writes a template with
`CHANGE-ME` placeholders, or copies one in with `--account-file`.

## Verify

```bash
barn create dev   # volume + instance; waits for SSM and first boot
barn doctor dev   # credentials, permissions, SSM, bootstrap, egress, tools
```

If `create` times out waiting for SSM, check the subnet's egress (2), the
security group's egress (3) and the instance profile (4). If first boot fails
later, `create` prints the tail of the console output.

`volume_gb` can grow (never shrink) an existing volume, at most once every 6
hours (an EBS limit); `create` then grows the filesystem.

If a run is interrupted while Terraform holds its state lock, later runs report
the state as locked. Delete the lockfile the error names (a developer may):
`aws s3 rm s3://<state_bucket>/env/<user>/<env>/<root>.tfstate.tflock`.
