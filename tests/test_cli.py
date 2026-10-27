import pytest

from agent_platform.cli import build_parser, parse_duration
from agent_platform.config import ConfigError, check_env_name, load_account, load_settings
from agent_platform.sync import credential_files, rsync_cmd


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


def test_load_settings_and_account(tmp_path):
    (tmp_path / "accounts").mkdir()
    (tmp_path / ".agent-platform.local.toml").write_text('account = "personal"\naws_profile = "admin"\nuser = "zach"\n')
    (tmp_path / "accounts" / "personal.toml").write_text(
        'name = "personal"\nregion = "us-east-1"\nstate_bucket = "b"\ndeveloper_role_arn = "arn:r"\n'
        'execution_instance_profile = "ip"\nsecurity_group_id = "sg-1"\nsubnet_ids = ["subnet-1"]\n'
        'ami_ssm_parameter = "/x"\nkms_key_id = ""\n[extra_tags]\n"cost-center" = "42"\n'
    )
    s = load_settings(tmp_path)
    assert s.default_size == "m7i.xlarge"
    a = load_account(tmp_path, s.account)
    assert a.role_mode == "create" and a.extra_tags == {"cost-center": "42"}


def test_load_settings_rejects_unknown_keys(tmp_path):
    (tmp_path / ".agent-platform.local.toml").write_text('account = "a"\naws_profile = "p"\ntypo = 1\n')
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


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
