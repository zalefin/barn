#!/bin/bash
# The checks scripts/integration.sh runs inside its container. Every step runs
# and is reported; the box (with --create) is destroyed whatever happens.
set -uo pipefail

account=$IT_ACCOUNT
name="it-$(date +%m%d-%H%M)"
results=()
failed=0

bold=""; plain=""
[ -n "${FORCE_COLOR:-}" ] && bold=$'\033[1m' plain=$'\033[0m'
step() { printf '\n%s== %s%s\n' "$bold" "$1" "$plain"; }
record() {  # record LABEL STATUS
  if [ "$2" = 0 ]; then results+=("pass  $1"); else results+=("FAIL  $1"); failed=1; fi
}
# check LABEL CMD...: CMD must succeed.
check() {
  local label=$1; shift
  step "$label"
  "$@"
  record "$label" $?
}
# check_quiet LABEL PATTERN CMD...: CMD must succeed without PATTERN in its output.
check_quiet() {
  local label=$1 pattern=$2; shift 2
  step "$label"
  local out status
  out=$("$@" 2>&1); status=$?
  printf '%s\n' "$out"
  if [ $status = 0 ] && grep -qE "$pattern" <<<"$out"; then
    echo ">> unexpected: $pattern"; status=1
  fi
  record "$label" $status
}
# check_denied LABEL CMD...: CMD must fail with an AWS authorization error.
check_denied() {
  local label=$1; shift
  step "$label"
  local out status=0
  out=$("$@" 2>&1) && status=1
  printf '%s\n' "$out"
  if [ $status = 0 ] && ! grep -qE 'UnauthorizedOperation|AccessDenied' <<<"$out"; then
    echo ">> failed, but not with an authorization error"; status=1
  fi
  record "$label" $status
}
# check_doctor LABEL CMD...: doctor may fail only the sign-in checks (the test
# box has no CLAUDE_CODE_OAUTH_TOKEN or KIRO_API_KEY).
check_doctor() {
  local label=$1; shift
  step "$label"
  local out others
  out=$("$@" 2>&1)
  printf '%s\n' "$out"
  others=$(grep '^✗' <<<"$out" | grep -v 'signed in')
  [ -z "$others" ]
  record "$label" $?
}
summary() {
  step Summary
  printf '  %s\n' "${results[@]}"
  [ $failed = 0 ] && echo "all checks passed" || echo "some checks failed"
}

# --- setup: credentials, keys, barn ---

mkdir -p ~/.aws ~/.ssh && chmod 700 ~/.ssh
umask 077
cat > ~/.aws/credentials <<EOF
[it-manager]
aws_access_key_id = $IT_MANAGER_AWS_ACCESS_KEY_ID
aws_secret_access_key = $IT_MANAGER_AWS_SECRET_ACCESS_KEY
aws_session_token = ${IT_MANAGER_AWS_SESSION_TOKEN:-}

[it-guest]
aws_access_key_id = $IT_GUEST_AWS_ACCESS_KEY_ID
aws_secret_access_key = $IT_GUEST_AWS_SECRET_ACCESS_KEY
aws_session_token = ${IT_GUEST_AWS_SESSION_TOKEN:-}
EOF
umask 022
unset "${!IT_MANAGER_@}" "${!IT_GUEST_@}"
ssh-keygen -q -t ed25519 -N '' -C it -f ~/.ssh/id_ed25519
ssh-keygen -q -t ed25519 -N '' -C it-guest -f ~/.ssh/guest_ed25519
mkdir -p /tmp/it && printf '%s\n' "$IT_ACCOUNT_TOML" > "/tmp/it/$account.toml"

check "install barn from the working tree" uv tool install -q ~/src
barn --version

# --- init: the manager's barn home; the guest is a second settings file ---

check "init (first run)" barn init --account-file "/tmp/it/$account.toml" --aws-profile it-manager
config=~/.config/barn/config.toml
sed -i 's/^user = .*/user = "it"/' "$config"
# [guest_keys] is the file's last table, so this line belongs to it.
printf '%s = ["%s"]\n' "$account" "$(cat ~/.ssh/guest_ed25519.pub)" >> "$config"
check_quiet "init (ready)" 'Edit the files above' barn init

cat > ~/guest.toml <<EOF
$(grep '^config_version' "$config")
default_account = "$account"
user = "it-guest"
ssh_public_key = "~/.ssh/guest_ed25519.pub"

[aws_profiles]
$account = "it-guest"
EOF
# shellcheck disable=SC2329  # called through check and check_quiet
guest() { BARN_SETTINGS=~/guest.toml barn "$@"; }

check_quiet "manager: list" 'rough us-east-1 prices|skipped account' barn list
check_quiet "guest: list" 'rough us-east-1 prices|skipped account' guest list
# list falls back to rough prices silently when the role lacks the
# (optional) pricing:GetProducts, so ask for one price directly.
for role in manager guest; do
  check "$role: pricing:GetProducts" aws pricing get-products --profile "it-$role" --region us-east-1 \
    --service-code AmazonEC2 --max-results 1 --query 'length(PriceList)' --output text
done

if [ "$IT_CREATE" != 1 ]; then
  summary
  echo "(no box created; pass --create for the full run)"
  exit $failed
fi

# --- a real box ---

destroyed=0
destroy() {
  [ $destroyed = 1 ] && return
  destroyed=1
  # destroy --volume asks for the box's name.
  check "manager: destroy --volume" bash -c "printf '%s\n' '$name' | barn destroy '$name' --volume"
  check_quiet "manager: list no longer shows the box" "$name" barn list
}
trap 'echo; echo "interrupted; destroying $name"; destroy; summary; exit 130' INT TERM

echo
echo "box: $name"
check "manager: create" barn create "$name" ${IT_SIZE:+--size "$IT_SIZE"}
check_doctor "manager: doctor (includes the policy simulation)" barn doctor "$name"
check "manager: ssh" barn ssh "$name" true </dev/null
check "manager: sync" barn sync "$name"
check "manager: hold" barn hold "$name" 30m

check "guest: list shows the box" bash -c "BARN_SETTINGS=~/guest.toml barn list 2>&1 | grep -q '$name'"
check "guest: connect" guest connect "$name"
check "guest: ssh with the guest key" guest ssh "$name" true </dev/null
check_denied "guest: stop is denied" guest stop "$name"

check "manager: stop" barn stop "$name"
check "manager: start" barn start "$name"
check "manager: ssh after start" barn ssh "$name" true </dev/null

destroy
summary
exit $failed
