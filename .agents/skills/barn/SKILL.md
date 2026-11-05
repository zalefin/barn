---
name: barn
description: Use the barn CLI to create, reach and manage EC2 dev boxes for long-running headless agent runs (Claude Code, Kiro CLI under herdr), reached by SSH through SSM. Use when the user wants to set up barn, create/start/stop/destroy/resize a box, connect or ssh to one, sync tokens and dotfiles onto it, run or check agents on it, share it with guests, keep it from idle-stopping, or debug a box with doctor.
---

# Using barn

barn gives each developer EC2 dev boxes in one or more AWS accounts. Each box
runs **herdr**, a persistent terminal workspace where agents (`claude`,
`kiro-cli`) keep running after everyone detaches. Boxes are reached only by SSH
tunnelled through AWS SSM; nothing listens on the network.

barn manages only a per-box **home volume** and **instance** (Terraform, state
in the account's S3 bucket). The network, roles and state bucket are account
prerequisites (`docs/account-prerequisites.md`, `docs/onboarding.md` in the
barn repo); barn cannot create them.

`barn --help` and `barn <command> --help` are authoritative if anything here
disagrees with the installed version (`barn --version`).

## Rules before you act

- **Anything that changes an AWS account needs the user's confirmation each
  time**: `create` (including resize and re-create), `destroy`, `start`,
  `stop`. They cost money or delete things. Read-only commands (`list`,
  `connect`, `ssh`, `doctor`) and `hold` and `sync` are fine to run when the
  task calls for them.
- **Never run `destroy --volume`** unless the user explicitly asks to delete
  the home volume. It is the box's only copy of its data: there are no
  backups. It also prompts for the name, so it cannot run unattended.
- Don't use `--force` on `destroy` to remove someone else's box unless the
  user says so.
- Ask which account to use when it isn't obvious; don't assume
  `default_account` is what the user meant for a new box.
- **Secrets** live only in the barn home's `home/` directory (usually
  `home/.barn/env`) and on boxes. Never put them in `startup.sh` (user data is
  readable by anyone on the box), in `shared_home`, or in any git repo.
- `create` takes ~5 minutes for a first boot (it waits up to 25). Run it in the
  background or with a long timeout, not a short one.

## Roles

- **Managers** (`barn-manager` role): `create`, `destroy`, `start`, `stop`,
  `sync`, plus everything guests can do. `create`, `destroy` (without
  `--force`) and `sync` refuse boxes owned by someone else.
- **Guests** (`barn-guest` role): `list`, `connect`, `ssh`, `doctor`, `hold`.
  They cannot start a stopped box; a manager must.

On the box everyone logs in as `ubuntu` (UID 1000) with sudo, and sees the
same herdr session.

## Setup

```bash
uv tool install git+<barn repo>          # or @vX.Y.Z; uv tool upgrade barn later
barn init                                 # writes ~/.config/barn with CHANGE-ME placeholders
barn init --account-file sandbox.toml --aws-profile my-dev   # start from an admin's account file
barn init --account-file work.toml        # add another account later
```

Requirements: Terraform >= 1.10, AWS CLI v2, the Session Manager plugin, `ssh`,
`uv`, an AWS profile that assumes `barn-manager` or `barn-guest`, and an SSH
key pair. `barn init` checks the tools.

`barn init` never overwrites files; re-run it after editing to see what is
still missing. Every other command refuses to run while a `CHANGE-ME` remains.

The barn home (`$BARN_HOME`, default `~/.config/barn/`):

| Path | What |
|---|---|
| `config.toml` | Settings (below). Migrated automatically on upgrade; old copy kept as `config.toml.<version>.bak`. Don't edit `config_version`. |
| `accounts/<account>.toml` | Where an account's network, roles and state bucket are. Usually from the platform admin. |
| `startup.sh` | What new boxes install at first boot. |
| `home/` | Overlay synced onto your boxes' home directory (tokens, dotfiles). |
| `terraform/`, `bootstrap/` | Managed copies refreshed on every run. Never edit. |
| `state/<account>/<name>/<root>/apply.log` | Terraform logs per box. |

