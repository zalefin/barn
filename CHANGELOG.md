# Changelog

barn follows [semantic versioning](https://semver.org/). Before 1.0.0, a
minor release (0.x.0) may change the CLI or the files in `~/.config/barn/`;
when it changes the files, barn migrates them automatically on first run
(README: [Versions](README.md#versions)). Releases are git tags `vX.Y.Z`.

## 0.2.0 (2026-11-02)

- **Multiple AWS accounts.** `config.toml`: `account` is now `default_account`,
  and `aws_profile` an `[aws_profiles]` table with one profile per account.
  Box commands take `--account`; `barn list` shows every account, with an
  ACCOUNT column. Terraform data dirs move to `state/<account>/<name>/`.
- **Versions and automatic migrations.** `config.toml` records
  `config_version`; a newer barn migrates the file (keeping
  `config.toml.<old version>.bak`) and the home. Migration 0.2.0 does the
  changes above, so 0.1.0 configs keep working.

## 0.1.0

The first version: managers and guests, full box names, `barn init`. Configs
from it have no `config_version`.
