# agent-platform — implementation plan

## 0. Read this first (for agents)

- **Status (2026-10-25):** M0–M8 implemented and tested in the personal
  account on t3.micro boxes (user's request; the default stays m7i.xlarge).
  See section 18 for collected facts and deviations from this spec.
- **Source of truth:** this file. `docs/design-log.md` is the verbatim interview
  history with question numbers (Q1–Q47); some of its entries are superseded.
  Use it only to understand *why*. Where they disagree, this file wins.
- **Items marked [SPEC]** were filled in while writing this spec, not discussed
  with the user. They are reasonable defaults; mention them to the user when you
  first implement them, and change them if the user objects.
- **Section 15 lists facts nobody has collected yet.** Collect them before the
  milestone that needs them; never guess.

## 1. Working rules for agents

1. **Git:** never `git add`, `git commit`, or `git push` unless the user asks.
   The directory is not a git repo yet; ask before running `git init`. Never add
   Claude/Anthropic attribution to commits or MRs. Use `glab` (not `gh`) for
   GitLab; `glab mr create` always gets `--remove-source-branch`.
2. **Python:** `uv` only (`uv init`, `uv add`, `uv run`, `uv sync`). Never pip,
   poetry, or user-level installs. Dependencies live in the project `.venv`.
3. **AWS scope:**
   - The only AWS environment you may *change* is the user's **personal test
     account**, and only after the user confirms each `terraform apply` /
     `destroy` (they cost money and are outward-facing).
   - The managed account (profile `managed-dev`) is **read-only and narrow**: the
     user authorized inspecting the developer role only. Do not list or describe
     other resources there without asking. Never apply anything there; the
     platform layer is handed to the platforms team.
4. **Secrets:** never write real tokens or credentials into tracked paths. They
   belong only in `home.local/` (gitignored) and on the box.
5. **Unpinned tools are intentional.** Do not add version pins to vendor install
   scripts (user decision).

## 2. Goal and scope

A git repo that a handful of developers clone. With it, each developer can:
create one or more personal EC2 dev boxes in AWS, reach them over SSH tunnelled
through SSM, push their personal config and OAuth tokens to them, and run Kiro
CLI and Claude Code under **herdr** for long-running headless agent runs they
detach from and come back to.

Two deployment targets, same code:
- **Personal AWS account** (full control): the user tests everything here first.
- **Managed account** (`managed-dev`, us-east-1): the platforms team
  applies the platform layer; developers use their existing SSO role.

**Out of scope for v1:** GitLab integration (the box uses a local `git init`
only), notifications when runs finish, backups or snapshots of any kind, version
pinning, a baked AMI, a CloudFormation version of the platform layer, Graviton.

## 3. Architecture

```
laptop (WSL)                                     AWS account
------------                                     -----------
agent-platform CLI (uv) --terraform--> S3 state bucket (env/<user>/<env>/...)
        |                              volume root  -> EBS gp3 (holds /home/ubuntu)
        |                              instance root -> EC2 Ubuntu 24.04
        |                                                  + execution instance profile
        +--ssh (ProxyCommand: aws ssm start-session        + zero-inbound security group
             --document-name AWS-StartSSHSession) -----> sshd on box
        +--rsync over that ssh: home/ then home.local/ --> /home/ubuntu
developer on box: `herdr` -> panes running claude / kiro-cli
on-box systemd timer: idle check -> `shutdown` (instance stops, volume kept)
```

Three Terraform roots:
- `terraform/platform` — once per account, applied by an admin with plain
  terraform. The CLI never touches it.
- `terraform/volume` — per environment, applied by the CLI.
- `terraform/instance` — per environment, applied by the CLI. Destroying it
  leaves the volume (and the home directory) intact.

## 4. Repo layout

```
PLAN.md                     this file
README.md                   developer quickstart
pyproject.toml, uv.lock     the CLI (console script: agent-platform)
src/agent_platform/         CLI package
tests/                      pytest
terraform/
  platform/                 admin-applied root (+ README for the platforms team)
  modules/developer-policy/ standalone module: developer IAM policy + JSON output
  volume/                   per-env root
  instance/                 per-env root
bootstrap/
  user-data.sh.tftpl        first-boot script (templated by the instance root)
  idle-check.sh             on-box idle logic (installed by user-data)
  idle-check.service/.timer
accounts/
  personal.toml             written from platform outputs (committed)
  managed.toml          ditto, once the platforms team applies
home/                       committed shared defaults synced onto the box
home.local/                 GITIGNORED personal overlay (tokens, personal config)
.agent-platform.local.toml  GITIGNORED per-developer settings
.state/                     GITIGNORED terraform data dirs per env
docs/
  design-log.md             interview history (historical)
```

`.gitignore` must contain at least: `home.local/`, `.agent-platform.local.toml`,
`.state/`, `.venv/`, `*.tfstate*`, `.terraform/`.

## 5. Platform root (`terraform/platform`)

**Creates:**
- **Execution role + instance profile.** Trusts `ec2.amazonaws.com`. Attaches
  `AmazonSSMManagedInstanceCore`. Admins attach further policies later;
  developers cannot modify it. Agents on the box use this role, never copied
  developer credentials.
- **State bucket.** Versioning on, public access blocked, SSE on. Terraform uses
  S3 native locking (`use_lockfile = true`; no DynamoDB). Terraform ≥ 1.10.
- **Security group.** Zero inbound rules. Egress all (needed for SSM, the
  vendor install scripts, apt, Docker).
- **Network**, by `vpc_mode`:
  - `create`: minimal VPC, one public subnet per listed AZ, internet gateway,
    public IPs on launch (still zero inbound).
  - `attach`: takes `vpc_id` and `subnet_ids` as variables, creates nothing.
- **Developer permissions**, by `developer_role_mode`:
  - `create` (fresh account): creates an `agent-platform-developer` role that
    trusts `trusted_principal_arns`, attaches the developer policy. The CLI
    assumes this role.
  - `attach` (existing account): creates the developer policy as a
    customer-managed policy and attaches it to `existing_role_name`. The CLI
    uses the developer's own login with no extra role hop.
  - *integrate* (not a mode; a handoff path): the platforms team calls
    `terraform/modules/developer-policy` from their own Terraform, or copies its
    `policy_json` output, and skips this root's role resources. The platform
    README recommends this for any role reconciled by central automation (e.g.
    `AWSReservedSSO_*`), because a policy attached outside that automation may be
    silently removed.

**Variables:** `region`, `name_prefix` (default `agent-platform`), `vpc_mode`,
`vpc_id`, `subnet_ids`, `azs`, `developer_role_mode`, `trusted_principal_arns`,
`existing_role_name`, `ami_ssm_parameter` (default the Canonical Ubuntu 24.04
amd64 gp3 parameter), `kms_key_id` (optional; default AWS-managed EBS
encryption), `extra_tags` (map, merged into every resource's tags).

**Output:** `account_file` — the full text of `accounts/<name>.toml` (section
11.1). The admin pastes it into the repo.

**Platform README** (for the platforms team) must list every resource, every IAM
action and why it is needed, the two modes and the integrate path, and ask
explicitly: approved AMI requirements, required agents (patching, Inspector),
mandatory tags, required KMS key, which VPC/subnets to attach to, and whether the
subnet has egress to the internet (install scripts) and to SSM endpoints.

## 6. IAM

### 6.1 Tags (on every resource the environment roots create)

| Key | Value |
|---|---|
| `agent-platform` | `true` |
| `agent-platform:owner` | developer username |
| `agent-platform:env` | environment name |

Plus `extra_tags` from the account file.

### 6.2 Developer policy (`modules/developer-policy`)

Scoped so developers can only touch resources tagged `agent-platform=true`:

- **Read:** `ec2:Describe*`, `ssm:GetParameter` on the AMI public parameter,
  `sts:GetCallerIdentity`.
- **Create:** `ec2:RunInstances` and `ec2:CreateVolume` require
  `aws:RequestTag/agent-platform = true` on instance/volume/network-interface
  resources; `ec2:CreateTags` allowed only when `ec2:CreateAction` is
  `RunInstances` or `CreateVolume`. RunInstances also needs the subnet, security
  group, and image resources (unconditioned).
- **Manage:** `StartInstances`, `StopInstances`, `TerminateInstances`,
  `ModifyInstanceAttribute`, `AttachVolume`, `DetachVolume`, `DeleteVolume`,
  `GetConsoleOutput` require `aws:ResourceTag/agent-platform = true`.
- **PassRole:** `iam:PassRole` on the execution role ARN only, with
  `iam:PassedToService = ec2.amazonaws.com`.
- **SSM (user decision: SSH is forced, SSM only provides the tunnel):**
  `ssm:StartSession` on tagged instances **and only with the
  `AWS-StartSSHSession` document**, with condition
  `ssm:SessionDocumentAccessCheck = true`. Without that condition, a call that
  omits the document name falls back to the default shell document
  (`SSM-SessionManagerRunShell`) and is allowed. Not allowed: the default
  shell document, `AWS-StartPortForwardingSession`, `AWS-StartInteractiveCommand`,
  `ssm:SendCommand`. Per-developer isolation comes from SSH keys: only the
  owner's public key is authorized on their box. `ssm:TerminateSession` / `ssm:ResumeSession` on
  `session/${aws:userid}-*`.
- **State:** `s3:ListBucket` on the state bucket (prefix `env/`);
  `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `env/*` (lockfiles need
  delete).
- **Doctor:** `iam:SimulatePrincipalPolicy` on the developer's own role.

### 6.3 Known role facts (managed account, verified 2026-10-23)

Profile `managed-dev` lands directly in SSO role
`<sso-developer-role>` (1 h max session, no boundary).
A policy simulation showed it **cannot** do `ec2:RunInstances`, `CreateVolume`,
`Start/Stop/TerminateInstances`, `CreateSecurityGroup`, `CreateVpc`,
`ssm:StartSession`, `s3:CreateBucket`, `s3:PutObject`, or IAM role creation. It
**can** do `iam:PassRole` and `cloudwatch:PutMetricAlarm`. So in the managed
account nothing works until the platforms team applies the platform layer in
`attach` mode or integrates the developer policy.

## 7. Environment roots

### 7.1 Naming and state

- Environment ID: `<user>-<env>`. A developer may have several.
- State keys: `env/<user>/<env>/volume.tfstate` and
  `env/<user>/<env>/instance.tfstate`, passed with `-backend-config` at init.
- The CLI runs terraform with `-chdir=terraform/<root>` and
  `TF_DATA_DIR=.state/<user>-<env>/<root>` so environments never share a
  `.terraform` directory.

### 7.2 Volume root

- One gp3 volume, default 100 GB, encrypted (KMS key if given), tagged.
- **[SPEC] AZ pinning:** the volume's AZ is fixed by the subnet chosen at first
  create. The CLI records that subnet in the volume root's outputs and the
  instance root always uses the same subnet, so a recreated instance can attach.
- Outputs: `volume_id`, `subnet_id`, `availability_zone`.

### 7.3 Instance root

- Ubuntu 24.04 LTS x86_64 from the AMI SSM parameter in the account file.
  Default type `m7i.xlarge`; `--size` overrides per environment. Re-running
  `create` with a new size resizes in place.
- Root volume: small gp3, disposable (holds OS, the Docker engine, /tmp).
  Docker's data-root is on the home volume (section 19).
- Attaches the execution instance profile and the platform security group.
- `instance_initiated_shutdown_behavior = "stop"` so the idle check's
  `shutdown` stops (never terminates) the instance.
- `aws_volume_attachment` to the volume from the volume root (read via that
  root's outputs, passed by the CLI as variables).
- Variables: `user`, `env`, `ssh_public_key`, `passwordless_sudo`
  (default `true`; changing it means destroy + recreate the instance),
  `idle_hours` (default 4), `instance_type`, plus account-file values.
- Outputs: `instance_id`.

## 8. First-boot script (`bootstrap/user-data.sh.tftpl`)

Runs as root once per instance (so again after every recreate). Must be
idempotent against an existing home volume. Order matters:

1. **SSM agent first**, so the box is reachable even if a later step fails:
   `snap refresh amazon-ssm-agent`, then `snap refresh --hold amazon-ssm-agent`
   (background refreshes restart the agent and drop sessions), ensure
   `snap.amazon-ssm-agent.amazon-ssm-agent.service` is running.
2. **Find the home volume** by its volume ID via `/dev/disk/by-id/nvme-Amazon_
   Elastic_Block_Store_vol<id-without-dash>` (Nitro renames devices). Format
   ext4 **only if it has no filesystem**. Add an fstab entry by UUID with
   `nofail`, mount at `/home/ubuntu`.
3. **The login user is Ubuntu's default `ubuntu`** (user decision,
   2026-10-25: UID 1000 from cloud-init, the same on every box, matches
   container images). The developer's name is only the owner tag and env
   prefix. Install
   the SSH public key into `~/.ssh/authorized_keys`. Passwordless sudo via
   `/etc/sudoers.d/` only if `passwordless_sudo` is true.
4. **[SPEC] Stable SSH host keys:** if
   `/home/ubuntu/.agent-platform/hostkeys/` exists (root-owned, 0700), copy those
   keys into `/etc/ssh`; otherwise save the freshly generated keys there.
   Recreated instances then keep the same host identity, so SSH does not raise
   a host-key-changed warning.
5. `loginctl enable-linger <user>` (required so herdr's server survives SSH
   logout).
6. apt: `git build-essential jq ripgrep unzip python3 rsync`; Docker (official
   Docker apt repo) with the user in the `docker` group.
7. **As the user**, run the four vendor installers unpinned (they install into
   `~/.local`, which lives on the home volume):
   ```
   curl -fsSL https://cli.kiro.dev/install | bash
   curl -fsSL https://claude.ai/install.sh | bash
   curl -fsSL https://herdr.dev/install.sh | bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
8. Install `idle-check.sh` and its systemd service and timer (section 10).
9. Write a completion marker `/var/lib/agent-platform/bootstrap-done` with a
   timestamp. `doctor` reads it; on failure the CLI fetches the boot log with
   `ec2:GetConsoleOutput`.

herdr is **not** started at boot. The developer runs `herdr` after SSH.

## 9. Access and sync

### 9.1 SSH over SSM

The CLI writes a managed file `~/.ssh/agent-platform.conf` and makes sure
`~/.ssh/config` has `Include agent-platform.conf` at the top. One entry per
environment:

```
Host <user>-<env>
  HostName <instance-id>
  User ubuntu
  ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p --profile <profile> --region <region>
```

In `create` role mode, the CLI also writes an AWS CLI profile
`agent-platform-<account>` with `role_arn` and `source_profile`, and the
ProxyCommand uses it. The instance ID changes on recreate; the CLI rewrites the
entry.

### 9.2 Sync (`sync <env>`)

- `rsync` over the SSH entry: first `home/`, then `home.local/`, onto
  `/home/ubuntu`. **Repo always wins** for files it contains. **Never deletes**
  remote files. Never touches paths not present in those directories.
- Refuses to run if `claude` or `kiro-cli` processes are running on the box
  (Kiro's SQLite file would race the live process).
- Refuses if `git` reports untracked files under `home/` matching credential
  patterns (`.credentials.json`, `data.sqlite3`, `*token*`, `*.pem`).
- Does not rewrite paths; `doctor` warns on `/mnt/` references.

What developers typically put in `home.local/` (paths relative to home):

| Path | What |
|---|---|
| `.claude/.credentials.json` | Claude Code OAuth token |
| `.claude/settings.json`, `.claude/CLAUDE.md`, `.claude/skills/`, `.claude/plugins/` | Claude Code config |
| `.kiro/agents/`, `.kiro/settings/`, `.kiro/skills/`, `.kiro/steering/` | Kiro config |
| `.local/share/kiro-cli/data.sqlite3` | Kiro auth token (whole file; also holds history) |
| `.gitconfig` | git identity |
| `.config/herdr/config.toml` | herdr config |
| `.agent-platform/env` | exported env vars, sourced from `.bashrc` |

**Never sync** the rest of `~/.local/share/kiro-cli/` (about 200 MB of bundled
`bun`, `node`, `tui.js` runtimes). The README must say so; `home/` ships a
README telling developers how to populate `home.local/`.

Tokens exist only on the laptop and the box. Never in SSM Parameter Store or
Secrets Manager. Accepted risk: refresh-token rotation may invalidate one copy.

## 10. Idle stop (`bootstrap/idle-check.sh`)

A root systemd timer runs every 5 minutes. The box is **active** if any holds:

1. A herdr agent is working (queried as the user via herdr's CLI/socket,
   `~/.config/herdr/herdr.sock`).
2. A herdr agent has been blocked for less than 24 hours. The script records
   first-seen-blocked time per pane in `/var/lib/agent-platform/`.
3. A systemd-logind session has been idle for less than 30 minutes
   (`loginctl show-session <id> -p IdleHint -p IdleSinceHint`). This comes from
   the terminal device's access timestamp; no keystroke content is recorded.
4. A herdr pane's foreground process is not its shell (from `/proc`,
   independent of herdr's API).
5. `~/.agent-platform/hold-until` holds a future timestamp (written by `hold`).

Not signals: CPU, running Docker containers, merely open sessions.

After `idle_hours` (default 4) with no active check: `wall` a 15-minute warning,
re-check, then `shutdown -h now` (instance stops; volume kept). **Fail safe:** if
the herdr query errors, treat the box as active. The decision logic is a pure
function with unit tests; the shell script only gathers inputs.

## 11. CLI (`agent-platform`, run with `uv run agent-platform ...`)

Python package using `uv`; wraps terraform via subprocess and uses boto3 for
waits, lookups, console output, and role assumption. In `create` role mode it
assumes the developer role before each command (chained sessions are capped at
1 hour, so assume per command).

| Command | Behavior |
|---|---|
| `create <env> [--size T]` | apply volume root, then instance root; wait for SSM online and bootstrap marker; write SSH entry. Re-run with new `--size` to resize. |
| `destroy <env> [--volume]` | destroy instance root; with `--volume`, also the volume root after typed confirmation. |
| `stop <env>` / `start <env>` | boto3 stop/start; wait for state. |
| `list` | this developer's environments: state, type, uptime, rough hourly cost. |
| `ssh <env>` | exec `ssh <user>-<env>`. |
| `sync <env>` | section 9.2. |
| `doctor <env>` | section 12. |
| `hold <env> <duration>` | writes `~/.agent-platform/hold-until` on the box over SSH. |

### 11.1 Account file (`accounts/<name>.toml`, committed)

```toml
name = "personal"
region = "us-east-1"
state_bucket = "..."
developer_role_arn = ""           # set in create mode; empty in attach mode
execution_instance_profile = "..."
security_group_id = "sg-..."
subnet_ids = ["subnet-..."]
ami_ssm_parameter = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
kms_key_id = ""
[extra_tags]
```

### 11.2 Local settings (`.agent-platform.local.toml`, gitignored)

```toml
account = "personal"              # selects accounts/<account>.toml
aws_profile = "..."               # the developer's own source profile
user = "zach"                     # owner tag and env prefix; default $USER
ssh_public_key = "~/.ssh/id_ed25519.pub"
default_size = "m7i.xlarge"
passwordless_sudo = true
idle_hours = 4
```

## 12. `doctor <env>`

Each check prints pass, warn, or fail; exit non-zero on any fail.

- AWS credentials valid; developer role assumable (create mode).
- IAM policy simulation of section 6.2 actions on the caller's role (catches a
  silently removed policy).
- Instance running; SSM reports it online.
- Bootstrap marker present; if not, show the console output tail.
- SSM agent snap service running; refresh hold present (warn if missing).
- From the box: reach `cli.kiro.dev`, `claude.ai`, `herdr.dev`, `astral.sh`, and
  SSM endpoints; GitLab reachability is warn-only.
- On PATH: `kiro-cli`, `claude`, `herdr`, `uv`, `git`, `docker`.
- Linger enabled; home volume mounted at `/home/ubuntu`.
- Token files present; Claude Code reports signed in.
- SSH-over-SSM logins register with logind and have a tty (needed by idle
  signal 3).
- No `/mnt/` paths in synced files (warn).

## 13. Milestones

Work in order. Each ends with its check passing. Ask the user before every
`terraform apply` or `destroy`.

| # | Deliverable | Check |
|---|---|---|
| M0 | Repo skeleton, `uv init`, `.gitignore`, CLI stub, pytest, `terraform fmt`/`validate` wiring, shellcheck for bootstrap scripts | `uv run pytest` and `terraform validate` pass on all roots |
| M1 | Platform root (`create` VPC mode, `create` role mode) and developer-policy module | applied in personal account; `account_file` output written to `accounts/personal.toml` |
| M2 | Volume + instance roots; user-data steps 1–5; SSH entry writing; `create`, `destroy`, `ssh` | `ssh <user>-<env>` works through SSM; home mounted; host key stable across recreate |
| M3 | User-data steps 6–9; `doctor` | `doctor` passes clean |
| M4 | `sync` with guards | Claude Code and Kiro both signed in from synced tokens |
| M5 | Idle check, timer, `hold` | unit tests for decision logic; box stops after idle window in a shortened test (`idle_hours` small); stays up with a working agent or a hold |
| M6 | `stop`, `start`, `list`, `--size` resize, multiple environments | two environments coexist with separate state |
| M7 | `attach` modes, platform README for the platforms team, developer README, `home/` README | README review by the user |
| M8 | Acceptance test (section 14) | all steps pass |

## 14. Acceptance test (personal account)

1. `create` an environment; `doctor` passes.
2. `ssh` in through SSM; run `herdr`; launch Claude Code and Kiro, both signed in
   from synced tokens.
3. Start a long task; detach; close the laptop; return; reattach; task still
   running.
4. `stop` and `start`; herdr restores its layout.
5. `destroy` the instance only; `create` again; home directory intact, no SSH
   host-key warning.
6. Create a second environment in parallel; names and state keys don't collide.
7. Confirm SSH-over-SSM sessions register with logind with a tty.
8. `destroy --volume` both environments.

## 15. Open facts (collect before the milestone that needs them)

| Fact | Needed by | How |
|---|---|---|
| Personal test account: AWS profile name, region, principal ARN to trust | M1 | ask the user |
| herdr's machine-readable agent-state output (command and JSON shape for working/blocked) | M5 | test on the first box; `herdr --help`, docs at herdr.dev |
| Whether logind registers SSH-over-SSM sessions with a tty | M5 | test on the first box |
| Managed account: approved AMI, required agents, mandatory tags, KMS key, VPC/subnet IDs, egress path | Managed-account handoff | platforms team (asked in platform README) |

Deferred decisions: where this repo lives in GitLab; GitLab access from boxes;
notifications.

## 16. Accepted risks

- Unpinned vendor installers: each new box may get different versions of
  pre-1.0 herdr, Kiro, and Claude Code.
- Copied OAuth refresh tokens may invalidate one side on rotation and kill a run.
- No backups; the home volume holds unpushed work.
- Managed-account self-serve depends entirely on the platforms team applying the
  platform layer.
- Kiro's `data.sqlite3` schema is undocumented; copied whole while Kiro is
  stopped.
- herdr is pre-1.0 and reportedly unstable nested inside tmux; don't wrap it in
  tmux.

## 17. Reference facts

- **Local (user's WSL):** Terraform 1.15.5, aws CLI, session-manager-plugin,
  uv 0.11.3, Claude Code 2.1.309, kiro-cli. Related repos:
  `~/source/agent-loop` (Kiro loop runner, Webex notifications),
  an SSO helper (`sso` CLI that writes `~/.aws/credentials`).
- **herdr** (v0.9.5 at time of writing; static-pie binary): server keeps panes
  alive after detach (`ctrl+b q`), `herdr` reattaches; layout restored from
  session files after a server restart. Socket `~/.config/herdr/herdr.sock`;
  config `~/.config/herdr/config.toml`. `herdr machine add` lets a laptop drive
  a remote herdr server (optional for developers).
- **Claude Code:** credentials `~/.claude/.credentials.json`; officially supports
  Ubuntu 20.04+ and Debian 10+.
- **Kiro CLI:** needs glibc 2.34+; token inside
  `~/.local/share/kiro-cli/data.sqlite3`.
- **OS choice:** Ubuntu 24.04 is on AWS's SSM supported list with the agent
  preinstalled (as a snap). Debian 13 is not on that list (only 11 and 12);
  Amazon Linux 2023 is not on Claude Code's list and sits exactly at Kiro's
  glibc minimum. Ubuntu matches the developers' WSL default.

## 18. Implementation notes (2026-10-25)

### Facts collected (section 15)

- **Personal test account:** profile `sandbox-admin` (IAM user, renamed from `agent-admin`), account
  111122223333, us-east-1. It is an AWS Organizations **management account**
  with SCPs; SCPs don't bind it, but `iam:SimulatePrincipalPolicy` still applies
  them, so `doctor` reports SCP-only simulator denials as warnings.
- **Test developers:** IAM users `zach` and `alice` (`terraform/test-users`,
  personal account only), trusted by the developer role. That root also holds a
  stand-in "existing" role used to test `developer_role_mode = "attach"`.
- **herdr agent state:** `herdr api snapshot` returns JSON with
  `result.snapshot.panes[].{pane_id, agent_status}` (`working`, `blocked`,
  `done`, `idle`, `unknown`). Claude Code's folder-trust prompt shows as `blocked`.
- **logind:** SSH-over-SSM logins register as `Class=user` sessions with
  `TTY=pts/N`, so idle signal 3 works.
- **Auth:** the user chose env-var tokens, `CLAUDE_CODE_OAUTH_TOKEN` and
  `KIRO_API_KEY`, in `home.local/.agent-platform/env`. Both sign in.
  `claude auth status` and `kiro-cli whoami` are the doctor checks.

### Deviations and additions

- `ssm:StartSession` on instances uses `ssm:resourceTag/agent-platform`
  (StartSession does not match on `aws:ResourceTag`), and
  `ssm:SessionDocumentAccessCheck` uses `BoolIfExists`. Verified: the
  SSH document works; the default shell document, port forwarding, and
  `ssm:SendCommand` are denied.
- Developer policy adds `ssm:DescribeInstanceInformation` (wait for SSM online).
  `RunInstances` on network interfaces is unconditioned (`aws_instance` cannot
  tag the ENI at launch).
- Account file adds `developer_policy_role_arn` (the role `doctor` simulates,
  in both role modes).
- Local settings add `volume_gb` (default 100). `AGENT_PLATFORM_SETTINGS`
  overrides the settings file path (one checkout acting as several developers).
- The SSH AWS profile is `agent-platform-<account>-<user>`, not
  `agent-platform-<account>`, so two developers can share a laptop.
- SSH entries use `HostKeyAlias <user>-<env>` so the stable host key is checked
  across recreates (the HostName instance ID changes).
- The instance ignores AMI changes (a new Ubuntu release must not replace the
  box). Changing user data replaces the box (`user_data_replace_on_change`).
  `idle_hours` is written to `/etc/agent-platform/idle.conf`.
- `.bashrc` line 1 (before Ubuntu's non-interactive return) puts `~/.local/bin`
  on PATH and sources `~/.agent-platform/env`, so `ssh box cmd` and
  `herdr --remote` work.
- The box user is always `ubuntu` (UID 1000); local settings have no `uid`.
  `passwordless_sudo = false` removes cloud-init's sudoers rule.
- Kiro's installer prompts on `/dev/tty` over an existing install, so user-data
  removes the old binaries first.
- `sync` copies each top-level entry rather than `dir/`, so the remote home
  directory's mode never changes. It skips `home/README.md`. Its credential
  guard checks all files under `home/`, tracked or not.
- Idle decision logic is `bootstrap/idle_decide.py` (stdlib, unit-tested).
  Signal 4 ignores panes whose foreground is an agent (`claude`, `kiro-cli*`,
  `node`, `bun`); agents are judged by signals 1–2, or a blocked agent would
  never let the box stop.

### Known gaps

- Claude Code often runs long shell commands as **background shells** and
  reports `done` while they run. The idle check does not see those (they are
  not a pane's foreground), so such a box can stop after `idle_hours`. Use
  `hold`, or a future signal that counts agent child processes.
- A customer-managed KMS key (`kms_key_id`) would need KMS grants in the
  developer policy; untested.

## 19. Changes for the demo account (2026-10-27)

- Demo account 123456789012, admin profile `zach` (IAM user `zach`, group
  `admin`). The admin applies `terraform/platform` with `demo.tfvars` (create
  VPC mode, create role mode trusting `user/zach`); `accounts/demo.toml` is the
  output. The developer CLI uses the same profile as its source profile and
  assumes `agent-platform-developer`, so it runs with the scoped policy only.
- Box size m7i.xlarge (4 vCPU, 16 GiB), the default.
- Docker's data-root moved to the home volume
  (`/home/ubuntu/.agent-platform/docker`, root-owned 0710), with
  `containerd-snapshotter` off so images stay under data-root, and a
  `RequiresMountsFor=/home/ubuntu` drop-in on `docker.service`. Project compose
  stacks (niflow-canvas: NiFi cluster, Airflow, Postgres) keep their images and
  volumes across `destroy`/`create`. `doctor` checks the data-root.
