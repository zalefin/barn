"""`barn init`: create the barn home from templates without overwriting anything,
then check the tools and list the files the user still has to edit."""

import getpass
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from string import Template

import boto3
from rich.markup import escape

from . import ui
from .config import PLACEHOLDER, RENAMED, ConfigError, account_names, check_account_name, placeholders
from .sync import short

TEMPLATES = Path(__file__).parent / "templates"
SSH_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")
MIN_TERRAFORM = (1, 10)
SSM_PLUGIN_DOCS = "https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"


def find_ssh_key():
    """The first usual public key that exists, as a ~ path, or None."""
    for name in SSH_KEYS:
        if (Path.home() / ".ssh" / name).exists():
            return f"~/.ssh/{name}"
    return None


def _write_new(path, text, mode=0o644):
    """Write path only if it does not exist. Returns whether it was written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return True


def scaffold(home, account=None, aws_profile=None, account_file=None):
    """Create what is missing in home. Returns [(path, action, detail)]. A new
    config.toml makes the account its default_account; with an existing one, the
    account file is added and todo() asks for its profile."""
    if account_file:
        account_file = Path(account_file).expanduser()
        if not account_file.is_file():
            raise ConfigError(f"no such account file: {account_file}")
    config = home / "config.toml"
    if config.exists() and not account and not account_file:
        try:
            account = tomllib.loads(config.read_text()).get("default_account")
        except tomllib.TOMLDecodeError:
            pass  # todo() reports it
    account = check_account_name(account or (account_file.stem if account_file else "default"), "--account")

    done = []
    text = Template((TEMPLATES / "config.toml").read_text()).substitute(
        account=account,
        aws_profile=aws_profile or PLACEHOLDER,
        user=os.environ.get("USER") or getpass.getuser(),
        ssh_public_key=find_ssh_key() or "~/.ssh/id_ed25519.pub",
    )
    done.append((config, "created" if _write_new(config, text) else "kept", ""))

    acct = home / "accounts" / f"{account}.toml"
    source = account_file.read_text() if account_file else (TEMPLATES / "account.toml").read_text()
    if _write_new(acct, source):
        done.append((acct, "copied" if account_file else "created", f"from {short(account_file)}" if account_file else ""))
    else:
        done.append((acct, "kept", ""))

    env = home / "home" / ".barn" / "env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.parent.chmod(0o700)
    done.append((env, "created" if _write_new(env, (TEMPLATES / "env").read_text(), 0o600) else "kept", ""))
    return done


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def tool_checks():
    """[(status, tool, detail)] for what barn runs locally."""
    results = []
    if shutil.which("terraform"):
        v = json.loads(_run("terraform", "version", "-json") or "{}").get("terraform_version", "")
        ok = tuple(int(p) for p in v.split(".")[:2] if p.isdigit()) >= MIN_TERRAFORM
        results.append(("pass" if ok else "fail", "terraform", v if ok else f"{v or 'unknown'}; need >= 1.10"))
    else:
        results.append(("fail", "terraform", "not found; https://developer.hashicorp.com/terraform/install"))
    aws = _run("aws", "--version").split(" ")[0] if shutil.which("aws") else ""
    results.append(("pass", "aws cli", aws) if aws else ("fail", "aws cli", "not found; install AWS CLI v2"))
    results.append(
        ("pass", "session-manager-plugin", "")
        if shutil.which("session-manager-plugin")
        else ("fail", "session-manager-plugin", f"not found; {SSM_PLUGIN_DOCS}")
    )
    for tool in ("ssh", "rsync"):
        results.append(("pass", tool, "") if shutil.which(tool) else ("fail", tool, "not found"))
    return results


def _toml(path, items):
    """Parsed path, or None with a to-do item when it is not valid TOML."""
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        items.append((True, path, f"not valid TOML: {e}"))
        return None


def todo(home):
    """[(required, path, what)] still to do by hand."""
    items = []
    config = home / "config.toml"
    settings = _toml(config, items)
    if settings is None:
        return items
    left = placeholders(settings)
    if left:
        items.append((True, config, f"set {', '.join(left)}"))
    for old in ("account", "aws_profile"):
        if old in settings:
            items.append((True, config, RENAMED[old]))
    key = Path(settings.get("ssh_public_key", "~/.ssh/id_ed25519.pub")).expanduser()
    if not key.exists():
        items.append((True, key, "create it: ssh-keygen -t ed25519 (or change ssh_public_key)"))

    default = settings.get("default_account")
    profiles = settings.get("aws_profiles", {})
    profiles = profiles if isinstance(profiles, dict) else {}
    known = boto3.Session().available_profiles
    for name in sorted({*account_names(home), *([default] if isinstance(default, str) else [])}):
        acct = home / "accounts" / f"{name}.toml"
        if not acct.exists():
            items.append((True, acct, "missing; copy your account file here (or change default_account)"))
            continue
        if (data := _toml(acct, items)) is not None:
            left = placeholders(data)
            if left:
                items.append((True, acct, f"fill in {', '.join(left)} from whoever set up the account"))
        profile = profiles.get(name, "")
        if not profile:
            items.append((True, config, f'add `{name} = "<profile>"` under [aws_profiles]'))
        elif profile != PLACEHOLDER and profile not in known:
            items.append((True, Path("~/.aws/config"), f"add profile {profile!r} (or change aws_profiles.{name})"))

    env = home / "home" / ".barn" / "env"
    if not any(line.strip().startswith("export ") for line in env.read_text().splitlines()):
        items.append((False, env, "add CLAUDE_CODE_OAUTH_TOKEN and KIRO_API_KEY, then `barn sync <name>`"))
    return items


def init(home, account=None, aws_profile=None, account_file=None):
    created = scaffold(home, account, aws_profile, account_file)
    ui.header("barn home", short(home))
    for path, action, detail in created:
        rel = str(path.relative_to(home))
        (ui.info if action == "kept" else ui.done)(f"{action:<7} {rel}", detail)

    ui.err.print("\n[name]Tools[/]")
    tools = tool_checks()
    for status, name, detail in tools:
        ui.check(status, name, detail)

    items = todo(home)
    required = [i for i in items if i[0]]
    ui.err.print("\n[name]To do[/]" if items else "")
    for n, (needed, path, what) in enumerate(items, 1):
        label = f"{n}." if needed else "·"
        ui.err.print(f" [accent]{label}[/] {escape(short(path))}  [muted]{escape(what)}{'' if needed else ' (optional)'}[/]")

    ui.err.print()
    if required or any(s == "fail" for s, _, _ in tools):
        ui.err.print("[muted]Edit the files above, then run[/] [accent]barn init[/] [muted]again to check.[/]")
    else:
        ui.err.print("[ok]●[/] ready   [accent]barn list[/]  [muted]·[/]  [accent]barn create <name>[/] [muted](managers)[/]")
    return 0
