"""Account file (accounts/<name>.toml) and local settings (.agent-platform.local.toml)."""

import getpass
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LOCAL_SETTINGS = ".agent-platform.local.toml"
# The login user on every box: Ubuntu's default user (UID 1000, from cloud-init).
BOX_USER = "ubuntu"
ENV_NAME = re.compile(r"^[a-z][a-z0-9-]{0,20}$")


class ConfigError(Exception):
    pass


def repo_root(start=None):
    """The directory holding accounts/ and terraform/; searched upward from cwd."""
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "terraform" / "instance").is_dir() and (d / "accounts").is_dir():
            return d
    raise ConfigError("run this from inside the agent-platform repo")


@dataclass
class Account:
    name: str
    region: str
    state_bucket: str
    execution_instance_profile: str
    security_group_id: str
    subnet_ids: list
    ami_ssm_parameter: str
    developer_role_arn: str = ""
    developer_policy_role_arn: str = ""
    kms_key_id: str = ""
    extra_tags: dict = field(default_factory=dict)

    @property
    def role_mode(self):
        return "create" if self.developer_role_arn else "attach"


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

    @property
    def public_key_path(self):
        return Path(self.ssh_public_key).expanduser()

    @property
    def private_key_path(self):
        p = self.public_key_path
        return p.with_suffix("") if p.suffix == ".pub" else p


def load_settings(root):
    # AGENT_PLATFORM_SETTINGS lets one checkout act as several developers (testing).
    path = Path(os.environ.get("AGENT_PLATFORM_SETTINGS") or root / LOCAL_SETTINGS)
    if not path.exists():
        raise ConfigError(f"missing {path}; copy .agent-platform.local.toml.example and edit it")
    data = tomllib.loads(path.read_text())
    data.setdefault("user", os.environ.get("USER") or getpass.getuser())
    known = Settings.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{path}: unknown settings {sorted(unknown)}")
    for key in ("account", "aws_profile"):
        if not data.get(key):
            raise ConfigError(f"{path}: '{key}' is required")
    return Settings(**data)


def load_account(root, name):
    path = root / "accounts" / f"{name}.toml"
    if not path.exists():
        raise ConfigError(f"missing account file {path}")
    data = tomllib.loads(path.read_text())
    data.pop("name", None)
    known = Account.__dataclass_fields__
    return Account(name=name, **{k: v for k, v in data.items() if k in known})


def check_env_name(env):
    if not ENV_NAME.match(env):
        raise ConfigError(f"bad environment name {env!r}: lowercase letters, digits, dashes; start with a letter")
    return env
