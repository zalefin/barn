"""The barn home directory, its settings file (config.toml) and account
files (accounts/<name>.toml). See docs/account-prerequisites.md."""

import dataclasses
import getpass
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The login user on every box: Ubuntu's default user (UID 1000, from cloud-init).
BOX_USER = "ubuntu"
# A box's full name: its SSH alias, hostname and `barn:name` tag (docs/plans/PLAN.md 23.2).
BOX_NAME = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")
# The S3 backend keeps workspaces under env/workspaces/, so a box named workspaces
# would collide; plugin-cache stays reserved from when box state was state/<name>/.
RESERVED_NAMES = {"workspaces", "plugin-cache"}
# An account's name: its file accounts/<name>.toml, a TOML key in [aws_profiles],
# and its directory state/<name>/ (so not plugin-cache).
ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# One OpenSSH public key on one line: type, base64 blob, optional comment.
PUBLIC_KEY = re.compile(r"^(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/]+=* ?[^\r\n]*$")
UBUNTU_AMI = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
# What `barn init` writes for values only the user can know.
PLACEHOLDER = "CHANGE-ME"


class ConfigError(Exception):
    pass


def app_home():
    """Everything the CLI reads and writes locally lives here."""
    if os.environ.get("BARN_HOME"):
        return Path(os.environ["BARN_HOME"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "barn"


@dataclass
class Account:
    name: str
    region: str
    state_bucket: str
    execution_instance_profile: str
    security_group_id: str
    subnet_ids: list
    ami_ssm_parameter: str = UBUNTU_AMI
    manager_role_arn: str = ""
    kms_key_id: str = ""
    extra_tags: dict = field(default_factory=dict)


@dataclass
class Settings:
    default_account: str
    user: str
    # {account name: AWS CLI profile}; each account is a different AWS account.
    aws_profiles: dict = field(default_factory=dict)
    ssh_public_key: str = "~/.ssh/id_ed25519.pub"
    default_size: str = "m7i.xlarge"
    passwordless_sudo: bool = True
    idle_hours: float = 4
    volume_gb: int = 100
    shared_home: str = ""
    guest_keys: list = field(default_factory=list)

    @property
    def public_key_path(self):
        return Path(self.ssh_public_key).expanduser()

    @property
    def private_key_path(self):
        p = self.public_key_path
        return p.with_suffix("") if p.suffix == ".pub" else p

    @property
    def shared_home_path(self):
        return Path(self.shared_home).expanduser() if self.shared_home else None

    def aws_profile(self, account):
        profile = self.aws_profiles.get(account)
        if not profile:
            raise ConfigError(
                f"no AWS profile for account {account!r}; add `{account} = \"<profile>\"` "
                "under [aws_profiles] in config.toml"
            )
        return profile


def placeholders(data):
    """Keys whose value (any list item, or any value of a table, as table.key) is
    still a `barn init` placeholder."""
    found = []
    for k, v in data.items():
        if isinstance(v, dict):
            found += [f"{k}.{sub}" for sub in placeholders(v)]
        elif PLACEHOLDER in (v if isinstance(v, list) else [v] if isinstance(v, str) else []):
            found.append(k)
    return found


# Keys an older barn wrote, and what replaced them.
RENAMED = {
    "account": "`account` is now `default_account`",
    "aws_profile": '`aws_profile` moved to a table: [aws_profiles] then `<account> = "<profile>"`',
}


def _build(cls, path, data, **fixed):
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(fields) - set(fixed)
    if unknown:
        hints = [RENAMED[k] for k in sorted(unknown) if k in RENAMED and cls is Settings]
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}" + (f" ({'; '.join(hints)})" if hints else ""))
    missing = [
        n
        for n, f in fields.items()
        if n not in fixed
        and not data.get(n)
        and f.default is dataclasses.MISSING
        and f.default_factory is dataclasses.MISSING
    ]
    if missing:
        raise ConfigError(f"{path}: missing required keys {missing}")
    todo = placeholders(data)
    if todo:
        raise ConfigError(f"{path}: fill in {', '.join(todo)} (`barn init` lists what is left)")
    return cls(**data, **fixed)


def load_settings(home):
    # BARN_SETTINGS lets one machine act as several people (testing).
    path = Path(os.environ.get("BARN_SETTINGS") or home / "config.toml")
    if not path.exists():
        raise ConfigError(f"missing {path}; run `barn init`")
    from .migrate import migrate  # imports this module

    migrate(home, path)
    data = tomllib.loads(path.read_text())
    data.pop("config_version", None)  # checked by migrate
    data.setdefault("user", os.environ.get("USER") or getpass.getuser())
    settings = _build(Settings, path, data)
    check_account_name(settings.default_account, f"{path}: default_account")
    if not isinstance(settings.aws_profiles, dict) or not all(
        isinstance(v, str) for v in settings.aws_profiles.values()
    ):
        raise ConfigError(f'{path}: [aws_profiles] must map account names to profile names (sandbox = "my-profile")')
    keys = settings.guest_keys
    if not isinstance(keys, list):
        raise ConfigError(f"{path}: guest_keys must be a list of public keys")
    bad = [k for k in keys if not isinstance(k, str) or not PUBLIC_KEY.match(k.strip())]
    if bad:
        raise ConfigError(f"{path}: guest_keys entries are not one-line SSH public keys: {bad}")
    settings.guest_keys = [k.strip() for k in keys]
    return settings


def account_names(home):
    """Every account with a file in accounts/, sorted."""
    return sorted(p.stem for p in (home / "accounts").glob("*.toml") if ACCOUNT_NAME.match(p.stem))


def check_account_name(name, what="account"):
    if not isinstance(name, str) or not ACCOUNT_NAME.match(name) or name == "plugin-cache":
        raise ConfigError(f"{what}: bad account name {name!r}; use letters, digits, dashes and underscores")
    return name


def load_account(home, name):
    check_account_name(name)
    path = home / "accounts" / f"{name}.toml"
    if not path.exists():
        raise ConfigError(f"missing account file {path}; run `barn init`")
    data = tomllib.loads(path.read_text())
    data.pop("name", None)
    return _build(Account, path, data, name=name)


def check_name(name):
    if not BOX_NAME.match(name) or name in RESERVED_NAMES:
        raise ConfigError(
            f"bad box name {name!r}: 2 to 40 lowercase letters, digits and dashes; "
            "start with a letter, end with a letter or digit"
            + (f"; {name!r} is reserved" if name in RESERVED_NAMES else "")
        )
    return name