Key `config.toml` settings:

```toml
default_account = "sandbox"      # where new boxes go without --account
user = "jdoe"                    # barn:owner tag on boxes you create
ssh_public_key = "~/.ssh/id_ed25519.pub"
default_size = "m7i.xlarge"
idle_hours = 4                   # idle time before the box stops itself
volume_gb = 100
startup_script = "startup.sh"    # remove for barn's built-in default
# shared_home = "~/src/team-dotfiles/home"   # team defaults, synced before home/

[aws_profiles]                   # one profile per account, used as-is
sandbox = "barn-jdoe-manager"

[guest_keys]                     # per account: extra keys allowed on boxes you create
sandbox = ["ssh-ed25519 AAAA... alice"]
```

## Everyday workflow

Box names: 2–40 lowercase letters, digits and dashes, unique per account. The
name is the SSH alias, hostname and `barn:name` tag. A `<user>-<purpose>`
convention (`jdoe-dev`) avoids clashes.

```bash
barn list                      # all boxes in all accounts: owner, state, type, uptime, cost
barn create jdoe-dev           # manager; add --account work for another account, --size TYPE to choose size
barn sync jdoe-dev             # push guest_keys and home/ overlay onto the box
barn doctor jdoe-dev           # everything green?
barn ssh jdoe-dev              # connect + ssh; afterwards plain `ssh jdoe-dev` works too
```

Box commands find the box in whichever account has it (`default_account`
first). Pass `--account` only to create outside `default_account` or when two
accounts have a box with the same name.

| Command | What it does |
|---|---|
| `create <name> [--size TYPE] [-v]` | Create, or update in place. Re-run with a new `--size` to resize. `-v` streams Terraform output. |
| `destroy <name> [-v]` | Delete the instance, **keep the home volume**. A later `create` brings back the files and the same SSH host key. |
| `destroy <name> --volume` | Also delete the home volume (asks for the name). |
| `stop <name>` / `start <name>` | A stopped box costs only its volumes. |
| `sync <name>` | Push `guest_keys`, then copy `shared_home` and `home/` onto the box. Local files win; nothing on the box is deleted; files are skipped while agents are running. |
| `connect <name>` | Write the box's entry to `~/.ssh/barn.conf` so `ssh`, `scp`, `rsync` and `herdr --remote` work. |
| `ssh <name> [ssh args]` | `connect`, then ssh. Extra args go to ssh. |
| `doctor <name>` | Check AWS, IAM (managers), SSM, first boot, tools, network, linger, home mount and sign-in. |
| `hold <name> <30m\|8h\|2d>` | Keep the box from idle-stopping that long. |

## Running agents on a box

On the box, `herdr` starts or attaches the persistent session; run `claude` or
`kiro-cli` in its panes, detach with `ctrl+b q`, reattach with `herdr`. Don't
run herdr inside tmux. From the laptop, `herdr --remote <name>` attaches over
the same SSH entry.

For non-interactive work, use ssh commands. `~/.barn/env` is sourced for
`ssh box cmd` too, so tokens are in the environment:

```bash
ssh jdoe-dev 'cd ~/src/app && git pull'
scp -r ./data jdoe-dev:~/
barn ssh jdoe-dev -N -L 8080:localhost:8080      # reach a service on the box
ssh jdoe-dev 'herdr agent list'                   # agents and their states
```

herdr has its own CLI for driving panes and agents (`herdr agent ...`,
`herdr pane ...`, `herdr workspace ...`). Run `herdr --skill` on the box for
its guide instead of guessing subcommands.

## Tokens and dotfiles (sync)

