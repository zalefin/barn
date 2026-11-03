# AGENTS.md

barn is a Python CLI (`uv tool install`) that gives developers EC2 dev boxes
for long-running, headless agent runs (Claude Code, Kiro CLI under herdr). It
manages only a per-box home volume and instance with Terraform; everything
else (network, roles, state bucket) is an account prerequisite. Boxes are
reached by SSH tunnelled through SSM; nothing listens on the network.

- [README.md](README.md): user docs: commands, config, sync, startup script, idle stop, releases.
- [docs/plans/PLAN.md](docs/plans/PLAN.md): the design and the source of
  truth. Sections 2–12 are the current design; later numbered sections record
  each change and the user decisions behind it. Add a section when you make a
  design change, and fold it into sections 2–12.
- [docs/account-prerequisites.md](docs/account-prerequisites.md),
  [docs/onboarding.md](docs/onboarding.md): what an AWS account must provide.

## Commands

```bash
uv sync                          # uv only: never pip/poetry
uv run barn ...                  # run from the checkout
scripts/check.sh                 # pytest, terraform fmt/validate, shellcheck: run before finishing
scripts/integration.sh           # real account, clean container: install, init, list (free)
scripts/integration.sh --create  # also a real box (~15 min, costs money: ask first)
```

## Rules

- **Git:** never commit, push or tag unless asked. No Claude/Anthropic
  attribution in commits, PRs or MRs.
- **AWS:** anything that changes an AWS account (apply, destroy, create,
  deletes, `integration.sh --create`) needs the user's confirmation each time.
  Ask which account and profile to use; never assume.
- **Secrets:** never write real tokens or credentials into tracked files. They
  live only in the user's barn home (`home/` there) and on boxes.
- **Vendor installers stay unpinned** (user decision).
- **Linux and Windows:** both are supported, and CI runs the tests on both.
  Put platform differences in `src/barn/compat.py`. Read and write text as
  UTF-8, and send everything that reaches a box with LF line endings.

## Config versions and migrations: keep them working

The barn home (`$BARN_HOME`, default `~/.config/barn/`) holds `config.toml`,
which records `config_version`. On every run barn runs
each migration in `src/barn/migrate.py` registered above that version, up to
the installed one, and keeps the old file as `config.toml.<old>.bak`. Users
never migrate by hand, so **any change to the format of `config.toml`, the
account files or the barn home's layout needs a migration**:

1. Bump `version` in `pyproject.toml` (before 1.0.0: a minor bump for format,
   CLI or layout changes, else a patch bump), then run `uv lock`. A migration
   registered above the package version never runs, and a test checks for that.
2. Add `@migration("<new version>")` in `migrate.py`. Edit `config.toml` as
   text with the helpers there, so comments and layout survive. Make it
   **idempotent**: the file may already be partly converted by hand. Never
   overwrite a user's files.
3. Add a test in `tests/test_migrate.py`: an old file migrates, a second run
   changes nothing, and hand-converted files are left alone.
4. Update `src/barn/templates/` (what `barn init` writes), README, PLAN.md and
   `CHANGELOG.md`.

Never change or delete a released migration. Tests that write a config should
use `migrate.installed()` as its version, unless they are testing migrations.

## How it fits together

- `src/barn/cli.py`: commands (typer) and the box lifecycle. `config.py`:
  settings and account files. `doctor.py`: health checks, including a shell
  script run on the box. `sync.py`: home overlay over ssh. `setup.py`:
  `barn init`.
- `src/barn/terraform/{volume,instance}` and `src/barn/bootstrap/` ship in the
  package. `tf.install_roots` copies them into the barn home on every run, and
  Terraform runs from that copy, with state in the account's S3 bucket under
  `env/<name>/`.
- **First boot** is `bootstrap/user-data.sh.tftpl`, rendered by the instance
  root's `templatefile()`:
  - It runs as root once per instance. It must be idempotent against a home
    volume that already holds a previous box's data, because the volume
    survives `destroy`/`create`.
  - Terraform template syntax applies: write a bash `${...}` as `$${...}`.
    `scripts/check.sh` renders the template and shellchecks it.
  - User data is replaced on change, so **any change to the bootstrap files or
    instance variables replaces existing instances on their next `create`**
    (home volumes are kept). Say so in the changelog.
  - EC2 allows 16 KB of gzipped user data, and barn uses about 9.6 KB. Large
    additions go elsewhere.
  - Failures are warnings when the box stays usable, and `die` only when it
    doesn't. The markers in `/var/lib/barn/` are read by `create` and `doctor`.
- **Startup script:** what a box installs (apt packages, Docker, Neovim,
  kiro-cli, claude, herdr, uv) lives in `bootstrap/startup-default.sh`, not in
  the fixed bootstrap. Users can replace it with `startup_script`; `doctor`
  checks the default's tools only on boxes where the default ran. Keep
  barn-required setup in the fixed part.
- **IAM:** the manager and guest policies live in `docs/*-policy.json` and
  `docs/account-prerequisites.md`, and `doctor.simulate_checks` lists the
  permissions it verifies. A new AWS call needs all three updated.
- **Ownership:** boxes are tagged `barn=true`, `barn:name` and `barn:owner`.
  Managers create, destroy, start and stop boxes; guests can only connect.
  `create`, `destroy` (without `--force`) and `sync` refuse boxes owned by
  someone else.
