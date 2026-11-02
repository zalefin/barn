"""`sync <name>`: push the account's guest_keys into the box's authorized_keys, then the
optional shared_home and <barn home>/home/ onto the box (docs/plans/PLAN.md 9.2, 23.4).

Local files win; remote files are never deleted; paths not in those
directories are never touched. The one deletion is a guest key removed from
guest_keys: the guests block of authorized_keys is rewritten wholesale.
"""

import fnmatch
import subprocess
from pathlib import Path

from . import ssh, ui
from .config import BOX_USER, ConfigError, check_name

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


def changed_files(itemized):
    """Paths rsync --itemize-changes reports as transferred (not just touched)."""
    paths = []
    for line in itemized.splitlines():
        code, _, path = line.partition(" ")
        # e.g. "<f+++++++++ .gitconfig" (new) or "<f.st...... .bashrc" (changed); "cd" lines are directories.
        if len(code) == 11 and code[0] in "<>c" and code[1] == "f":
            paths.append(path)
    return paths


def short(path):
    try:
        return f"~/{Path(path).relative_to(Path.home())}"
    except ValueError:
        return str(path)


def sources(ctx):
    """Directories synced onto the box, in order (later ones win)."""
    shared = ctx.settings.shared_home_path
    return [d for d in (shared, ctx.home / "home") if d and d.is_dir()]


def sync(ctx, name):
    from .cli import check_owner, push_guest_keys, write_ssh_entry

    alias = check_name(name)
    with ui.step("Checking owner"):
        check_owner(ctx, name, "sync", hint="it would overwrite their tokens")
        inst = ctx.require_instance(name)
        # The entry may point at an old instance (recreated from another laptop).
        write_ssh_entry(ctx, name, inst["InstanceId"])
    if inst["State"]["Name"] != "running":
        raise ConfigError(f"{name} is {inst['State']['Name']}; run `barn start {name}` first")
    shared = ctx.settings.shared_home_path
    # Before the agent check: revoking a guest must not wait for agents to finish.
    push_guest_keys(ctx, name)

    hits = credential_files(shared) if shared and shared.is_dir() else []
    if hits:
        ui.fail("refusing to sync: credential-looking files in the shared home", short(shared))
        ui.block("\n".join(str(h) for h in hits))
        ui.info("move them to", short(ctx.home / "home"))
        return 1

    with ui.step(f"Checking {alias} for running agents"):
        busy = running_agents(alias)
    if busy:
        ui.fail("not syncing files: agents are running on the box; stop them first")
        ui.block(busy)
        return 1

    srcs = [s for s in sources(ctx) if rsync_cmd(s, alias)]
    if not srcs:
        ui.info("nothing to sync", f"put files in {short(ctx.home / 'home')}")
        return 0
    for src in srcs:
        with ui.step(f"{short(src)}/ → {alias}:~/") as st:
            r = subprocess.run(rsync_cmd(src, alias), text=True, capture_output=True)
            if r.returncode != 0:
                raise RuntimeError(f"rsync failed ({r.returncode}): {r.stderr.strip()}")
            files = changed_files(r.stdout)
            st.result = f"{len(files)} file{'s' * (len(files) != 1)} updated" if files else "up to date"
        if files:
            ui.block("\n".join(files[:20]) + (f"\n… and {len(files) - 20} more" if len(files) > 20 else ""))
    return 0
