import pytest

from typer.testing import CliRunner

from barn import cli
from barn.cli import app, parse_duration
from barn.config import UBUNTU_AMI, ConfigError, check_env_name, load_account, load_settings
from barn.sync import changed_files, credential_files, rsync_cmd
from barn.tf import PACKAGE, install_roots, summary
from barn.ui import elapsed


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


@pytest.fixture
def calls(monkeypatch):
    """Record what each command would do, without AWS."""
    seen = []
    monkeypatch.setattr(cli, "Context", lambda verbose=False: f"ctx(verbose={verbose})")
    for name in ("create", "destroy", "hold"):
        monkeypatch.setattr(cli, name, lambda *a, _n=name: seen.append((_n, *a)) or 0)
    monkeypatch.setattr(cli, "check_env_name", lambda env: env)
    monkeypatch.setattr(cli.os, "execvp", lambda *a: seen.append(("execvp", *a)))
    return seen


def test_commands_parse(calls):
    run = CliRunner().invoke
    assert run(app, ["create", "dev", "--size", "t3.micro", "-v"]).exit_code == 0
    assert run(app, ["destroy", "dev", "--volume"]).exit_code == 0
    assert run(app, ["hold", "dev", "8h"]).exit_code == 0
    assert calls == [
        ("create", "ctx(verbose=True)", "dev", "t3.micro"),
        ("destroy", "ctx(verbose=False)", "dev", True),
        ("hold", "ctx(verbose=False)", "dev", "8h"),
    ]


def test_ssh_passes_extra_args_through(calls, monkeypatch):
    class Ctx:
        def alias(self, env):
            return f"zach-{env}"

    monkeypatch.setattr(cli, "Context", Ctx)
    r = CliRunner().invoke(app, ["ssh", "dev", "-N", "-L", "8080:localhost:8080"])
    assert r.exit_code == 0, r.output
    assert calls == [("execvp", "ssh", ["ssh", "zach-dev", "-N", "-L", "8080:localhost:8080"])]


def test_help_lists_every_command():
    out = CliRunner().invoke(app, ["--help"]).output
    for name in ("create", "destroy", "start", "stop", "list", "ssh", "sync", "doctor", "hold"):
        assert name in out


def test_terraform_summary_and_elapsed():
    assert summary("...\nApply complete! Resources: 1 added, 0 changed, 0 destroyed.\n") == (
        "1 added, 0 changed, 0 destroyed"
    )
    assert summary("Destroy complete! Resources: 2 destroyed.") == "2 destroyed"
    assert summary("Error: boom") == ""
    assert (elapsed(5), elapsed(65), elapsed(3725)) == ("5s", "1m05s", "1h02m")


def test_changed_files_ignores_dirs_and_untouched():
    out = "cd+++++++++ .claude/\n<f+++++++++ .claude/settings.json\n.d..t...... ./\n<f.st...... .gitconfig\n"
    assert changed_files(out) == [".claude/settings.json", ".gitconfig"]


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


def test_init_scaffolds_without_overwriting(tmp_path, monkeypatch):
    from barn.config import PLACEHOLDER
    from barn.setup import scaffold, todo

    monkeypatch.setenv("USER", "zach")
    done = scaffold(tmp_path)
    assert [(p.relative_to(tmp_path).as_posix(), a) for p, a, _ in done] == [
        ("config.toml", "created"),
        ("accounts/default.toml", "created"),
        ("home/.barn/env", "created"),
    ]
    assert (tmp_path / "home" / ".barn" / "env").stat().st_mode & 0o777 == 0o600
    config = (tmp_path / "config.toml").read_text()
    assert 'user = "zach"' in config and f'aws_profile = "{PLACEHOLDER}"' in config

    required = [what for needed, _, what in todo(tmp_path) if needed]
    assert any("aws_profile" in w for w in required) and any("state_bucket" in w for w in required)
    with pytest.raises(ConfigError, match="fill in aws_profile"):
        load_settings(tmp_path)

    (tmp_path / "config.toml").write_text("edited")
    assert all(a == "kept" for _, a, _ in scaffold(tmp_path))
    assert (tmp_path / "config.toml").read_text() == "edited"


def test_init_copies_account_file(tmp_path, monkeypatch):
    from barn.setup import scaffold

    src = tmp_path / "sandbox.toml"
    src.write_text(ACCOUNT)
    home = tmp_path / "home"
    scaffold(home, aws_profile="dev", account_file=src)
    assert (home / "accounts" / "sandbox.toml").read_text() == ACCOUNT
    assert 'account = "sandbox"' in (home / "config.toml").read_text()
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    assert load_account(home, load_settings(home).account).security_group_id == "sg-1"
    with pytest.raises(ConfigError, match="no such account file"):
        scaffold(tmp_path / "other", account_file=tmp_path / "missing.toml")
