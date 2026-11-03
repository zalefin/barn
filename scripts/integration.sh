#!/bin/bash
# Manual end-to-end check against a real AWS account, from a clean container:
# install barn from the working tree, init, list as a manager and as a guest,
# and with --create a real box's whole life (create, doctor, ssh, sync, hold,
# guest access, stop, start, destroy). Run it at the end of a feature, mainly
# to catch permissions the policies are missing. Not part of scripts/check.sh.
#
# The container gets short-lived credentials for your existing manager and
# guest profiles (aws configure export-credentials), nothing else of yours.
# With --create it costs a few cents: one box for about 15 minutes, destroyed
# with its home volume at the end, also when a step fails or on Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
usage: scripts/integration.sh [--create] [--size TYPE] [--account-file PATH]
                              [--manager-profile P] [--guest-profile P]

  --create            also create, exercise and destroy a real box
  --size TYPE         instance type for --create (default: barn's default_size)
  --account-file      default: your barn home's default account file
  --manager-profile   default: that account's profile in your config.toml
  --guest-profile     default: the manager profile with -manager -> -guest
EOF
}

create=0 size="" account_file="" manager="" guest=""
while [ $# -gt 0 ]; do
  case "$1" in
    --create) create=1 ;;
    --size) size=$2; shift ;;
    --account-file) account_file=$2; shift ;;
    --manager-profile) manager=$2; shift ;;
    --guest-profile) guest=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

# Defaults from your own barn home: its default account and that account's profile.
home=${BARN_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/barn}
read -r default_account default_profile < <(python3 - "${BARN_SETTINGS:-$home/config.toml}" <<'PY'
import sys, tomllib
try:
    s = tomllib.load(open(sys.argv[1], "rb"))
except OSError:
    s = {}
a = s.get("default_account", "")
print(a or "-", s.get("aws_profiles", {}).get(a, "") or "-")
PY
)
account_file=${account_file:-$home/accounts/$default_account.toml}
manager=${manager:-${default_profile#-}}
guest=${guest:-${manager%-manager}-guest}
[ -f "$account_file" ] || { echo "no account file at $account_file; pass --account-file" >&2; exit 2; }
[ -n "$manager" ] || { echo "no manager profile; pass --manager-profile" >&2; exit 2; }
account=$(basename "$account_file" .toml)

echo "account $account ($account_file) · manager $manager · guest $guest"
docker build -q -t barn-integration scripts/integration >/dev/null

# Role credentials for the container, as IT_MANAGER_* and IT_GUEST_* environment
# variables (passed to docker by name, so never on a command line).
names=()
for role in manager guest; do
  profile=$manager; [ "$role" = guest ] && profile=$guest
  prefix=IT_$(tr '[:lower:]' '[:upper:]' <<<"$role")_
  creds=$(aws configure export-credentials --profile "$profile" --format env-no-export) || {
    echo "cannot get credentials for profile $profile" >&2; exit 1; }
  while IFS='=' read -r key value; do
    export "$prefix$key=$value"
    names+=(-e "$prefix$key")
  done <<<"$creds"
done
expires=${IT_MANAGER_AWS_CREDENTIAL_EXPIRATION:-}
[ -n "$expires" ] && echo "credentials expire at $expires"

export IT_ACCOUNT=$account IT_CREATE=$create IT_SIZE=$size
IT_ACCOUNT_TOML=$(cat "$account_file")
export IT_ACCOUNT_TOML

# The working tree (tracked and untracked, not ignored or deleted files) goes
# in on stdin, so uncommitted changes are tested and no host path is mounted.
color=(); [ -t 1 ] && color=(-e FORCE_COLOR=1)
comm -23 <(git ls-files -co --exclude-standard | sort) <(git ls-files -d | sort) \
  | tar -cf - -T - \
  | docker run --rm -i --init "${color[@]}" \
      -v barn-integration-cache:/home/barn/.cache \
      "${names[@]}" -e IT_ACCOUNT -e IT_CREATE -e IT_SIZE -e IT_ACCOUNT_TOML \
      barn-integration \
      bash -c 'mkdir -p ~/src && tar -C ~/src -xf - && exec ~/src/scripts/integration/inner.sh'
