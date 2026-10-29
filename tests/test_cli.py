import pytest

from barn.cli import build_parser, parse_duration
from barn.config import UBUNTU_AMI, ConfigError, check_env_name, load_account, load_settings
from barn.sync import credential_files, rsync_cmd
from barn.tf import PACKAGE, install_roots


def test_parse_duration():
    assert parse_duration("30m") == 1800
    assert parse_duration("8h") == 8 * 3600
    assert parse_duration("2d") == 2 * 86400
    with pytest.raises(ConfigError):
        parse_duration("8 hours")


def test_env_names():
    assert check_env_name("dev-2") == "dev-2"
    for bad in ("Dev", "2dev", "a_b", ""):
        with pytest.raises(ConfigError):
            check_env_name(bad)


def test_parser_commands():
    p = build_parser()
    assert p.parse_args(["create", "dev", "--size", "t3.micro"]).size == "t3.micro"
    assert p.parse_args(["destroy", "dev", "--volume"]).volume
    assert p.parse_args(["hold", "dev", "8h"]).duration == "8h"


ACCOUNT = (
    'region = "us-east-1"\nstate_bucket = "b"\nexecution_instance_profile = "ip"\n'
    'security_group_id = "sg-1"\nsubnet_ids = ["subnet-1"]\n'
)


def test_load_settings_and_account(tmp_path, monkeypatch):
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    (tmp_path / "accounts").mkdir()
    (tmp_path / "config.toml").write_text('account = "personal"\naws_profile = "admin"\nuser = "zach"\n')
    (tmp_path / "accounts" / "personal.toml").write_text(ACCOUNT + '[extra_tags]\n"cost-center" = "42"\n')
    s = load_settings(tmp_path)
    assert s.default_size == "m7i.xlarge" and s.shared_home_path is None
    a = load_account(tmp_path, s.account)
    assert a.name == "personal" and a.extra_tags == {"cost-center": "42"}
    assert a.ami_ssm_parameter == UBUNTU_AMI and a.developer_role_arn == ""


def test_load_settings_rejects_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    (tmp_path / "config.toml").write_text('account = "a"\naws_profile = "p"\ntypo = 1\n')
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_load_account_rejects_missing_and_stale_keys(tmp_path):
    (tmp_path / "accounts").mkdir()
    (tmp_path / "accounts" / "a.toml").write_text('region = "us-east-1"\n')
    with pytest.raises(ConfigError, match="missing required keys"):
        load_account(tmp_path, "a")
    # Written by the old platform output; the CLI no longer assumes roles.
    (tmp_path / "accounts" / "a.toml").write_text(ACCOUNT + 'developer_policy_role_arn = "arn:r"\n')
    with pytest.raises(ConfigError, match="unknown keys"):
        load_account(tmp_path, "a")


def test_install_roots_mirrors_package(tmp_path):
    install_roots(tmp_path)
    assert (tmp_path / "terraform" / "instance" / "main.tf").read_bytes() == (
        PACKAGE / "terraform" / "instance" / "main.tf"
    ).read_bytes()
    assert (tmp_path / "bootstrap" / "user-data.sh.tftpl").is_file()
    # The instance root reads bootstrap files relative to itself.
    assert (tmp_path / "terraform" / "instance" / ".." / ".." / "bootstrap" / "idle-check.sh").is_file()

    stale = tmp_path / "terraform" / "volume" / "old.tf"
    stale.write_text("")
    edited = tmp_path / "terraform" / "volume" / "main.tf"
    edited.write_text("# edited")
    install_roots(tmp_path)
    assert not stale.exists()
    assert edited.read_bytes() == (PACKAGE / "terraform" / "volume" / "main.tf").read_bytes()


def test_credential_files(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    assert credential_files(home) == []
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / "gitlab_token.txt").write_text("x")
    assert sorted(map(str, credential_files(home))) == ["home/.claude/.credentials.json", "home/gitlab_token.txt"]


def test_rsync_never_deletes_or_touches_home_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "README.md").write_text("docs")
    (home / ".gitconfig").write_text("")
    (home / ".claude").mkdir()
    cmd = rsync_cmd(home, "zach-dev")
    assert not any("delete" in c for c in cmd if c.startswith("-"))
    assert cmd[-3:] == [str(home / ".claude"), str(home / ".gitconfig"), "zach-dev:/home/ubuntu/"]
    assert not any(c.endswith("README.md") or c.endswith("/") and c.startswith(str(home)) for c in cmd)


def test_rsync_empty_source(tmp_path):
    assert rsync_cmd(tmp_path, "a") is None
