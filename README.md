# agent-platform

Personal EC2 dev boxes for long-running, headless Claude Code and Kiro CLI
runs. Each box runs **herdr**: you start agents, detach, close your laptop,
and come back later. You reach the box with SSH tunnelled through AWS SSM.
Nothing listens on the network.

An account administrator sets up the account once (`terraform/platform`, see
its README). After that, every developer runs the CLI in this repo.

## Prerequisites (laptop / WSL)

- Terraform >= 1.10, AWS CLI v2, the Session Manager plugin, `rsync`, `ssh`
- [`uv`](https://docs.astral.sh/uv/)
- An AWS CLI profile for your own login (for example, an SSO profile)
- An SSH key pair (`ssh-keygen -t ed25519`)

## Setup

```bash
git clone <this repo> && cd agent-platform
uv sync
cp .agent-platform.local.toml.example .agent-platform.local.toml
$EDITOR .agent-platform.local.toml     # account, aws_profile, user, ssh key
```

`account` selects `accounts/<account>.toml`, which the administrator commits.
`user` is your name in AWS tags and SSH host names (`ssh zach-dev`). On the box
you log in as `ubuntu` (UID 1000, the same UID most container images use).

Put your tokens and personal config in `home.local/` (gitignored). See
[`home/README.md`](home/README.md). The short version is a
`home.local/.agent-platform/env` file containing
`export CLAUDE_CODE_OAUTH_TOKEN=...` and `export KIRO_API_KEY=...`.

## Use

```bash
uv run agent-platform create dev              # volume + box; ~5 min first boot
uv run agent-platform sync dev                # push home/ then home.local/
uv run agent-platform doctor dev              # everything green?
uv run agent-platform ssh dev                 # or plain: ssh <user>-dev
herdr                                         # on the box: start/attach the session
```

Inside herdr, run `claude` or `kiro-cli` in panes. Detach with `ctrl+b q` and
reattach later with `herdr`. Don't run herdr inside tmux.

| Command | What it does |
|---|---|
| `create <env> [--size TYPE]` | Creates or updates the environment. Re-run it with a new `--size` to resize in place. |
| `destroy <env>` | Deletes the box but **keeps the home volume**. `create` brings it back with your files and the same SSH host key. |
| `destroy <env> --volume` | Also deletes the home volume, after you type the environment name to confirm. |
| `stop` / `start <env>` | Stops or starts the box. A stopped box costs only its volumes. |
| `list` | Your environments: state, type, uptime, rough cost. |
| `ssh <env> [args]` | Runs `ssh <user>-<env>`. The CLI keeps `~/.ssh/agent-platform.conf` up to date. |
| `sync <env>` | Copies `home/` and `home.local/` to the box. The repo wins, nothing is deleted, and it refuses while agents are running. |
| `doctor <env>` | Checks AWS, IAM, SSM, bootstrap, tools, network, linger, home mount and sign-in. |
| `hold <env> <30m\|8h\|2d>` | Keeps the box from idle-stopping for that long. |

You can have several environments (`create dev`, `create big --size m7i.2xlarge`).
Each has its own volume, box and state.

## Idle stop

Every 5 minutes, a timer on the box checks whether it is in use:

- a herdr agent is working, or has been blocked for less than 24 h
- you have typed in an SSH session in the last 30 min
- a herdr pane is running something other than its shell or an agent
- a `hold` is active

After `idle_hours` (default 4) with none of these, it broadcasts a 15-minute
warning and then **stops** the box. It never terminates it, and the home volume
is kept. If the herdr query fails, the box counts as in use. Run `start` to
bring it back; herdr restores its layout.

## What lives where

- `~` on the box is a separate encrypted EBS volume, so it survives
  `destroy`/`create`. There are **no backups**: push your work.
- Tools (`claude`, `kiro-cli`, `herdr`, `uv`) are installed unpinned into
  `~/.local` on first boot of each box. apt packages and the Docker engine
  live on the disposable root disk.
- Docker's data (images, containers, named volumes) lives on the home volume
  at `~/.agent-platform/docker`, so a project's compose stack comes back after
  `destroy`/`create` without re-pulling or re-initializing.
- Reach services running on the box with SSH port forwards, for example
  `agent-platform ssh dev -N -L 8080:localhost:8080`.
- Agents use the box's own IAM role, never your credentials.

## Development

```bash
scripts/check.sh      # pytest, terraform fmt/validate, shellcheck
```

Layout: `src/agent_platform/` (CLI), `terraform/{platform,modules/developer-policy,volume,instance}`,
`bootstrap/` (first-boot script and idle check), `accounts/` (account files),
and `terraform/test-users/` (test-account IAM users only). The design is in `PLAN.md`.
