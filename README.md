# barn

Personal EC2 dev boxes for long-running, headless Claude Code and Kiro CLI
runs. Each box runs **herdr**: you start agents, detach, close your laptop,
and come back later. You reach the box with SSH tunnelled through AWS SSM.
Nothing listens on the network.

The CLI manages only home volumes and EC2 instances. It expects the AWS
account to already have a network, security group, instance profile, state
bucket and developer permissions:
**[docs/account-prerequisites.md](docs/account-prerequisites.md)** lists exactly
what is needed and how to point the CLI at it.

## Prerequisites (laptop / WSL)

- Terraform >= 1.10, AWS CLI v2, the Session Manager plugin, `rsync`, `ssh`
- [`uv`](https://docs.astral.sh/uv/)
- An AWS CLI profile with the developer permissions (for example, an SSO profile)
- An SSH key pair (`ssh-keygen -t ed25519`)

## Install

```bash
uv tool install git+<this repo>   # or, from a clone: uv tool install .
uv tool upgrade barn              # later
```

## Setup

Everything local lives in `~/.config/barn/` (set
`BARN_HOME` to move it):

```bash
mkdir -p ~/.config/barn/accounts
cp examples/config.toml ~/.config/barn/config.toml                # account, aws_profile, user, ssh key
cp examples/account.toml ~/.config/barn/accounts/<account>.toml   # or get it from your admin
```

`account` selects `accounts/<account>.toml`. `aws_profile` is used as-is by
boto3, Terraform and SSH. If your permissions come from a role, put the role in
the profile (`role_arn` + `source_profile`, or SSO). `user` is your name in AWS
tags and SSH host names (`ssh zach-dev`). On the box you log in as `ubuntu`
(UID 1000, the same UID most container images use).

Put your tokens and personal config in `~/.config/barn/home/` (see
[Sync](#sync)). The short version is a `home/.barn/env` file
containing `export CLAUDE_CODE_OAUTH_TOKEN=...` and `export KIRO_API_KEY=...`.

## Use

```bash
barn create dev   # volume + box; ~5 min first boot
barn sync dev     # push your home/ onto the box
barn doctor dev   # everything green?
barn ssh dev      # or plain: ssh <user>-dev
herdr             # on the box: start/attach the session
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
| `ssh <env> [args]` | Runs `ssh <user>-<env>`. The CLI keeps `~/.ssh/barn.conf` up to date. |
| `sync <env>` | Copies your home directory overlay to the box. Local files win, nothing is deleted, and it refuses while agents are running. |
| `doctor <env>` | Checks AWS, IAM, SSM, bootstrap, tools, network, linger, home mount and sign-in. |
| `hold <env> <30m\|8h\|2d>` | Keeps the box from idle-stopping for that long. |

You can have several environments (`create dev`, `create big --size m7i.2xlarge`).
Each has its own volume, box and state.

`barn --help` (or `barn <command> --help`) lists everything, and
`barn --install-completion` adds shell completion. Long steps show a spinner;
Terraform's output goes to a log file instead of the terminal (`create -v` and
`destroy -v` stream it), and a failure prints its last lines and the log path.

## Sync

`sync <env>` copies these onto the box's home directory, in order:

1. `shared_home` from `config.toml`, if set: team defaults, for example a
   directory in a shared git repo. `sync` refuses to run if it finds
   credential-looking files there (`.credentials.json`, `data.sqlite3`,
   `*token*`, `*.pem`).
2. `~/.config/barn/home/`: your personal config and tokens.

Paths are relative to the home directory: `home/.gitconfig` lands at
`~/.gitconfig`. Top-level `README.md` files are skipped. Nothing on the box is
deleted, and paths absent from these directories are never touched.

The simplest way to pass tokens is environment variables in
`home/.barn/env`. The box sources it at the top of `~/.bashrc`, so
interactive shells, herdr panes and `ssh box cmd` all see it:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`
export KIRO_API_KEY=...
```

Run `chmod 600` on it; rsync keeps the mode. Other things worth syncing:
`.claude/settings.json`, `.claude/CLAUDE.md`, `.claude/skills/`, `.kiro/`,
`.gitconfig`, `.config/herdr/config.toml`. Never sync the rest of
`~/.local/share/kiro-cli/` (about 200 MB of bundled runtimes).

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

On your laptop, in `~/.config/barn/`:

| Path | What |
|---|---|
| `config.toml` | Your settings. |
| `accounts/<name>.toml` | Where an account's prerequisites are ([docs](docs/account-prerequisites.md)). |
| `home/` | Synced onto your boxes. |
| `terraform/`, `bootstrap/` | Managed: refreshed from the installed package on every run, so Terraform always runs here. Don't edit. |
| `state/` | Terraform data dirs and logs (`<user>-<env>/<root>/apply.log`) per environment, and the provider cache. The state itself is in the account's S3 bucket. |

On the box:

- `~` is a separate encrypted EBS volume, so it survives
  `destroy`/`create`. There are **no backups**: push your work.
- Tools (`claude`, `kiro-cli`, `herdr`, `uv`) are installed unpinned into
  `~/.local` on first boot of each box. apt packages and the Docker engine
  live on the disposable root disk.
- Docker's data (images, containers, named volumes) lives on the home volume
  at `~/.barn/docker`, so a project's compose stack comes back after
  `destroy`/`create` without re-pulling or re-initializing.
- Reach services running on the box with SSH port forwards, for example
  `barn ssh dev -N -L 8080:localhost:8080`.
- Agents use the box's own IAM role, never your credentials.

## Development

```bash
uv sync
uv run barn ...    # run from the checkout
scripts/check.sh   # pytest, terraform fmt/validate, shellcheck
```

Layout: `src/barn/` (CLI), `src/barn/terraform/{volume,instance}`
(per-environment roots, shipped in the package), `src/barn/bootstrap/`
(first-boot script and idle check), `docs/` (account prerequisites and the
developer policy), `examples/`. The design is in `PLAN.md`.
