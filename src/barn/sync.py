"""`sync <env>`: push the optional shared_home, then <barn home>/home/,
onto the box (PLAN.md section 9.2).

Local files win; remote files are never deleted; paths not in those
directories are never touched.
"""

import fnmatch
import subprocess
import sys

from . import ssh
from .config import BOX_USER

CREDENTIAL_PATTERNS = (".credentials.json", "data.sqlite3", "*token*", "*.pem")
# Processes whose live state files (Kiro's SQLite DB, Claude's credentials) a sync would race.
AGENT_PROCESSES = "claude|kiro-cli|kiro-cli-chat"


def credential_files(home_dir):
    """Files under the shared home that look like credentials."""
    hits = []
    for path in sorted(home_dir.rglob("*")):
        if path.is_file() and any(fnmatch.fnmatch(path.name, p) for p in CREDENTIAL_PATTERNS):
            hits.append(path.relative_to(home_dir.parent))
    return hits


def running_agents(alias):
    r = ssh.run(alias, f"pgrep -u \"$USER\" -x '{AGENT_PROCESSES}' -a || true")
    if r.returncode != 0:
        raise RuntimeError(f"cannot reach {alias}: {r.stderr.strip()}")
    return r.stdout.strip()


def rsync_cmd(src, alias):
    """rsync each top-level entry of src rather than `src/`, so the remote home
    directory's own mode is never changed (sshd refuses a group-writable home)."""
    entries = sorted(str(p) for p in src.iterdir() if p.name != "README.md")
    if not entries:
        return None
    return [
        "rsync",
        "-rlpt",  # no --delete: remote-only files stay
        "--itemize-changes",
        *entries,
        f"{alias}:/home/{BOX_USER}/",
    ]


def sources(ctx):
    """Directories synced onto the box, in order (later ones win)."""
    shared = ctx.settings.shared_home_path
    return [d for d in (shared, ctx.home / "home") if d and d.is_dir()]


def sync(ctx, env):
    alias = ctx.alias(env)
    shared = ctx.settings.shared_home_path

    hits = credential_files(shared) if shared and shared.is_dir() else []
    if hits:
        print(f"refusing to sync: credential-looking files in the shared home {shared}:", file=sys.stderr)
        for h in hits:
            print(f"  {h}  (move it to {ctx.home / 'home'})", file=sys.stderr)
        return 1

    busy = running_agents(alias)
    if busy:
        print("refusing to sync: agents are running on the box (stop them first):", file=sys.stderr)
        print(busy, file=sys.stderr)
        return 1

    for src in sources(ctx):
        cmd = rsync_cmd(src, alias)
        if cmd is None:
            continue
        print(f"syncing {src}/ -> {alias}:~/", file=sys.stderr)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            return r.returncode
    return 0
