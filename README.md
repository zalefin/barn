# barn

EC2 dev boxes for long-running, headless Claude Code and Kiro CLI
runs, in one or more AWS accounts. Each box runs **herdr**: you start agents, detach, close your laptop,
and come back later. You reach the box with SSH tunnelled through AWS SSM.
Nothing listens on the network.

A box belongs to the person who created it, and others can connect to it to
watch or steer its agents or upload files. **Managers** create, destroy, start
and stop boxes; **guests** can only connect ([Guests](#guests)).

The CLI manages only home volumes and EC2 instances. It expects the AWS
account to already have a network, security group, instance profile, state
bucket and the manager and guest roles:
**[docs/account-prerequisites.md](docs/account-prerequisites.md)** lists exactly
what is needed and how to point the CLI at it;
**[docs/onboarding.md](docs/onboarding.md)** is the order of work for setting
up a new account and adding people to it.

## Prerequisites (laptop / WSL)

- Terraform >= 1.10, AWS CLI v2, the Session Manager plugin, `rsync`, `ssh`
- [`uv`](https://docs.astral.sh/uv/)
- An AWS CLI profile that assumes the `barn-manager` or `barn-guest` role (for example, an SSO profile)
- An SSH key pair (`ssh-keygen -t ed25519`)

## Install

```bash
uv tool install git+<this repo>          # or a release: git+<this repo>@v0.2.0; from a clone: uv tool install .
uv tool upgrade barn                     # later; barn --version shows what you have
```

An upgrade that changes the config format migrates `config.toml` on its next
run ([Versions](#versions)).

## Setup

Everything local lives in `~/.config/barn/` (set `BARN_HOME` to move it).
`barn init` creates it, checks your tools, and lists what is left to edit:

```bash
barn init                                                   # templates with CHANGE-ME placeholders
barn init --account-file sandbox.toml --aws-profile my-dev   # or start from your admin's account file
barn init --account-file work.toml                           # later: add another account
```

It never overwrites a file, so run it again after editing to see what is
left; other commands refuse to run while a `CHANGE-ME` remains.

Each account you use has a file `accounts/<account>.toml`, which says where
the account's network, roles and state bucket are
([docs](docs/account-prerequisites.md)), and an AWS CLI profile under
`[aws_profiles]` in `config.toml`:

```toml
default_account = "sandbox"   # used when a command has no --account

[aws_profiles]
sandbox = "barn-zach-manager"
work = "barn-zach-work"
```

The profile is used as-is by boto3, Terraform and SSH; put the role in the
profile (`role_arn` + `source_profile`, or SSO). Every box command takes
`--account <account>`, and `barn list` shows the boxes of every account.

`user` (default `$USER`) goes into the `barn:owner` tag of boxes you create. On the box everyone logs in as `ubuntu`
(UID 1000, the same UID most container images use).

Put your tokens and personal config in `~/.config/barn/home/` (see
[Sync](#sync)). The short version is a `home/.barn/env` file
containing `export CLAUDE_CODE_OAUTH_TOKEN=...` and `export KIRO_API_KEY=...`.

## Use

Box names are yours to choose (2–40 lowercase letters, digits and dashes),
unique in the account, and the same for everyone: the name is the SSH alias,
the box's hostname and its `barn:name` tag. Two accounts may each have a box
of the same name, but `~/.ssh/barn.conf` holds one entry per name: plain `ssh`
reaches whichever one you last connected to. A habit like `<you>-<purpose>`
keeps them apart.

```bash
barn create zach-dev   # volume + box in default_account; ~5 min first boot (managers)
barn sync zach-dev     # push your home/ and guest_keys onto the box
barn doctor zach-dev   # everything green?
barn ssh zach-dev      # or plain: ssh zach-dev
herdr                  # on the box: start/attach the session
```

For a box in another account, add `--account <account>` to any of these
(`barn create zach-dev --account work`). Inside herdr, run `claude` or `kiro-cli` in panes. Detach with `ctrl+b q` and
reattach later with `herdr`. Don't run herdr inside tmux.

| Command | Who | What it does |
|---|---|---|
| `create <name> [--size TYPE]` | manager | Creates or updates the box. Re-run it with a new `--size` to resize in place. Refuses a name someone else's box uses. |
| `destroy <name>` | manager | Deletes the box but **keeps the home volume**. `create` brings it back with your files and the same SSH host key. Refuses someone else's box unless `--force`. |
| `destroy <name> --volume` | manager | Also deletes the home volume, after you type the name to confirm. |
| `stop` / `start <name>` | manager | Stops or starts any box. A stopped box costs only its volumes. |
| `sync <name>` | manager | Pushes the account's `guest_keys` and copies your home directory overlay to your box. Local files win, nothing is deleted, and files are skipped while agents are running. Refuses someone else's box. |
| `list [--account A]` | anyone | Every box in every account (or just one): account, owner (yours marked), state, type, uptime, rough cost. An account whose credentials fail is skipped with a warning. |
| `connect <name>` | anyone | Adds the box to `~/.ssh/barn.conf` with your key and profile, so plain `ssh`, `scp`, `rsync` and `herdr --remote` work. Says if it is stopped. |
| `ssh <name> [args]` | anyone | `connect`, then `ssh <name>`. |
| `doctor <name>` | anyone | Checks AWS, IAM (managers only), SSM, bootstrap, tools, network, linger, home mount and sign-in. |
| `hold <name> <30m\|8h\|2d>` | anyone | Keeps the box from idle-stopping for that long. |

You can have several boxes (`create zach-dev`, `create zach-big --size m7i.2xlarge`).
Each has its own volume, instance and state.

## Guests

To let someone onto the boxes you create in an account, add their public key
to that account's list under `[guest_keys]` in your `config.toml`:

```toml
[guest_keys]
sandbox = [
  "ssh-ed25519 AAAA... alice",
]
work = []
```

Keys are scoped per account: a box gets only its own account's list, and an
account with no list gets no guests. `create` and `sync` write these into a `# >>> barn guests >>>` block of the
box's `~/.ssh/authorized_keys`. Remove a key and run `barn sync <name>` to
revoke it (this works even while agents run). That removes only the key barn
wrote: a guest has sudo, so one who added their key elsewhere keeps it. Keys you add to
`authorized_keys` by hand, outside barn's blocks, are kept, also across
`destroy`/`create`.

The guest then runs, with their own `barn init` and the same account file:

```bash
barn list                  # find the box
barn connect zach-dev      # adds it to ~/.ssh/barn.conf
ssh zach-dev               # or scp, rsync -a ./data zach-dev:~/, herdr --remote zach-dev
```

Guests log in as the same `ubuntu` user, with sudo, and see the same herdr
session. Several people can be attached at once (`herdr` on the box or
`herdr --remote <name>` from a laptop); they see the same panes and can all
type, so agree on who drives. They cannot start a stopped box (ask a manager), but can `hold` it
and shut it down from inside. Nothing separates guests from the owner on the
box, so only add people you trust with your tokens.

`~/.ssh/barn.conf` holds one entry per box name, written by whoever on your
laptop last ran `create`, `start`, `connect`, `ssh` or `doctor` for it.

`barn --help` (or `barn <command> --help`) lists everything, and
`barn --install-completion` adds shell completion. Long steps show a spinner;
Terraform's output goes to a log file instead of the terminal (`create -v` and
`destroy -v` stream it), and a failure prints its last lines and the log path.

## Sync

`sync <name>` first pushes the box's account's `guest_keys` ([Guests](#guests)), then copies
these onto the box's home directory, in order:

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
- a `hold` is active (`barn hold <name> 8h`, from anyone who can connect)

After `idle_hours` (default 4) with none of these, it broadcasts a 15-minute
warning and then **stops** the box. It never terminates it, and the home volume
is kept. If the herdr query fails, the box counts as in use. Run `start` to
bring it back; herdr restores its layout.

## What lives where

On your laptop, in `~/.config/barn/`:

| Path | What |
|---|---|
| `config.toml` | Your settings, `default_account` and one AWS profile per account. |
| `accounts/<name>.toml` | Where an account's prerequisites are ([docs](docs/account-prerequisites.md)), one file per account. |
| `home/` | Synced onto your boxes. |
| `terraform/`, `bootstrap/` | Managed: refreshed from the installed package on every run, so Terraform always runs here. Don't edit. |
| `state/` | Terraform data dirs and logs (`<account>/<name>/<root>/apply.log`) per box, and the provider cache. The state itself is in the account's S3 bucket (`env/<name>/`). |

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
  `barn ssh zach-dev -N -L 8080:localhost:8080`.
- Agents use the box's own IAM role, never your credentials.

## Development

```bash
uv sync
uv run barn ...    # run from the checkout
scripts/check.sh   # pytest, terraform fmt/validate, shellcheck
```

### Versions

barn uses [semantic versioning](https://semver.org/); the version lives in
`pyproject.toml`, the changes in [CHANGELOG.md](CHANGELOG.md), and each release
is a git tag `vX.Y.Z`. Before 1.0.0, a breaking change (CLI, `config.toml`
format, home layout) bumps the minor version and anything else the patch.

`config.toml` records `config_version`, the barn version whose format it is
in. On every run barn compares it with the installed version and runs, in
order, each migration in `src/barn/migrate.py` registered above it; the old
file is kept as `config.toml.<old version>.bak`. A config from a newer barn is
an error, never downgraded. A file without `config_version` is from 0.1.0.

To release a change to the config format or the home layout:

1. Bump `version` in `pyproject.toml` and run `uv lock`.
2. Add a `@migration("<new version>")` function to `migrate.py` that rewrites
   old files (text edits keep comments; make it idempotent, since a file may
   already be partly converted by hand), plus a test in `tests/test_migrate.py`.
3. Update the template, docs and `CHANGELOG.md`; commit, tag `v<version>`, push the tag.

Layout: `src/barn/` (CLI), `src/barn/terraform/{volume,instance}`
(per-box roots, shipped in the package), `src/barn/bootstrap/`
(first-boot script, idle check, `authorized_keys` blocks), `docs/` (account
prerequisites and the manager and guest policies), `src/barn/templates/` (what `barn init` writes). The design is in [`docs/plans/PLAN.md`](docs/plans/PLAN.md).
