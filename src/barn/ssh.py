"""SSH over SSM: the managed ~/.ssh/barn.conf and helpers to run
commands on a box."""

import fcntl
import os
import re
import subprocess
from pathlib import Path

from .config import PUBLIC_KEY

# Also installed by first boot; one implementation of the authorized_keys blocks.
AUTHORIZED_KEYS_PY = Path(__file__).parent / "bootstrap" / "authorized_keys.py"

INCLUDE = "Include barn.conf"
BEGIN = "# >>> barn {name} >>>"
END = "# <<< barn {name} <<<"


def _replace_block(text, name, body):
    """Replace (or append, or with body=None remove) a marked block in text."""
    begin, end = BEGIN.format(name=name), END.format(name=name)
    pattern = re.compile(rf"^{re.escape(begin)}\n.*?^{re.escape(end)}\n?", re.S | re.M)
    text = pattern.sub("", text)
    if body is None:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}{begin}\n{body.rstrip()}\n{end}\n"


def host_entry(alias, instance_id, user, identity_file, aws_profile, region):
    return f"""Host {alias}
  HostName {instance_id}
  User {user}
  IdentityFile {identity_file}
  IdentitiesOnly yes
  HostKeyAlias {alias}
  StrictHostKeyChecking accept-new
  ServerAliveInterval 30
  ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p --profile {aws_profile} --region {region}
"""


def write_host(alias, entry, ssh_dir=None):
    """Write (entry) or remove (entry=None) one box's block."""
    ssh_dir = Path(ssh_dir or Path.home() / ".ssh")
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    managed = ssh_dir / "barn.conf"
    # Several barn runs (people, boxes) may share this file: lock, then replace atomically.
    with open(ssh_dir / ".barn.conf.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = managed.read_text() if managed.exists() else ""
        tmp = managed.with_name(".barn.conf.tmp")
        tmp.write_text(_replace_block(old, alias, entry))
        tmp.chmod(0o600)
        os.replace(tmp, managed)

    config = ssh_dir / "config"
    text = config.read_text() if config.exists() else ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != INCLUDE:
        lines = [INCLUDE] + [line for line in lines if line.strip() != INCLUDE]
        config.write_text("\n".join(lines) + "\n")
        config.chmod(0o600)


def forget_host_key(alias):
    """Drop alias from known_hosts. Only for a deleted home volume: the saved host
    keys live on it, so the next box under this alias has a new identity."""
    subprocess.run(["ssh-keygen", "-R", alias], capture_output=True)


def run(alias, script, timeout=120, check=False, tty=False):
    """Run a bash script on the box; returns CompletedProcess with text output."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    if tty:
        cmd += ["-tt"]
        cmd += [alias, script]
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check)
    cmd += [alias, "bash -s"]
    return subprocess.run(cmd, input=script, text=True, capture_output=True, timeout=timeout, check=check)


def reachable(alias, timeout=60):
    try:
        return run(alias, "true", timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def authorized_keys_script(block, keys):
    """A bash script (run as the box user) that makes `block` of
    ~/.ssh/authorized_keys hold exactly keys and leaves the rest alone."""
    for k in keys:
        if not PUBLIC_KEY.match(k):
            raise ValueError(f"not a one-line SSH public key: {k!r}")
    source = AUTHORIZED_KEYS_PY.read_text()
    keys_text = "".join(f"{k}\n" for k in keys)
    return f"""set -euo pipefail
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/authorized_keys.py" <<'BARN_AK_PY'
{source}BARN_AK_PY
cat > "$tmp/keys" <<'BARN_KEYS'
{keys_text}BARN_KEYS
install -d -m 0700 ~/.ssh
python3 "$tmp/authorized_keys.py" ~/.ssh/authorized_keys {block} "$tmp/keys"
"""
