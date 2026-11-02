# barn — implementation plan

## 0. Read this first (for agents)

- **Status (2026-10-25):** M0–M8 implemented and tested in the personal
  account on t3.micro boxes (user's request; the default stays m7i.xlarge).
  See section 18 for collected facts and deviations from this spec.
- **2026-10-29:** the platform layer was removed and the CLI became an
  installable tool (section 20), then the project was renamed from
  agent-platform to **barn** (section 21). Sections 2–12 describe the current design;
  sections 13–19 are history.
- **2026-11-02:** managers and guests, and full box names instead of
  `<user>-<env>` (section 23), implemented in barn and the demo platform and
  folded into sections 2–12. Sandbox verification status: section 23.10.
- **2026-11-02:** the user's setup moved to their laptop, with the `zach`
  admin user as source profile (section 24). This file moved from the repo
  root to `docs/plans/PLAN.md`.
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
   Never add
   Claude/Anthropic attribution to commits or MRs. Use `glab` (not `gh`) for
   GitLab; `glab mr create` always gets `--remove-source-branch`.
2. **Python:** `uv` only (`uv init`, `uv add`, `uv run`, `uv sync`). Never pip,
   poetry, or user-level installs. Dependencies live in the project `.venv`.
3. **AWS scope:**
   - The only AWS environment you may *change* is the **sandbox account
     123456789012** (admin profile `zach`, us-east-1; the earlier personal
     account's profile no longer exists), and only after the user confirms each
     `terraform apply` / `destroy` and each delete or retag (they cost money and
     are outward-facing). Read-only calls there are fine.
   - The managed account (profile `managed-dev`) is **read-only and narrow**: the
     user authorized inspecting the developer role only. Do not list or describe
     other resources there without asking. Never apply anything there; the
     account prerequisites are handed to the platforms team.
4. **Secrets:** never write real tokens or credentials into tracked paths. They
   belong only in `~/.config/barn/home/` and on the box. Test users' AWS access
   keys go only into `~/.aws/credentials` (and stay in `test-users`' local,
   gitignored state).
5. **Unpinned tools are intentional.** Do not add version pins to vendor install
   scripts (user decision).

## 2. Goal and scope

A CLI that a handful of developers install. With it, a **manager** can
create one or more EC2 dev boxes in AWS, reach them over SSH tunnelled
through SSM, push their personal config and OAuth tokens to them, and run Kiro
CLI and Claude Code under **herdr** for long-running headless agent runs they
detach from and come back to. A box belongs to its creator; **guests** (and
other managers) can connect to it to watch or steer its agents or upload files
(section 23).

The tool manages **only** per-environment home volumes and EC2 instances. It
assumes the account's network, security group, instance profile, state bucket
and developer permissions already exist (docs/account-prerequisites.md).

Two deployment targets, same code:
- **Sandbox AWS account** (full control): the user tests everything here first.
- **Managed account** (`managed-dev`, us-east-1): the platforms team
  provides the prerequisites; people use SSO roles carrying the two policies.

**Out of scope for v1:** GitLab integration (the box uses a local `git init`
only), notifications when runs finish, backups or snapshots of any kind, version
pinning, a baked AMI, a CloudFormation version of the platform layer, Graviton.

## 3. Architecture

```
laptop (WSL)                                     AWS account
------------                                     -----------
barn CLI (uv tool) -terraform-> S3 state bucket (env/<name>/...)
        |                              volume root  -> EBS gp3 (holds /home/ubuntu)
        |                              instance root -> EC2 Ubuntu 24.04
        |                                                  + execution instance profile
        +--ssh (ProxyCommand: aws ssm start-session        + zero-inbound security group
             --document-name AWS-StartSSHSession) -----> sshd on box
        +--rsync over that ssh: shared_home, then ~/.config/barn/home/ --> /home/ubuntu
developer on box: `herdr` -> panes running claude / kiro-cli
on-box systemd timer: idle check -> `shutdown` (instance stops, volume kept)
```

Two Terraform roots, shipped inside the Python package and copied into
`~/.config/barn/terraform/` on every run (terraform always runs there):
- `volume` — per box.
- `instance` — per box. Destroying it leaves the volume (and the home
  directory) intact.

Account setup is out of scope. Each account's prerequisites come from its
**platform**, infrastructure code outside this repo (one repo per account);
barn's docs never name one. Agents ask the user where an account's platform is
(`docs/onboarding.md`).

## 4. Repo layout

```
README.md                   developer quickstart
pyproject.toml, uv.lock     the CLI (console script: barn; `uv tool install`)
src/barn/         CLI package
  terraform/volume/         per-box root (package data)
  terraform/instance/       per-box root (package data)
  bootstrap/
    user-data.sh.tftpl      first-boot script (templated by the instance root)
    idle-check.sh           on-box idle logic (installed by user-data)
    idle-check.service/.timer
    authorized_keys.py      owner/guests blocks of authorized_keys (first boot and the CLI)
tests/                      pytest
docs/
  account-prerequisites.md  what an account must provide; the account file
  onboarding.md             order of work: a new account, adding/removing people
  manager-policy.json       the manager IAM policy, with placeholders
  guest-policy.json         the guest IAM policy, with placeholders
  design-log.md             interview history (historical)
  plans/PLAN.md             this file
src/barn/templates/         config.toml, account.toml, env written by `barn init`
```

Local, per developer (`~/.config/barn/`, or `$BARN_HOME`):

```
config.toml                 settings (section 11.2)
accounts/<name>.toml        account file (section 11.1)
home/                       personal overlay synced onto the box (tokens, config)
terraform/, bootstrap/      managed copies of the package's roots; never edited
state/<name>/<root>/        TF_DATA_DIR per box and root
state/plugin-cache/         TF_PLUGIN_CACHE_DIR
```

## 5. Account prerequisites (not managed by this tool)

The CLI assumes these exist; docs/account-prerequisites.md is the full contract:
subnets with egress to the internet and the SSM endpoints, a security group
(no inbound needed), an EC2 instance profile with `AmazonSSMManagedInstanceCore`,
an S3 state bucket, an Ubuntu 24.04 AMI SSM parameter, the roles
`barn-manager` and `barn-guest` (section 6.2), and an AWS CLI profile per
person and role. The CLI never creates, assumes or modifies IAM roles; each
person's AWS CLI profile assumes their role.

## 6. IAM

### 6.1 Tags (on every resource the environment roots create)

| Key | Value |
|---|---|
| `barn` | `true` |
| `barn:name` | box name (section 7.1) |
| `barn:owner` | creator's `user` (informational, and the owner guard) |

Plus `extra_tags` from the account file.

### 6.2 Manager and guest policies

`docs/guest-policy.json`: `ec2:DescribeInstances`, `ec2:DescribeVolumes`,
`ssm:DescribeInstanceInformation` on `*`, plus the manager policy's two
`StartSshSession*` statements verbatim. Nothing else.

`docs/manager-policy.json` (the earlier "developer policy"; sections 18 and 22
list later changes), scoped so managers can only touch resources tagged
`barn=true`:

- **Read:** `ec2:Describe*`, `ssm:GetParameter` on the AMI public parameter,
  `sts:GetCallerIdentity`.
- **Create:** `ec2:RunInstances` and `ec2:CreateVolume` require
  `aws:RequestTag/barn = true` on instance/volume/network-interface
  resources; `ec2:CreateTags` allowed only when `ec2:CreateAction` is
  `RunInstances` or `CreateVolume`. RunInstances also needs the subnet, security
  group, and image resources (unconditioned).
- **Manage:** `StartInstances`, `StopInstances`, `TerminateInstances`,
  `ModifyInstanceAttribute`, `AttachVolume`, `DetachVolume`, `DeleteVolume`,
  `GetConsoleOutput` require `aws:ResourceTag/barn = true`.
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
- **Doctor:** `iam:SimulatePrincipalPolicy` on the manager role
  (`MANAGER_ROLE_ARN`).

### 6.3 Known role facts (managed account, verified 2026-10-23)

Profile `managed-dev` lands directly in SSO role
`<sso-developer-role>` (1 h max session, no boundary).
A policy simulation showed it **cannot** do `ec2:RunInstances`, `CreateVolume`,
`Start/Stop/TerminateInstances`, `CreateSecurityGroup`, `CreateVpc`,
`ssm:StartSession`, `s3:CreateBucket`, `s3:PutObject`, or IAM role creation. It
**can** do `iam:PassRole` and `cloudwatch:PutMetricAlarm`. So in the managed
account nothing works until the platforms team provides the prerequisites and
attaches the developer policy.

## 7. Environment roots

### 7.1 Naming and state

- Box name: chosen by the manager in full (`barn create zach-dev`),
  `^[a-z][a-z0-9-]{0,38}[a-z0-9]$`, unique per account, not `workspaces` or
  `plugin-cache`. It is
  the SSH alias, the hostname, the `barn:name` tag and `Name = barn-<name>`.
  A person may have several boxes.
- State keys: `env/<name>/volume.tfstate` and `env/<name>/instance.tfstate`,
  passed with `-backend-config` at init.
- The CLI copies the packaged roots into `~/.config/barn/terraform/`
  (only changed files, atomically) and runs terraform with
  `-chdir=<home>/terraform/<root>` and
  `TF_DATA_DIR=<home>/state/<name>/<root>`, so boxes never share a
  `.terraform` directory and the location never depends on a checkout.

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
- Variables: `user` (owner tag), `name`, `ssh_public_key`, `passwordless_sudo`
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
   container images). The creator's name is only the owner tag. Write
   the owner's SSH public key into the owner block of `~/.ssh/authorized_keys`
   (section 23.4: other blocks and hand-added keys are kept; a file with no
   barn blocks is replaced). Passwordless sudo via
   `/etc/sudoers.d/` only if `passwordless_sudo` is true.
4. **[SPEC] Stable SSH host keys:** if
   `/home/ubuntu/.barn/hostkeys/` exists (root-owned, 0700), copy those
   keys into `/etc/ssh`; otherwise save the freshly generated keys there.
   Recreated instances then keep the same host identity, so SSH does not raise
   a host-key-changed warning.
5. `loginctl enable-linger <user>` (required so herdr's server survives SSH
   logout).
6. apt: `git build-essential jq ripgrep unzip unar python3 rsync`; Docker (official
   Docker apt repo) with the user in the `docker` group; Neovim, the latest
   stable release from GitHub (Ubuntu's is too old for most configs).
7. **As the user**, run the four vendor installers unpinned (they install into
   `~/.local`, which lives on the home volume):
   ```
   curl -fsSL https://cli.kiro.dev/install | bash
   curl -fsSL https://claude.ai/install.sh | bash
   curl -fsSL https://herdr.dev/install.sh | bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
8. Install `idle-check.sh` and its systemd service and timer (section 10).
9. Write a completion marker `/var/lib/barn/bootstrap-done` with a
   timestamp. `doctor` reads it; on failure the CLI fetches the boot log with
   `ec2:GetConsoleOutput`.

herdr is **not** started at boot. The developer runs `herdr` after SSH.

## 9. Access and sync

### 9.1 SSH over SSM

The CLI writes a managed file `~/.ssh/barn.conf` and makes sure
`~/.ssh/config` has `Include barn.conf` at the top. One entry per box,
written by `create`, `start`, `connect`, `ssh` and `doctor` with the caller's
own key and profile:

```
Host <name>
  HostName <instance-id>
  User ubuntu
  ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p --profile <profile> --region <region>
```

`<profile>` is the caller's `aws_profile`. The instance ID changes on
recreate; the CLI rewrites the entry.

### 9.2 Sync (`sync <name>`)

- Manager only, and only on the caller's own box (owner guard, 23.4).
- First rewrites the guests block of the box's `authorized_keys` from
  `guest_keys`, even if it then skips the file copy because agents run.

- `rsync` over the SSH entry: first the optional `shared_home` (team
  defaults), then `~/.config/barn/home/`, onto `/home/ubuntu`.
  **Local always wins** for files it contains. **Never deletes** remote files.
  Never touches paths not present in those directories. Top-level `README.md`
  is skipped.
- Refuses to run if `claude` or `kiro-cli` processes are running on the box
  (Kiro's SQLite file would race the live process).
- Refuses if `shared_home` contains files matching credential patterns
  (`.credentials.json`, `data.sqlite3`, `*token*`, `*.pem`).
- Does not rewrite paths; `doctor` warns on `/mnt/` references.

What developers typically put in `~/.config/barn/home/` (paths relative to home):

| Path | What |
|---|---|
| `.claude/.credentials.json` | Claude Code OAuth token |
| `.claude/settings.json`, `.claude/CLAUDE.md`, `.claude/skills/`, `.claude/plugins/` | Claude Code config |
| `.kiro/agents/`, `.kiro/settings/`, `.kiro/skills/`, `.kiro/steering/` | Kiro config |
| `.local/share/kiro-cli/data.sqlite3` | Kiro auth token (whole file; also holds history) |
| `.gitconfig` | git identity |
| `.config/herdr/config.toml` | herdr config |
| `.barn/env` | exported env vars, sourced from `.bashrc` |

**Never sync** the rest of `~/.local/share/kiro-cli/` (about 200 MB of bundled
`bun`, `node`, `tui.js` runtimes). The README must say so.

Tokens exist only on the laptop and the box. Never in SSM Parameter Store or
Secrets Manager. Accepted risk: refresh-token rotation may invalidate one copy.

## 10. Idle stop (`bootstrap/idle-check.sh`)

A root systemd timer runs every 5 minutes. The box is **active** if any holds:

1. A herdr agent is working (queried as the user via herdr's CLI/socket,
   `~/.config/herdr/herdr.sock`).
2. A herdr agent has been blocked for less than 24 hours. The script records
   first-seen-blocked time per pane in `/var/lib/barn/`.
3. A systemd-logind session has been idle for less than 30 minutes
   (`loginctl show-session <id> -p IdleHint -p IdleSinceHint`). This comes from
   the terminal device's access timestamp; no keystroke content is recorded.
4. A herdr pane's foreground process is not its shell (from `/proc`,
   independent of herdr's API).
5. `~/.barn/hold-until` holds a future timestamp (written by `hold`).

Not signals: CPU, running Docker containers, merely open sessions.

After `idle_hours` (default 4) with no active check: `wall` a 15-minute warning,
re-check, then `shutdown -h now` (instance stops; volume kept). **Fail safe:** if
the herdr query errors, treat the box as active. The decision logic is a pure
function with unit tests; the shell script only gathers inputs.

## 11. CLI (`barn`, installed with `uv tool install`)

Python package using `uv`; wraps terraform via subprocess and uses boto3 for
waits, lookups and console output. boto3, terraform and the SSH ProxyCommand
all use the caller's profile for the account (`[aws_profiles]`) unchanged. Runs from any directory.
Built on Typer; output (spinners, colors, tables) goes through `barn.ui` on
rich. Terraform apply/destroy output is written to
`<home>/state/<account>/<name>/<root>/<command>.log` unless `-v` is given. AWS
errors (for example a guest calling `start`) print as one line.

| Command | Who | Behavior |
|---|---|---|
| `create <name> [--size T]` | manager | owner guard; apply volume root, then instance root; wait for SSM online and bootstrap marker; write SSH entry; push `guest_keys`. Re-run with new `--size` to resize. |
| `destroy <name> [--volume] [--force]` | manager | owner guard (unless `--force`); destroy instance root; with `--volume`, also the volume root after typed confirmation. |
| `stop <name>` / `start <name>` | manager | boto3 stop/start on any box; wait for state. |
| `sync <name>` | manager | section 9.2. |
| `list [--account A]` | anyone | every barn box in every account file's account, or just A (`tag:barn` only): account, owner (yours marked), state, type, uptime, rough hourly cost. |
| `connect <name>` | anyone | find the box by `barn:name`, write its SSH entry with the caller's key and profile; say if it is stopped. |
| `ssh <name>` | anyone | `connect`, then exec `ssh <name>` (exits 1 if the box is not running). |
| `doctor <name>` | anyone | section 12. |
| `hold <name> <duration>` | anyone | writes `~/.barn/hold-until` on the box over SSH. |

### 11.1 Account file (`~/.config/barn/accounts/<name>.toml`)

Hand-written or handed out by whoever owns the account. Unknown or missing
keys are errors.

```toml
region = "us-east-1"
state_bucket = "..."
execution_instance_profile = "..."
security_group_id = "sg-..."
subnet_ids = ["subnet-..."]
ami_ssm_parameter = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"  # optional, default shown
manager_role_arn = ""             # optional: the manager role; doctor simulates its policy
kms_key_id = ""                   # optional
[extra_tags]
```

### 11.2 Local settings (`~/.config/barn/config.toml`)

```toml
default_account = "personal"      # accounts/<account>.toml when a command has no --account
user = "zach"                     # barn:owner of boxes you create; default $USER
ssh_public_key = "~/.ssh/id_ed25519.pub"
default_size = "m7i.xlarge"
passwordless_sudo = true
idle_hours = 4
volume_gb = 100
shared_home = ""                  # optional team defaults dir, synced first
guest_keys = []                   # optional: public keys authorized on boxes you create

[aws_profiles]                    # one AWS CLI profile per account file
personal = "..."
```

## 12. `doctor <name>`

Each check prints pass, warn, or fail; exit non-zero on any fail.

- AWS credentials valid (via `aws_profile`).
- IAM policy simulation of section 6.2 actions on the account file's
  `manager_role_arn` (catches a silently removed policy); skipped with a
  warning when unset, and with a note when the caller's assumed role is not
  the manager role (guests). A role that allows shell sessions is a warning.
- Instance running; SSM reports it online.
- Bootstrap marker present; if not, show the console output tail.
- SSM agent snap service running; refresh hold present (warn if missing).
- From the box: reach `cli.kiro.dev`, `claude.ai`, `herdr.dev`, `astral.sh`, and
  SSM endpoints; GitLab reachability is warn-only.
- On PATH: `kiro-cli`, `claude`, `herdr`, `uv`, `git`, `docker`, `nvim`.
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
- Managed-account self-serve depends entirely on the platforms team providing the
  account prerequisites.
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
  `KIRO_API_KEY`, in `home.local/.barn/env`. Both sign in.
  `claude auth status` and `kiro-cli whoami` are the doctor checks.

### Deviations and additions

- `ssm:StartSession` on instances uses `ssm:resourceTag/barn`
  (StartSession does not match on `aws:ResourceTag`), and
  `ssm:SessionDocumentAccessCheck` uses `BoolIfExists`. Verified: the
  SSH document works; the default shell document, port forwarding, and
  `ssm:SendCommand` are denied.
- Developer policy adds `ssm:DescribeInstanceInformation` (wait for SSM online).
  `RunInstances` on network interfaces is unconditioned (`aws_instance` cannot
  tag the ENI at launch).
- Account file adds `developer_policy_role_arn` (the role `doctor` simulates,
  in both role modes).
- Local settings add `volume_gb` (default 100). `BARN_SETTINGS`
  overrides the settings file path (one checkout acting as several developers).
- The SSH AWS profile is `barn-<account>-<user>`, not
  `barn-<account>`, so two developers can share a laptop.
- SSH entries use `HostKeyAlias <user>-<env>` so the stable host key is checked
  across recreates (the HostName instance ID changes).
- The instance ignores AMI changes (a new Ubuntu release must not replace the
  box). Changing user data replaces the box (`user_data_replace_on_change`).
  `idle_hours` is written to `/etc/barn/idle.conf`.
- `.bashrc` line 1 (before Ubuntu's non-interactive return) puts `~/.local/bin`
  on PATH and sources `~/.barn/env`, so `ssh box cmd` and
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
  assumes the developer role, so it runs with the scoped policy only.
- Box size m7i.xlarge (4 vCPU, 16 GiB), the default.
- Docker's data-root moved to the home volume
  (`/home/ubuntu/.barn/docker`, root-owned 0710), with
  `containerd-snapshotter` off so images stay under data-root, and a
  `RequiresMountsFor=/home/ubuntu` drop-in on `docker.service`. Project compose
  stacks (niflow-canvas: NiFi cluster, Airflow, Postgres) keep their images and
  volumes across `destroy`/`create`. `doctor` checks the data-root.

## 20. Platform layer removed; installable CLI (2026-10-29)

- `terraform/platform`, `terraform/modules/developer-policy` and
  `terraform/test-users` moved out of this repo to
  `~/source/barn-platform-minimal` (demo only), with the generated
  account files. This repo documents the prerequisites instead
  (docs/account-prerequisites.md; the policy is rendered from the old module
  into docs/developer-policy.json).
- The CLI no longer assumes the developer role or writes AWS CLI profiles. Role
  modes are gone; developers configure role assumption in their AWS profile.
  `developer_policy_role_arn` is gone; `developer_role_arn` now only names the
  role `doctor` simulates. "Policy denies shell sessions" is a warning, not a
  failure, so broader existing roles pass.
- Installable with `uv tool install`: the volume and instance roots and
  `bootstrap/` are package data under `src/barn/`, copied into
  `~/.config/barn/` on each run. Settings, account files and the
  synced home overlay live there too; no repo checkout is needed.
- Sync: the committed `home/` + gitignored `home.local/` pair became optional
  `shared_home` (credential-guarded) + `~/.config/barn/home/`.

## 21. Renamed to barn (2026-10-29)

- Repo and GitLab project `zach/agent-platform` -> `zach/barn`; Python package
  `barn`, console script `barn`.
- Renamed throughout, including the wire identifiers: AWS tags (`barn`,
  `barn:owner`, `barn:env`, Name `barn-<user>-<env>`) and the developer
  policy's tag conditions, on-box paths (`~/.barn/`, `/var/lib/barn`,
  `/etc/barn`, `/usr/local/lib/barn`), local paths (`~/.config/barn/`,
  `BARN_HOME`, `BARN_SETTINGS`, `~/.ssh/barn.conf`). State keys are unchanged.
- No migration: nothing depended on the old names. Environments created before
  the rename are not found by barn; destroy them with the old version.
- The demo platform (`~/source/barn-platform-minimal`) was renamed too: its
  resources are named `barn-*` (`barn-attach-*` for the attach stack), so
  re-applying a stack created before the rename recreates its resources. Its
  account files were dropped; regenerate them with `terraform output`.


## 22. End-to-end verification of the developer policy (2026-10-29)

- Sandbox account 123456789012: `barn-platform-minimal` applied with an IAM
  user (`barn-dev`) that can only assume a stand-in existing role, and the
  developer policy attached to that role (`developer_role_mode = "attach"`).
  The whole lifecycle passed as that user; CloudTrail event history gave the
  exact API calls, and deny experiments proved the authorization-only ones.
  Method and results: docs/account-prerequisites.md ("How this policy was
  verified"); tooling in `barn-platform-minimal/e2e/`.
- Policy changes from it and from an independent review: exact Describe list
  (no `ec2:Describe*`), no `sts:GetCallerIdentity` or session-management grants,
  `ModifyInstanceAttribute` only for `InstanceType` and
  `InstanceInitiatedShutdownBehavior` (user data could run code as root on any
  box), scoped `CreateTags`/`DeleteTags` for changing `extra_tags`,
  `ModifyVolume` for growing `volume_gb`, ENIs pinned to the platform subnets,
  and a correct ARN for custom AMI parameters.
- barn changes: first boot and `create` grow the home filesystem to the
  volume; `doctor` simulates the new permissions (and two that must be denied)
  in the role's partition with standard retries; narrow terminals drop `list`'s
  instance column; terraform init errors print as a block.
- Known, documented, not fixed: developers in one account can take over each
  other's boxes and volumes (no owner binding), `AWS-StartSSHSession` forwards
  any port, no proxy support, no cost limits.

## 23. Managers and guests; full box names (2026-11-02)

Interview 2026-10-30 to 2026-11-02. Goal: several developers can connect to
one person's box (to watch or steer its agents, or upload files), while only
managers create and destroy boxes. Developers fully trust each other: there is
**no security separation between people**, only between the two roles.

### 23.1 Decisions

| # | Decision |
|---|---|
| 1 | A box belongs to one person (its creator); others *connect* to it. It is still a single developer's workspace, not a shared one. |
| 2 | Two IAM roles: **`barn-manager`** (today's developer policy: create, destroy, start, stop, resize, state, connect) and **`barn-guest`** (find boxes and open the SSH tunnel, nothing else). |
| 3 | People are IAM users with their own access keys. Their AWS CLI profiles assume a role (`role_arn` + `source_profile`). barn still never assumes roles itself. |
| 4 | No owner binding in IAM: any manager can manage any box. Accepted (full trust). barn adds an *accident* guard, not a security one (23.4). |
| 5 | Sandbox users: `zach` and `alice` may assume both roles; `foo` only `barn-guest`. |
| 6 | Everyone logs in as `ubuntu` (same user, sudo, same herdr session). Guests can read the owner's tokens and reach the instance role. Accepted. |
| 7 | Who can SSH in is decided by keys: the owner's key (first boot) plus `guest_keys` from the owner's `config.toml` (pushed over SSH). |
| 8 | Boxes have **full names chosen by the user** (`barn create zach-dev`), no `<user>-` prefix, so connecting to someone else's box uses the same name everywhere. |
| 9 | Guests use the same `barn init` and account file as managers. |
| 10 | A guest cannot start a stopped box (ask a manager). Guests can `hold` it and can shut it down from inside (sudo). |
| 11 | Naming is "manager" and "guest" everywhere; `developer` is renamed. |
| 12 | Existing environments are not migrated by barn, except the sandbox volume `barn-zach-a2n`, which is migrated by hand to the box name `zach-a2n` (23.8). |

### 23.2 Names, tags and state

- **Box name:** `^[a-z][a-z0-9-]{0,38}[a-z0-9]$` (2 to 40 characters, no
  trailing dash). Unique per account. It is the SSH alias, the box's hostname,
  and the `Name` tag `barn-<name>`.
- **Tags:** `barn = true`, `barn:name = <name>`, `barn:owner = <user>` (who
  created it; informational plus the guard in 23.4), plus `extra_tags`.
  `barn:env` is gone.
- **State:** `env/<name>/volume.tfstate` and `env/<name>/instance.tfstate`. The
  `env/` prefix stays so the manager policy's S3 statements are unchanged.
  Local `TF_DATA_DIR`: `<home>/state/<name>/<root>`; logs alongside.
- **Terraform variables:** `name` replaces `env`; `user` stays (owner tag).
- **`user`** in `config.toml` stays optional (default `$USER`) and is used only
  for `barn:owner` and the guard. It no longer appears in any name.
- **Old boxes** (`barn:env` tag, state at `env/<user>/<env>/`) are not found.
  The only one with data is sandbox volume `barn-zach-a2n`, migrated by hand
  (23.8).

### 23.3 Policies

- `docs/developer-policy.json` → **`docs/manager-policy.json`**, unchanged
  except the `Doctor` statement's placeholder becomes `MANAGER_ROLE_ARN`.
- New **`docs/guest-policy.json`**:
  - `ec2:DescribeInstances`, `ec2:DescribeVolumes`,
    `ssm:DescribeInstanceInformation` on `*` (`list`, `connect`, `ssh`);
  - the manager policy's two `StartSshSession*` statements, verbatim.
- Account file: `developer_role_arn` → **`manager_role_arn`** (`doctor` only).
  Unknown keys are already an error, so an old file fails with the key named.
- `docs/account-prerequisites.md`: two roles, a "Managers and guests" section,
  both policies' tables, and the "does not protect" list restated for two roles
  (any manager can manage any box; a guest has sudo on boxes it can reach, so
  it can read the owner's tokens and use the instance role; the tunnel still
  forwards any port).

### 23.4 CLI

The `<env>` argument becomes `<name>` everywhere. Commands, by who can use them:

| Command | Who | Change |
|---|---|---|
| `create <name>` | manager | Refuses if a box or volume with that name has a different `barn:owner` ("owned by alice"). Pushes `guest_keys` after first boot. |
| `destroy <name> [--volume]` | manager | Refuses on another owner's box unless `--force`. |
| `start` / `stop <name>` | manager | Work on any box (no owner check). |
| `sync <name>` | manager | Refuses on another owner's box (it would overwrite their tokens). Pushes `guest_keys` even when it refuses to copy files because agents are running. |
| `list` | anyone | **All** barn boxes in the account, with an owner column; your own marked. Filters by `tag:barn` only. |
| `connect <name>` | anyone | **New.** Finds the box by `barn:name`, writes its `~/.ssh/barn.conf` entry (with *your* `aws_profile` and key), says if it is stopped. Afterwards plain `ssh`, `scp`, `rsync` and `herdr --remote <name>` work. |
| `ssh <name>` | anyone | Does what `connect` does (refreshing a stale instance ID), then execs `ssh <name>`. |
| `hold <name> <d>` | anyone | Unchanged (over SSH). |
| `doctor <name>` | anyone | The IAM simulation runs only if the caller's assumed role is `manager_role_arn`; otherwise it is skipped with a note. On-box checks run for everyone. |

Settings (`config.toml`):

```toml
guest_keys = [                    # optional; authorized on every box you create
  "ssh-ed25519 AAAA... alice",
]
```

**`authorized_keys` layout** (on the home volume, so it survives stop/start):

```
# >>> barn owner >>>       written by first boot from ssh_public_key
# <<< barn owner <<<
# >>> barn guests >>>      rewritten wholesale by create and sync from guest_keys
# <<< barn guests <<<
```

First boot replaces only the owner block and keeps everything else (today it
overwrites the file), so guests survive `destroy`/`create`, and a key someone
added by hand outside the blocks is kept. A file with no barn blocks at all is
the pre-23 layout (only the owner's key, written by the old first boot): first
boot replaces it wholesale. Removing a key from `guest_keys` and
running `sync` revokes it: the one place `sync` deletes something.

### 23.5 Demo platform (`~/source/barn-platform-minimal`)

- Policy module outputs `manager_policy_json` and `guest_policy_json`.
- `terraform/platform`: `developer_role_mode` → `role_mode` (`create` |
  `attach`).
  - **create:** roles `barn-manager` and `barn-guest`, trusting
    `manager_principal_arns` / `guest_principal_arns`; output
    `aws_profiles`, a ready-to-paste `~/.aws/config` snippet per role (with
    `role_session_name` set to the user name for readable CloudTrail; not
    enforced).
  - **attach:** `existing_manager_role_name` and an optional
    `existing_guest_role_name`.
  - Account file output uses `manager_role_arn`.
- `terraform/test-users` (sandbox): IAM users `alice` and `foo` with access
  keys, allowed to `sts:AssumeRole` the two roles. `zach` is the existing admin
  IAM user and is only referenced by ARN. `barn-dev`, `barn-ssm`, the stand-in
  existing role and `deny_actions` are removed (attach mode stays in the module,
  untested this round).
- `sandbox.tfvars`: `role_mode = "create"`; managers `zach`, `alice`; guests
  `zach`, `alice`, `foo`.
- `handoff/` and `e2e/`: regenerated for the new roles and names.

### 23.6 Implementation order

1. **Naming** (barn): names, tags, state keys, `TF_DATA_DIR`, alias = name,
   hostname, `list` shows all boxes, owner guard. Tests: name validation,
   guard, `barn.conf` entries.
2. **Guests on the box:** owner block in first boot; `guest_keys`; key push in
   `create` and `sync`. Tests: block rewrite (add, remove, hand-added keys kept).
3. **`connect`**, and `ssh` refreshing the entry.
4. **Policies and docs:** rename to manager, add the guest policy,
   `manager_role_arn`, `doctor` skip, README ("Guests" section, new names),
   `account-prerequisites.md`.
5. **Demo platform** (23.5).
6. **Migrate `barn-zach-a2n`** (23.8).
7. **Sandbox verification** (23.7). Every `terraform apply`/`destroy` in the
   sandbox is confirmed with the user first.

### 23.7 Verification (sandbox account 123456789012)

- Before: migrate `barn-zach-a2n` (23.8).
- Apply `test-users` and `platform`; set up profiles for `zach`, `alice`
  (manager and guest) and `foo` (guest).
- As `zach` (manager): `create zach-dev` with `alice`'s and `foo`'s keys in
  `guest_keys`.
- As `foo` (guest), these work: `list`, `connect zach-dev`, `ssh`, an `rsync`
  upload, `hold`, `herdr --remote zach-dev`, and herdr attached at the same time
  as `zach` (**unknown whether herdr allows two clients at once**; if not,
  document "take turns").
- As `foo`, these are denied: `start`, `stop`, `create`, `destroy`, reading
  `env/` state, a shell-document session.
- As `alice` (manager): `create alice-dev`; `zach` connects to it as a guest.
  `zach`'s `create alice-dev` and `sync alice-dev` are refused by the guard;
  `stop`/`start alice-dev` work.
- Remove `foo`'s key from `guest_keys`, `sync`: `foo` can no longer SSH in.
- CloudTrail event history for `barn-guest`: exactly the guest policy's
  actions, nothing denied in normal use.
- `destroy --volume` `alice-dev` and `zach-dev` at the end. `zach-a2n` stays.

### 23.8 Migrating `barn-zach-a2n` (one-off, sandbox)

The volume `vol-0ccccccccccccc001` (us-east-1a, 100 GB, available) holds data
that must be kept. It becomes the box **`zach-a2n`**: the same string as its
old SSH alias, hostname and `Name` tag (`barn-zach-a2n`), so the host keys saved
on the volume still match the `known_hosts` entry for `zach-a2n` and nothing
changes for SSH. A one-off runbook, not a barn command: retagging `barn:*` is
denied to managers by design, so steps 1 and 3 use the admin profile `zach`.
Confirm each step that changes AWS with the user.

1. **Retag** (admin): add `barn:name = zach-a2n`, delete `barn:env`. `Name`,
   `barn` and `barn:owner = zach` already match the new layout.
2. **Move state:** copy `env/zach/a2n/volume.tfstate` to
   `env/zach-a2n/volume.tfstate` (resource address `aws_ebs_volume.home` is
   unchanged). The old `instance.tfstate` is empty (the box was destroyed); it
   is not copied.
3. **Clean up** (admin): delete `env/zach/a2n/*`, and the empty (181–182 byte)
   leftovers `env/zach/{e2e,hn,nv,probe,ss}/*`. Their local data dirs
   (`~/.config/barn/state/zach-*`) can go too.
4. **Check before applying:** with the new barn, `terraform plan` on the
   volume root for `zach-a2n` must show **no changes**. A diff there means a
   tag or variable mismatch; fix it, never apply a replacement.
5. **`barn create zach-a2n`** (as `zach`'s manager profile, `user = zach`):
   volume root no-op, new instance, attach, first boot. First boot sees the
   pre-23 `authorized_keys` and rewrites it into blocks; host keys come from
   `~/.barn/hostkeys`.
6. **Verify:** `ssh zach-a2n` connects without a host-key warning, the files
   are there, `barn doctor zach-a2n` is green.

Do this after implementation steps 1–2 (23.6) and the sandbox platform apply,
since the new roles are needed for step 5.

### 23.9 Handoff notes (read before starting section 23)

**Facts collected 2026-11-02 (sandbox, read-only):**
- IAM users: `zach` (admin) exists; `barn-dev` and `barn-ssm` exist (to be
  removed); `alice` and `foo` do **not** exist yet. Roles: `barn-execution`,
  `barn-test-existing-developer` (to be removed).
- State bucket `<state-bucket>`: `env/zach/a2n/volume.tfstate`
  holds `zach-a2n`'s volume; everything else under `env/zach/` is empty
  leftovers (23.8 step 3).
- `barn-platform-minimal` platform state is local
  (`terraform/platform/terraform.tfstate`); apply with `AWS_PROFILE=zach`.

**Hard stop on the platform re-apply:** switching `sandbox.tfvars` to
`role_mode = "create"` must change **only IAM** (new roles and policies; the
stand-in role and its attachment removed). If the plan replaces or destroys the
VPC, subnets, security group, execution role/instance profile or the state
bucket, stop and fix the Terraform (`moved` blocks, unchanged resource
addresses). `zach-a2n`'s volume is pinned to its subnet's AZ (us-east-1a), and
the bucket holds all state.

**Small details the sections above leave implicit:**
- The name `workspaces` is reserved (it would collide with the backend's
  `workspace_key_prefix = env/workspaces`); validation rejects it.
- `doctor`'s "caller is the manager role" test: the role name in the
  `sts get-caller-identity` ARN (`assumed-role/<role>/<session>`) equals the
  last path segment of `manager_role_arn`.
- Rename everywhere `env` reaches the user: CLI help, errors, the idle-check
  `wall` message (`barn hold <name> <duration>`), README, templates.
- Templates: `templates/config.toml` gets a commented `guest_keys` example;
  `templates/account.toml` gets `manager_role_arn`. The platform's
  `account.toml.tftpl` and `handoff/accounts/sandbox.toml` likewise.
- `barn-platform-minimal/e2e/` scripts (`run.sh`, `sniff.py`, `deny.sh`)
  refer to `barn-dev` and the stand-in role; update or drop them along with
  `deny_actions`.
- Migration step 4 (23.8): barn has no plan command. Run `terraform plan
  -detailed-exitcode` on the volume root with exactly the backend config from
  `Terraform.init` (key `env/zach-a2n/volume.tfstate`) and the variables
  `volume_vars` would pass (subnet = the volume's recorded `subnet_id` output).
  Exit code 0 is required.
- `scripts/check.sh` (pytest, terraform fmt/validate, shellcheck) must pass
  before handing back.

**Testing several people on one machine:**
- One `BARN_HOME` per person, e.g.
  `barn-platform-minimal/e2e/people/{zach,alice,foo}/` (gitignore their
  `state/`, `terraform/`, `bootstrap/`, and any key files), each with its own
  `config.toml` (`user`, `aws_profile`, `ssh_public_key`) and the sandbox
  account file.
- One SSH key pair per person (`ssh-keygen -t ed25519 -N ''`) inside that
  person's directory. `alice` and `foo` public keys go into zach's
  `guest_keys`; zach's into alice's.
- AWS profiles: source profiles `zach` (exists), `alice`, `foo` (keys from
  `terraform output -json access_keys`), and role profiles
  `barn-zach-manager`, `barn-zach-guest`, `barn-alice-manager`,
  `barn-alice-guest`, `barn-foo-guest` (from the platform's `aws_profiles`
  output). Persons test the guest path with their `-guest` profile.
- `~/.ssh/barn.conf` is shared by every `BARN_HOME` on the machine: an alias's
  entry carries whoever last ran `connect`/`ssh`/`create`. Run each person's
  steps in sequence and re-run `barn connect <name>` as that person before
  plain `ssh`/`rsync`/`herdr --remote`. For the two-herdr-clients test, two
  terminals as the same person are enough.

### 23.10 Implementation and verification (2026-11-02)

Implemented as specified in 23.1–23.9, with these additions:
- `bootstrap/authorized_keys.py` is the one implementation of the owner/guests
  blocks: first boot installs and runs it, and `create`/`sync` send it over SSH
  to rewrite the guests block. Writing guests onto a file with no blocks wraps
  the old lines in an owner block (they are the pre-23 owner key). An orphaned
  begin marker is treated as plain text.
- `plugin-cache` is reserved as well as `workspaces` (`state/plugin-cache/` is
  the provider cache).
- `sync`, `ssh` and `doctor` refresh the box's `barn.conf` entry first; `sync`
  needs a running box. `ssh` refuses only `stopped`/`stopping`. `~/.ssh/barn.conf`
  writes are locked and atomic (several people/boxes on one laptop).
- AWS errors print as one line (e.g. a guest's `barn stop`).
- `scripts/check.sh` falls back to `uvx --from shellcheck-py shellcheck`.
- Test homes use `t3.medium` and `volume_gb = 25`.

Sandbox (123456789012), all as specified:
- IAM: `test-users` then `platform` (6 add, 2 destroy, IAM only) then
  `test-users` again. **Deviation:** `barn-dev` and `barn-ssm` were removed from
  `test-users` state with `terraform state rm`, not destroyed (user: keep
  existing access keys alive); later the same day, at the user's request, both
  users, their access keys and inline policies, and their local AWS profiles
  were deleted.
  Local state backups: `terraform.tfstate.pre-23.bak` in both roots.
- Migration (23.8): retag, state copy, cleanup, step-4 plan exit 0,
  `barn create zach-a2n`: volume no-op, files intact, host key is the one
  saved 2026-10-29, `authorized_keys` converted to blocks, `doctor` 51/51 as
  `barn-manager`.
- 23.7: as `foo` (guest) `list`, `connect`, `ssh`, `rsync` upload, `hold`,
  `doctor` (simulation skipped with a note) work; `start`, `stop`, `create`,
  `destroy`, state reads, shell and port-forward documents, `SendCommand`,
  console output are denied. **Two herdr clients at once work** (both see the
  same panes and can type). `zach` as guest connects to `alice-dev`;
  `zach`'s `create`/`sync`/`destroy alice-dev` are refused by the guard;
  `stop`/`start alice-dev` work; a guest's `ssh` to a stopped box exits 1 with
  "ask a manager". Removing `foo` from `guest_keys` + `sync` (while a fake
  `claude` ran) revoked `foo`; `alice` and a hand-added key stayed, also
  across `destroy`/`create` (strict host key checking passed). CloudTrail for
  `barn-guest`: only DescribeInstances, DescribeVolumes,
  DescribeInstanceInformation, StartSession, GetCallerIdentity in normal use;
  the only denials were the deliberate negative tests. Both test boxes
  destroyed with `--volume`; `zach-a2n` kept (running).

## 24. Laptop migration (done 2026-11-02)

The user's barn setup moved from the devbox to their laptop (Arch Linux,
`obsidian`; not WSL as planned). The one-off instructions
(`MIGRATION-laptop.md`, never committed) were deleted afterwards.

- barn reinstalled from `main`. AWS profiles `barn-zach-manager` and
  `barn-zach-guest` (source profile `zach`); the stale `barn-dev`,
  `barn-dev-user` profiles removed. Backups: `~/.aws/*.pre-23.*`,
  `~/.config/barn.pre-23`.
- `config.toml`: account `sandbox`, `aws_profile = "barn-zach-manager"`.
  `accounts/sandbox.toml` with `manager_role_arn`; the pre-23 account files
  and `state/zach-a2n` deleted.
- **Deviation:** the laptop's SSH key is its own (`SHA256:G59G…`), not the
  devbox key that owns `zach-a2n`. At the user's request the laptop key was
  appended by hand to `zach-a2n`'s `authorized_keys`, outside barn's blocks
  (so `create` and `sync` keep it), via the devbox. `barn create zach-a2n`
  from the laptop would therefore replace the instance (new owner key in user
  data); the volume is kept.
- Verified from the laptop: `barn list`, `connect`, `ssh zach-a2n` with the
  saved host key, `doctor` 51/51 as `barn-manager`. `barn sync` not run (the
  laptop's `home/` has no tokens; the box's work).
- Same day: test user `foo` removed (devbox, `barn-platform-minimal`: out of
  `guest_principal_arns` and `test-users`, platform applied first). Its local
  profiles and e2e home on the devbox remain.

## 25. Multiple accounts (2026-11-02)

One person can use barn in several AWS accounts at once.

- `config.toml`: `account` is replaced by **`default_account`**, and
  `aws_profile` by an **`[aws_profiles]`** table, `<account> = "<profile>"`,
  since each AWS account needs its own profile. The old keys are an error
  that names the replacement.
- Every account with a file in `accounts/` is usable. Box commands (`create`,
  `destroy`, `start`, `stop`, `sync`, `connect`, `ssh`, `doctor`) take
  `--account <name>` (long form only: `ssh` passes other options through, and
  `-a`/`-A` are ssh's); without it they use `default_account`. `hold` goes
  over the SSH alias and needs no account.
- `list` reads every account (in parallel) and adds an ACCOUNT column, sorted
  by account then name; `--account` limits it to one. An account that fails
  (no profile, expired SSO token, AWS error) is warned about and skipped; it is
  an error only when it was asked for by name or every account failed.
- Terraform data dirs move to `state/<account>/<name>/<root>` (box names are
  unique per account only). State in S3 is unchanged; the old
  `state/<name>/` dirs are unused and can be deleted.
- `~/.ssh/barn.conf` still has one entry per box name, so the same name in two
  accounts shares an SSH alias: plain `ssh` reaches whichever was connected
  last. Not prevented; documented.
- `barn init --account-file X.toml` with an existing `config.toml` adds the
  account file (named after the file, not `default_account`) and lists the
  missing `[aws_profiles]` entry as a to-do. Account names: letters, digits,
  `-` and `_` (a TOML bare key and a directory name), not `plugin-cache`.

## 26. Versions and automatic migrations (2026-11-02)

- Semantic versioning from here: `pyproject.toml` is the source, releases are
  tags `vX.Y.Z`, changes go in `CHANGELOG.md`. Retroactively, `v0.1.0` is the
  last commit before multiple accounts (87e106f); multiple accounts plus this
  are 0.2.0. Before 1.0.0, breaking changes bump the minor version.
- `config.toml` gets `config_version` (the barn version whose format it is
  in; `barn init` writes the installed version). Missing means 0.1.0.
- `migrate.py`: migrations registered by version. `load_settings` (every
  command) and `barn init` run those above `config_version` and up to the
  installed version, in order; then back up the old file as
  `config.toml.<old>.bak`, stamp the installed version, and print what
  changed. Nothing is written when no migration is pending, so a plain upgrade
  never touches the file. A newer `config_version` than the installed barn is
  an error (no downgrades).
- Migrations edit the file as text (rename/remove top-level keys, add table
  entries, set a key) and re-parse it after each step, so comments survive
  and a bad result never reaches disk. They must be idempotent: a file may be
  partly converted by hand (this machine's config was, before 0.2.0).
- Migration 0.2.0: `account` → `default_account`, `aws_profile` →
  `[aws_profiles] <account> = ...`, `state/<name>/` → `state/<account>/<name>/`
  (only dirs with terraform files; `plugin-cache` stays). Verified on copies
  of the platform repo's `handoff/config.toml` and an e2e home.
- Account files are not versioned: barn does not write them and their format
  has not changed. If it does, they get the same treatment.
- No new dependency: tomlkit has no public key rename, and flat configs need
  only line edits checked with `tomllib`.

## 27. Guest keys per account (0.3.0, 2026-11-02)

- `guest_keys` (a list) becomes a `[guest_keys]` table, `<account> = [keys]`,
  like `[aws_profiles]`. `create` and `sync` push the box's account's list
  (`Context.guest_keys`); an account without one gets an empty guests block,
  so moving to 0.3.0 and syncing a box in another account revokes guests
  there that the old global list had added.
- Migration 0.3.0 moves the old list, verbatim with its comments, under
  `default_account`. An empty list is dropped. The migrate text helpers now
  handle values spanning several lines (`pop_key` finds the end by parsing).
- A flat list in a file already at 0.3.0 is an error that shows the table form.
  `barn init` lists a `[guest_keys]` entry without an account file as an
  optional to-do (likely a typo).
- "For now": a per-account table in the user's `config.toml`. Per-box lists or
  keys in the shared account file were not wanted yet.