Put personal config under `~/.config/barn/home/`; paths are relative to the
box's home (`home/.gitconfig` → `~/.gitconfig`). Tokens go in
`home/.barn/env` (mode 600), sourced at the top of `~/.bashrc`:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`
export KIRO_API_KEY=...
```

Worth syncing: `.claude/settings.json`, `.claude/CLAUDE.md`, `.claude/skills/`,
`.kiro/`, `.gitconfig`, `.config/herdr/config.toml`. Never sync
`~/.local/share/kiro-cli/` (hundreds of MB of runtimes). `sync` refuses
credential-looking files in `shared_home`. Run `barn sync <name>` after any
change to `home/` or `guest_keys`.

## Startup script

`startup.sh` runs as root on each instance's first boot, after barn's own
setup. As written by `barn init` it runs barn's default
(`bash /usr/local/lib/barn/startup-default.sh`: apt basics, Docker, Neovim,
`kiro-cli`, `claude`, `herdr`, `uv`), and you can add lines after it:

```bash
bash /usr/local/lib/barn/startup-default.sh
apt-get install -y -q postgresql-client
sudo -u "$BARN_USER" -H bash -lc 'curl -fsSL https://bun.sh/install | bash' \
  || echo "bun install failed" >> "$BARN_WARNINGS"
```

- Install user tools as `$BARN_USER` into `~/.local`. Append to
  `$BARN_WARNINGS` (or exit non-zero) to surface a warning in `create` and
  `doctor`.
- It must be safe to rerun: a recreated box's home volume already has the tools.
- Limit about 5 KB gzipped, no secrets. For more, download and run a script.
- **Editing it means the next `barn create <name>` replaces the instance**
  (home volume kept). Existing boxes keep what they have until then; `doctor`
  reports boxes on an older version.

## Guests

To let someone onto boxes you create in an account, add their public key under
`[guest_keys]` for that account and run `barn sync <name>` (or `create`). To
revoke, remove it and `sync` again. That removes only barn's block in
`authorized_keys`; a guest has sudo, so only add people trusted with your
tokens. The guest uses their own `barn init` with the same account file, then
`barn list`, `barn connect <name>`, `ssh <name>`.

Several people can be attached to herdr at once and all can type.

## Idle stop

Every 5 minutes the box checks for use: a herdr agent working (or blocked for
less than 24 h), SSH typing in the last 30 min, a pane running something other
than its shell or an agent, or an active `hold`. After `idle_hours` with none
of these it warns, then **stops** the box 15 minutes later (never terminates
it). `barn start <name>` brings it back and herdr restores its layout. Before
leaving unattended work that doesn't show as an active agent (a long build in a
plain shell is fine; a detached background job is not), use `barn hold`.

## What survives what

- `~` is a separate encrypted EBS volume: survives `stop`, `destroy` and
  re-`create`. No backups, so push work to git.
- Root disk (apt packages, Neovim, Docker engine) is rebuilt on each new
  instance. Docker's data is at `~/.barn/docker`, so images and volumes survive.
- Agents on the box use the box's own IAM role, never your credentials.

## Troubleshooting

1. `barn doctor <name>` first; it names the failing check.
2. `create` failed: it prints the last lines and the log path
   (`state/<account>/<name>/<root>/apply.log`); re-run with `-v` to stream it.
   A permission error usually means the account's manager policy lacks
   something: tell the user, don't edit IAM yourself.
3. First boot on the box: `/var/log/barn-bootstrap.log`; markers in
   `/var/lib/barn/`.
4. `ssh <name>` reaches the wrong box: `~/.ssh/barn.conf` holds one entry per
   name, from the last `connect`/`ssh`/`create`/`start`/`doctor`. Run
   `barn connect <name> --account <account>`.
5. Box is stopped (idle or by someone): a manager runs `barn start <name>`.
6. An account missing from `barn list` with a warning: its profile's
   credentials failed (expired SSO: `aws sso login --profile <profile>`).
