"""`sync <name>`: push the account's guest_keys into the box's authorized_keys, then the
optional shared_home and <barn home>/home/ onto the box (docs/plans/PLAN.md 9.2, 23.4).

Local files win; remote files are never deleted; paths not in those
directories are never touched. Files go as a tar stream over ssh (no rsync, so
Linux and Windows work alike): only those whose content or mode differ from
the box's copy, text files with LF line endings, keeping their mtimes. The one deletion is a guest key removed from
guest_keys: the guests block of authorized_keys is rewritten wholesale.
"""

import base64
import fnmatch
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

from . import compat, ssh, ui
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


# Where synced files keep their mode on Windows, which has none: private ones.
PRIVATE_DIRS = (".ssh", ".aws", ".barn")


def overlay(srcs):
    """{relative path (with /): local path} of every file to sync; later
    sources win. A source's top-level README.md is not synced."""
    files = {}
    for src in srcs:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src).as_posix()
            if rel != "README.md" and (p.is_symlink() or p.is_file()):
                files[rel] = p
    return files


def file_mode(rel, path, data):
    """The mode a file gets on the box: its own on Linux; on Windows 600 for
    credentials and private directories, 755 for scripts, else 644."""
    if not compat.WINDOWS:
        return path.stat().st_mode & 0o777
    if rel.split("/")[0] in PRIVATE_DIRS or any(fnmatch.fnmatch(path.name, p) for p in CREDENTIAL_PATTERNS):
        return 0o600
    return 0o755 if data.startswith(b"#!") else 0o644


def local_entries(files):
    """{rel: (manifest entry, file data or None, mtime)}; entries compare to the box's."""
    out = {}
    for rel, path in files.items():
        if path.is_symlink():
            out[rel] = (["l", os.readlink(path)], None, 0)
            continue
        data = compat.lf(path.read_bytes())
        entry = ["f", hashlib.sha256(data).hexdigest(), file_mode(rel, path, data)]
        out[rel] = (entry, data, int(path.stat().st_mtime))
    return out


# Run on the box: the same manifest entries for the paths that exist there.
MANIFEST = """python3 - <<'BARN_PY'
import base64, hashlib, json, os, stat
home = os.path.expanduser("~")
out = {}
for rel in json.loads(base64.b64decode("PATHS")):
    path = os.path.join(home, rel)
    try:
        st = os.lstat(path)
    except OSError:
        continue
    if stat.S_ISLNK(st.st_mode):
        out[rel] = ["l", os.readlink(path)]
    elif stat.S_ISREG(st.st_mode):
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        out[rel] = ["f", digest, stat.S_IMODE(st.st_mode)]
    else:
        out[rel] = ["other"]
print(json.dumps(out))
BARN_PY
"""


def remote_entries(alias, paths):
    payload = base64.b64encode(json.dumps(sorted(paths)).encode()).decode()
    r = ssh.run(alias, MANIFEST.replace("PATHS", payload), timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read files on {alias}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def changed(local, remote):
    """Paths whose entry differs on the box (or that it does not have)."""
    return sorted(rel for rel, (entry, _, _) in local.items() if remote.get(rel) != entry)


def archive(local, paths):
    """A gzipped tar of paths, with their mtimes and modes. No directory entries:
    existing directories (the home itself, ~/.ssh) keep their modes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for rel in paths:
            entry, data, mtime = local[rel]
            info = tarfile.TarInfo(rel)
            if entry[0] == "l":
                info.type, info.linkname = tarfile.SYMTYPE, entry[1]
                tar.addfile(info)
            else:
                info.size, info.mtime, info.mode = len(data), mtime, entry[2]
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def push_files(alias, srcs):
    """Copy the overlay onto the box. Returns the paths that were updated."""
    local = local_entries(overlay(srcs))
    paths = changed(local, remote_entries(alias, local)) if local else []
    if paths:
        r = ssh.pipe(alias, "tar -xzpf - -C ~", archive(local, paths))
        if r.returncode != 0:
            raise RuntimeError(f"copying files to {alias} failed: {r.stderr.strip()}")
    return paths


def short(path):
    try:
        return f"~/{Path(path).relative_to(Path.home()).as_posix()}"
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

    srcs = sources(ctx)
    if not overlay(srcs):
        ui.info("nothing to sync", f"put files in {short(ctx.home / 'home')}")
        return 0

    with ui.step(f"Checking {alias} for running agents"):
        busy = running_agents(alias)
    if busy:
        ui.fail("not syncing files: agents are running on the box; stop them first")
        ui.block(busy)
        return 1
    with ui.step(f"{', '.join(short(s) + '/' for s in srcs)} → {alias}:~/") as st:
        files = push_files(alias, srcs)
        st.result = f"{len(files)} file{'s' * (len(files) != 1)} updated" if files else "up to date"
    if files:
        ui.block("\n".join(files[:20]) + (f"\n… and {len(files) - 20} more" if len(files) > 20 else ""))
    return 0
