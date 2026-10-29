#!/usr/bin/env python3
"""Idle-stop decision for an barn box (PLAN.md section 10).

idle-check.sh gathers raw inputs into a directory; this script parses them,
decides, updates the state file, and prints one action: none, warn, or shutdown.
Parsing and decision are pure functions so they can be unit-tested off the box.
Stdlib only: it runs under the box's system python3.
"""

import json
import sys
from pathlib import Path

BLOCKED_GRACE = 24 * 3600  # a blocked agent keeps the box up this long
SESSION_IDLE = 30 * 60  # a login session idle less than this keeps the box up
WARN_LEAD = 15 * 60  # wall warning this long before shutdown

# Foreground processes that are agents; their pane is judged by herdr's status
# (signals 1 and 2), not by signal 4, or a blocked agent would never let go.
AGENT_COMMANDS = {"claude", "kiro-cli", "kiro-cli-chat", "node", "bun"}
WORKING = "working"
BLOCKED = "blocked"


def parse_herdr_snapshot(text):
    """Return {pane_id: agent_status} from `herdr api snapshot` output.

    Walks the whole document for records carrying an agent status, so it does
    not depend on exactly where herdr nests pane and agent records.
    """
    doc = json.loads(text)
    panes = {}

    def walk(node):
        if isinstance(node, dict):
            status = node.get("agent_status")
            if status is None and "agent_id" in node:
                status = node.get("status") or node.get("state")
            pane_id = node.get("pane_id")
            if isinstance(status, str) and pane_id:
                # Keep the busiest status if a pane appears more than once.
                if panes.get(pane_id) != WORKING:
                    panes[pane_id] = status
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return panes


def parse_sessions(text):
    """Parse lines of `id<TAB>class<TAB>tty<TAB>idle_since_usec` into dicts."""
    sessions = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        sid, cls, tty, since = parts
        try:
            since_s = int(since) / 1_000_000
        except ValueError:
            since_s = 0
        sessions.append({"id": sid, "class": cls, "tty": tty, "idle_since": since_s})
    return sessions


def parse_foreground(text):
    """One foreground command name per line (panes whose foreground is not the shell)."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_hold(text):
    try:
        return float(text.strip())
    except (ValueError, AttributeError):
        return 0.0


def active_reasons(now, inputs, state):
    """Return (reasons, new_blocked_since). Empty reasons means idle.

    inputs keys: herdr_error (bool), panes ({pane: status}), sessions (list),
    foreground (list of command names), hold_until (epoch seconds).
    state["blocked_since"]: {pane: epoch} first time each pane was seen blocked.
    """
    reasons = []
    prev_blocked = state.get("blocked_since", {})
    blocked_since = {}

    if inputs.get("herdr_error"):
        reasons.append("herdr query failed (fail safe)")

    for pane, status in sorted(inputs.get("panes", {}).items()):
        if status == WORKING:
            reasons.append(f"agent working in {pane}")
        elif status == BLOCKED:
            since = prev_blocked.get(pane, now)
            blocked_since[pane] = since
            if now - since < BLOCKED_GRACE:
                reasons.append(f"agent blocked in {pane} for {int(now - since)}s")

    for s in inputs.get("sessions", []):
        if s["class"] != "user" or not s["tty"] or not s["idle_since"]:
            continue
        if now - s["idle_since"] < SESSION_IDLE:
            reasons.append(f"session {s['id']} on {s['tty']} active")

    for cmd in inputs.get("foreground", []):
        if cmd not in AGENT_COMMANDS:
            reasons.append(f"foreground process {cmd}")

    if inputs.get("hold_until", 0) > now:
        reasons.append("hold")

    return reasons, blocked_since


def decide(now, inputs, state, idle_hours):
    """Return (action, reasons, new_state). action is none, warn, or shutdown."""
    reasons, blocked_since = active_reasons(now, inputs, state)
    new = {
        "last_active": state.get("last_active", now),
        "warned_at": state.get("warned_at"),
        "blocked_since": blocked_since,
    }
    if reasons:
        new["last_active"] = now
        new["warned_at"] = None
        return "none", reasons, new

    if now - new["last_active"] < idle_hours * 3600:
        return "none", reasons, new
    if new["warned_at"] is None:
        new["warned_at"] = now
        return "warn", reasons, new
    if now - new["warned_at"] >= WARN_LEAD:
        return "shutdown", reasons, new
    return "none", reasons, new


def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def gather(input_dir):
    d = Path(input_dir)
    inputs = {"herdr_error": False, "panes": {}}
    rc = read(d / "herdr.rc").strip()
    if rc == "absent":
        pass  # herdr server not running: no panes, not an error
    elif rc != "0":
        inputs["herdr_error"] = True
    else:
        try:
            inputs["panes"] = parse_herdr_snapshot(read(d / "herdr.json"))
        except ValueError:
            inputs["herdr_error"] = True
    inputs["sessions"] = parse_sessions(read(d / "sessions.tsv"))
    inputs["foreground"] = parse_foreground(read(d / "foreground.txt"))
    inputs["hold_until"] = parse_hold(read(d / "hold-until"))
    return inputs


def main(argv):
    input_dir, state_path, idle_hours, now = argv[1], argv[2], float(argv[3]), float(argv[4])
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        state = {}
    action, reasons, new_state = decide(now, gather(input_dir), state, idle_hours)
    Path(state_path).write_text(json.dumps(new_state))
    print(action)
    for r in reasons:
        print(r, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
