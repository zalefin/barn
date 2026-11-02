#!/bin/bash
# Gathers idle-stop inputs and acts on idle_decide.py's verdict (PLAN.md
# section 10). Runs as root from idle-check.timer every 5 minutes.
set -uo pipefail

# shellcheck source=/dev/null
. /etc/barn/idle.conf # BARN_USER, IDLE_HOURS
LIB=/usr/local/lib/barn
STATE=/var/lib/barn
HOME_DIR="/home/$BARN_USER"
UID_NUM=$(id -u "$BARN_USER")
IN=$(mktemp -d)
trap 'rm -rf "$IN"' EXIT

# Signals 1-2: herdr agent states. A missing socket means no herdr server.
if [ -S "$HOME_DIR/.config/herdr/herdr.sock" ]; then
  timeout 30 runuser -u "$BARN_USER" -- env HOME="$HOME_DIR" XDG_RUNTIME_DIR="/run/user/$UID_NUM" \
    "$HOME_DIR/.local/bin/herdr" api snapshot > "$IN/herdr.json" 2> "$IN/herdr.err"
  echo $? > "$IN/herdr.rc"
else
  echo absent > "$IN/herdr.rc"
fi

# Signal 3: logind sessions (id, class, tty, idle-since in usec).
loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}' | while read -r sid; do
  [ -n "$sid" ] || continue
  printf '%s\t%s\t%s\t%s\n' "$sid" \
    "$(loginctl show-session "$sid" -p Class --value)" \
    "$(loginctl show-session "$sid" -p TTY --value)" \
    "$(loginctl show-session "$sid" -p IdleSinceHint --value)"
done > "$IN/sessions.tsv"

# Signal 4: herdr panes whose foreground process group is not the shell's.
for server in $(pgrep -u "$BARN_USER" -x herdr); do
  for shell in $(pgrep -P "$server"); do
    stat=$(cat "/proc/$shell/stat" 2>/dev/null) || continue
    read -r -a f <<< "${stat##*) }" # f[0]=state f[2]=pgrp f[5]=tpgid
    pgrp=${f[2]}
    tpgid=${f[5]}
    if [ "$tpgid" -gt 0 ] && [ "$tpgid" != "$pgrp" ]; then
      cat "/proc/$tpgid/comm" 2>/dev/null
    fi
  done
done > "$IN/foreground.txt"

# Signal 5: hold.
cp "$HOME_DIR/.barn/hold-until" "$IN/hold-until" 2>/dev/null

action=$(python3 "$LIB/idle_decide.py" "$IN" "$STATE/idle-state.json" "$IDLE_HOURS" "$(date +%s)")
echo "idle-check: action=$action"
case "$action" in
  warn)
    wall "barn: this box has been idle for ${IDLE_HOURS}h and will stop in 15 minutes. Run 'barn hold $(hostname) <duration>' or start working to keep it up."
    ;;
  shutdown)
    wall "barn: idle; stopping now (home volume is kept)."
    shutdown -h now
    ;;
esac
