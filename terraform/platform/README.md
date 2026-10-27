# agent-platform: platform layer (for account administrators)

This root is applied **once per AWS account** by an administrator using plain
Terraform (>= 1.10). Developers never apply it; their `agent-platform` CLI only
reads its output, committed as `accounts/<name>.toml`.

```bash
cd terraform/platform
terraform init                      # local state, or add your own backend
terraform apply -var-file=<account>.tfvars
terraform output -raw account_file > ../../accounts/<account>.toml
```

See `terraform.tfvars.example` for the inputs.

## What it creates

| Resource | Why |
|---|---|
| IAM role + instance profile `<prefix>-execution`, trusts `ec2.amazonaws.com`, with `AmazonSSMManagedInstanceCore` | The dev box's own identity. The SSM agent needs it to register, and agents on the box use it (developer credentials are never copied to the box). Developers cannot modify it; attach further policies here if agents need AWS access. |
| S3 bucket `<prefix>-state-<account>-<region>` (versioned, public access blocked, SSE-S3) | Terraform state for each developer environment, under `env/<user>/<env>/`. Locking uses S3 native lockfiles (no DynamoDB). |
| Security group `<prefix>-box`: **no inbound rules**, egress all | SSH arrives through the SSM tunnel, so nothing listens on the network. Egress is needed for SSM, apt, Docker and the vendor install scripts. |
| `vpc_mode = "create"` only: VPC `10.42.0.0/16`, one public subnet per `azs`, internet gateway, route table | Self-contained network for accounts without one. Instances get public IPs for egress; inbound is still zero. |
| Customer-managed policy `<prefix>-developer` | The developer permissions below. |
| `developer_role_mode = "create"` only: role `<prefix>-developer` trusting `trusted_principal_arns` | The role developers' CLI assumes. |

## Modes

- **`vpc_mode`**
  - `create`: builds the minimal VPC above.
  - `attach`: uses your `vpc_id` and `subnet_ids` and creates no network.
    The subnets need egress to the internet (install scripts, apt, Docker) and
    to the SSM endpoints (`ssm`, `ssmmessages`, `ec2messages`), through a NAT
    or VPC endpoints.
- **`developer_role_mode`**
  - `create`: creates `<prefix>-developer` and attaches the policy. The CLI
    assumes the role on every command (1 hour sessions).
  - `attach`: attaches the policy to your `existing_role_name`. Developers use
    their normal login (for example, an SSO profile) with no extra role hop.
- **Integrate (recommended for centrally managed roles such as `AWSReservedSSO_*`):**
  a policy attached outside your role automation may be silently removed by it.
  Instead, call `terraform/modules/developer-policy` from your own Terraform,
  or copy the `developer_policy_json` output into your role definitions, and
  set `developer_role_mode = "attach"` with the policy attachment removed (or
  apply only the resources you need). `agent-platform doctor` simulates the
  policy on the developer's role and reports if it disappears.

## Developer permissions (and why)

Everything a developer can change is limited to resources tagged
`agent-platform = true`. Every environment resource also carries
`agent-platform:owner` and `agent-platform:env` tags, plus your `extra_tags`.

| Actions | Scope | Why |
|---|---|---|
| `ec2:Describe*`, `ssm:DescribeInstanceInformation`, `sts:GetCallerIdentity` | `*` | Terraform reads, `list`, and waiting for the box to come online in SSM. |
| `ssm:GetParameter` | the Ubuntu AMI public parameter only | Resolve the current Ubuntu 24.04 AMI. |
| `ec2:RunInstances`, `ec2:CreateVolume` | instance and volume, only with `aws:RequestTag/agent-platform = true` | Create the box and its home volume, tagged at creation. |
| `ec2:RunInstances` | the platform subnets and security group, any image, network interfaces | Resources a launch uses. The ENI is created by the launch and deleted with the instance. |
| `ec2:CreateTags` | only when `ec2:CreateAction` is `RunInstances` or `CreateVolume` | Tagging at creation. Developers cannot retag existing resources. |
| `ec2:Start/Stop/TerminateInstances`, `ModifyInstanceAttribute`, `Attach/Detach/DeleteVolume`, `GetConsoleOutput` | tagged resources | Lifecycle, resize, home volume attach, and boot-log diagnostics. |
| `iam:PassRole` | the execution role only, `iam:PassedToService = ec2.amazonaws.com` | Launch the box with its instance profile. |
| `ssm:StartSession` | tagged instances (`ssm:resourceTag/agent-platform`) **and only the `AWS-StartSSHSession` document**, with `ssm:SessionDocumentAccessCheck` | SSH tunnel. Shell sessions, port forwarding, interactive commands and `ssm:SendCommand` are **not** allowed. SSH keys provide per-developer isolation: only the owner's key is authorized on their box. |
| `ssm:TerminateSession`, `ssm:ResumeSession` | the caller's own sessions | End or resume their own tunnels. |
| `s3:ListBucket` (prefix `env/*`), `s3:GetObject/PutObject/DeleteObject` (`env/*`) | state bucket | Terraform state and lockfiles (lockfile release needs delete). |
| `iam:SimulatePrincipalPolicy` | the developer role itself | `doctor` checks that the policy is still attached. |

## Questions for the platforms team

Please answer these before applying in a managed account:

1. **AMI:** is a stock Canonical Ubuntu 24.04 AMI allowed, or must boxes use an
   approved or hardened image? (`ami_ssm_parameter` can point at your own
   parameter.)
2. **Required agents:** must boxes run patching, Inspector, EDR or other agents?
   They would be installed by `bootstrap/user-data.sh.tftpl` or baked into the AMI.
3. **Mandatory tags:** which tags must every resource carry? (`extra_tags`)
4. **KMS:** is a specific KMS key required for EBS? (`kms_key_id`; developers
   then need `kms:CreateGrant`/`Decrypt`/`GenerateDataKey*` on it, which this
   policy does not grant yet.)
5. **Network:** which VPC and subnets should boxes use (`vpc_mode = "attach"`)?
6. **Egress:** do those subnets reach the internet (cli.kiro.dev, claude.ai,
   herdr.dev, astral.sh, Ubuntu and Docker apt repositories) and the SSM
   endpoints?
7. **Role:** which developer role should get the policy, and should we use
   `attach` or integrate it into your role automation?
