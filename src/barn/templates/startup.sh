#!/bin/bash
# What your new boxes install at first boot (config.toml: startup_script).
# Written by `barn init`; edit freely. It runs as root on the first boot of each
# instance, after barn's own setup: on `barn create`, and again after
# destroy/create. After editing, `barn create <name>` replaces that box's
# instance to run the new version (the home volume is kept).
#
# Keep it safe to rerun: the home volume (~/.local, ...) may already hold a
# previous box's tools. No secrets here: it goes into the instance's user data,
# which anyone on the box can read. Tokens go in home/.barn/env.
#
# $BARN_USER is the login user (ubuntu); install into their home as them. A
# line appended to "$BARN_WARNINGS" shows as a warning in `barn create` and
# `barn doctor`, and so does a non-zero exit. Output goes to
# /var/log/barn-bootstrap.log on the box.
set -uo pipefail

# barn's default: apt packages (git, jq, ripgrep, ...), Docker, Neovim,
# kiro-cli, claude, herdr and uv. To pick your own instead, delete this line
# and copy what you want from /usr/local/lib/barn/startup-default.sh on a box.
bash /usr/local/lib/barn/startup-default.sh

# Your additions, for example:
# apt-get install -y -q postgresql-client
# sudo -u "$BARN_USER" -H bash -lc 'curl -fsSL https://bun.sh/install | bash' \
#   || echo "bun install failed" >> "$BARN_WARNINGS"
