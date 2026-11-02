"""Automatic upgrades of the barn home.

config.toml records `config_version`: the barn version whose format it is in
(written by `barn init`, or by the last migration). When a newer barn loads it,
every migration registered at a version above config_version, up to the
installed version, runs in order; the old file is kept as
config.toml.<old version>.bak and config_version becomes the installed version.
A file without config_version predates versioning (0.1.0). A file from a newer
barn than the installed one is an error, never downgraded.

Every release that changes config.toml's format or the home's layout ships a
migration here, registered at that release's version (README: Versions).
Migrations edit config.toml as text, so comments and layout survive, and must
be idempotent: a file may already be partly in the new format (edited by hand).
"""

import json
import os
import re
import shutil
import tomllib
from importlib.metadata import version as dist_version

from . import ui
from .config import ConfigError

KEY = "config_version"
PRE_VERSIONING = "0.1.0"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# [(version tuple, version, function)], in version order. function(home, text)
# returns (new text, [notes]) and may also change files in home.
MIGRATIONS = []


def parse(version):
    m = SEMVER.match(version) if isinstance(version, str) else None
    if not m:
        raise ConfigError(f"bad version {version!r}: expected MAJOR.MINOR.PATCH")
    return tuple(int(p) for p in m.groups())


def installed():
    return dist_version("barn")


def migration(version):
    def register(fn):
        MIGRATIONS.append((parse(version), version, fn))
        MIGRATIONS.sort(key=lambda m: m[0])
        return fn

    return register


# --- editing config.toml as text (top-level keys with one-line values) ---

TABLE_HEADER = re.compile(r"^\s*\[")


def _key_re(key):
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def _top_level(lines):
    """Index of the first table header: keys before it are top-level."""
    return next((i for i, line in enumerate(lines) if TABLE_HEADER.match(line)), len(lines))


def _find(lines, key):
    end = _top_level(lines)
    return next((i for i in range(end) if _key_re(key).match(lines[i])), None)


def rename_key(text, old, new):
    """Rename a top-level key, keeping its value and trailing comment."""
    lines = text.splitlines(keepends=True)
    i = _find(lines, old)
    if i is not None:
        lines[i] = re.sub(rf"^(\s*){re.escape(old)}(\s*=)", rf"\g<1>{new}\g<2>", lines[i], count=1)
    return "".join(lines)


def remove_key(text, key):
    lines = text.splitlines(keepends=True)
    i = _find(lines, key)
    if i is not None:
        del lines[i]
    return "".join(lines)


def set_key(text, key, value, comment=""):
    """Set a top-level key to a string value: in place if present, else as the
    first key (after the file's leading comments)."""
    line = f"{key} = {json.dumps(value)}" + (f"  # {comment}" if comment else "") + "\n"
    lines = text.splitlines(keepends=True)
    i = _find(lines, key)
    if i is not None:
        lines[i] = line
    else:
        at = next((n for n, raw in enumerate(lines) if raw.strip() and not raw.lstrip().startswith("#")), len(lines))
        lines.insert(at, line)
    return "".join(lines)


def add_table_entries(text, table, entries):
    """Add string entries to [table], creating it at the end of the file. Keys
    the table already has are left alone."""
    have = tomllib.loads(text).get(table, {})
    new = {k: v for k, v in entries.items() if k not in have}
    if not new:
        return text
    body = "".join(f"{k} = {json.dumps(v)}\n" for k, v in new.items())
    lines = text.splitlines(keepends=True)
    header = next((i for i, raw in enumerate(lines) if raw.strip() == f"[{table}]"), None)
    if header is None:
        return text.rstrip("\n") + f"\n\n[{table}]\n{body}"
    lines.insert(header + 1, body)
    return "".join(lines)


# --- migrations ---


@migration("0.2.0")
def multiple_accounts(home, text):
    """`account` became `default_account`, `aws_profile` an [aws_profiles] entry
    for that account, and terraform data dirs moved from state/<name>/ to
    state/<account>/<name>/."""
    notes = []
    data = tomllib.loads(text)
    account = data.get("account")
    if isinstance(account, str) and "default_account" not in data:
        text = rename_key(text, "account", "default_account")
        notes.append(f"account → default_account = {account!r}")
    account = account or data.get("default_account")
    profile = data.get("aws_profile")
    if isinstance(profile, str):
        text = remove_key(text, "aws_profile")
        if isinstance(account, str):
            text = add_table_entries(text, "aws_profiles", {account: profile})
            notes.append(f"aws_profile → [aws_profiles] {account} = {profile!r}")

    state = home / "state"
    if isinstance(account, str) and state.is_dir():
        for box in sorted(state.iterdir()):
            old_layout = any((box / root / f).exists() for root in ("volume", "instance")
                             for f in ("terraform.tfstate", "vars.tfvars.json", "apply.log"))
            target = state / account / box.name
            if box.name in ("plugin-cache", account) or not old_layout:
                continue
            if target.exists():
                notes.append(f"left {box} in place: {target} exists")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(box, target)
            notes.append(f"state/{box.name}/ → state/{account}/{box.name}/")
    return text, notes


# --- running them ---


def migrate(home, path, now=None):
    """Bring the config file at path (and home) up to the installed version.
    Returns the versions of the migrations that ran."""
    now = now or installed()
    try:
        text = path.read_text()
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError):
        return []  # load_settings reports it
    have = data.get(KEY, PRE_VERSIONING)
    try:
        have_v = parse(have)
    except ConfigError as e:
        raise ConfigError(f"{path}: {KEY}: {e}") from None
    if have_v > parse(now):
        raise ConfigError(
            f"{path} is from barn {have}, newer than this barn ({now}); upgrade: `uv tool upgrade barn`"
        )
    pending = [m for m in MIGRATIONS if have_v < m[0] <= parse(now)]
    if not pending:
        return []

    backup = path.with_name(f"{path.name}.{have}.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    notes = []
    for _, version, fn in pending:
        text, more = fn(home, text)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(
                f"migrating {path} to {version} produced invalid TOML ({e}); the file is unchanged, edit it by hand"
            ) from None
        notes += more
    text = set_key(text, KEY, now, "the barn version this file's format is from; barn migrates it on upgrade")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.chmod(path.stat().st_mode & 0o777)
    os.replace(tmp, path)

    ui.info(f"migrated {path.name} from barn {have} to {now}", f"old file kept as {backup.name}")
    for note in notes:
        ui.info(f"  {note}")
    return [version for _, version, _ in pending]
