#!/bin/bash
# barn's default startup script: the packages and tools a box gets when
# config.toml has no startup_script (PLAN.md section 8). The first-boot script
# runs it as root after barn's own setup. It is on every box at
# /usr/local/lib/barn/startup-default.sh, so your own startup script can run it
# and then add to it. Must be safe to rerun against a home volume that already
# holds a previous box's tools.
#
# The first-boot script sets BARN_USER (the login user) and BARN_WARNINGS (a
# file: each line appended to it shows as a warning in `barn create` and
# `barn doctor`).
set -uo pipefail

USER_NAME=${BARN_USER:-ubuntu}
step() { echo "barn startup: $*"; }
warn() { echo "barn startup WARNING: $*"; echo "$*" >> "${BARN_WARNINGS:-/dev/null}"; }
# doctor expects the default tools only on boxes where this script ran.
date -u +%Y-%m-%dT%H:%M:%SZ > /var/lib/barn/startup-default

# Packages and Docker. Docker's data-root and its wait for the home mount are
# set up by the first-boot script, so any Docker install keeps its data on the
# home volume.
step "packages"
export DEBIAN_FRONTEND=noninteractive
apt_retry() { for _ in 1 2 3 4 5; do "$@" && return 0; sleep 10; done; return 1; }
apt_retry apt-get update -q || warn "apt-get update failed"
apt_retry apt-get install -y -q git build-essential jq ripgrep unzip unar python3 rsync ca-certificates curl \
  || warn "apt packages failed"
install -m 0755 -d /etc/apt/keyrings
if curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; then
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt_retry apt-get update -q
  apt_retry apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    || warn "docker install failed"
  usermod -aG docker "$USER_NAME"
else
  warn "docker gpg key download failed"
fi
# Neovim: the latest stable release, as Ubuntu's package is too old for most
# configs. On the root disk, so every new box gets the current release.
NVIM_ARCH=$(uname -m | sed 's/aarch64/arm64/')
install_nvim() {
  curl -fsSL "https://github.com/neovim/neovim/releases/latest/download/nvim-linux-$NVIM_ARCH.tar.gz" -o /tmp/nvim.tar.gz \
    && rm -rf /opt/nvim && mkdir -p /opt/nvim \
    && tar -xzf /tmp/nvim.tar.gz -C /opt/nvim --strip-components=1 \
    && ln -sf /opt/nvim/bin/nvim /usr/local/bin/nvim
}
apt_retry install_nvim || { warn "neovim release download failed; using Ubuntu's"; apt_retry apt-get install -y -q neovim; }
rm -f /tmp/nvim.tar.gz

# Vendor installers, as the user, unpinned (they install into ~/.local, on the
# home volume).
step "vendor installers"
# pipefail: a failed download must fail the install, not run an empty script.
as_user() { sudo -u "$USER_NAME" -H bash -lc "set -o pipefail; $1" </dev/null; }
# Three tries: a dropped connection mid-download is common enough.
install_tool() {
  for try in 1 2 3; do
    as_user "$2" && return 0
    step "$1 install failed (try $try)"
    sleep 10
  done
  warn "$1 install failed"
}
# Kiro's installer prompts on /dev/tty over an existing install (as on a
# recreated box, or a failed try); remove what it would remove after a "y".
install_tool kiro-cli 'rm -f ~/.local/bin/kiro-cli ~/.local/bin/kiro-cli-chat; curl -fsSL https://cli.kiro.dev/install | bash'
install_tool claude 'curl -fsSL https://claude.ai/install.sh | bash'
install_tool herdr 'curl -fsSL https://herdr.dev/install.sh | bash'
install_tool uv 'curl -LsSf https://astral.sh/uv/install.sh | sh'
