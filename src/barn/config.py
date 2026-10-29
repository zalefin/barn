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
ENV_NAME = re.compile(r"^[a-z][a-z0-9-]{0,20}$")
UBUNTU_AMI = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"


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
    developer_role_arn: str = ""
    kms_key_id: str = ""
    extra_tags: dict = field(default_factory=dict)


@dataclass
class Settings:
    account: str
    aws_profile: str
    user: str
    ssh_public_key: str = "~/.ssh/id_ed25519.pub"
    default_size: str = "m7i.xlarge"
    passwordless_sudo: bool = True
    idle_hours: float = 4
    volume_gb: int = 100
    shared_home: str = ""

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


def _build(cls, path, data, **fixed):
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(fields) - set(fixed)
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
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
    return cls(**data, **fixed)


def load_settings(home):
    # BARN_SETTINGS lets one machine act as several developers (testing).
    path = Path(os.environ.get("BARN_SETTINGS") or home / "config.toml")
    if not path.exists():
        raise ConfigError(f"missing {path}; see the README (Setup)")
    data = tomllib.loads(path.read_text())
    data.setdefault("user", os.environ.get("USER") or getpass.getuser())
    return _build(Settings, path, data)


def load_account(home, name):
    path = home / "accounts" / f"{name}.toml"
    if not path.exists():
        raise ConfigError(f"missing account file {path}; see docs/account-prerequisites.md")
    data = tomllib.loads(path.read_text())
    data.pop("name", None)
    return _build(Account, path, data, name=name)


def check_env_name(env):
    if not ENV_NAME.match(env):
        raise ConfigError(f"bad environment name {env!r}: lowercase letters, digits, dashes; start with a letter")
    return env
