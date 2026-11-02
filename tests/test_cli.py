import pytest

from typer.testing import CliRunner

from barn import cli
from barn.cli import app, parse_duration
from barn.config import UBUNTU_AMI, ConfigError, account_names, check_name, load_account, load_settings
from barn.sync import changed_files, credential_files, rsync_cmd
from barn.tf import PACKAGE, install_roots, summary
from barn.ui import elapsed


def test_parse_duration():
    assert parse_duration("30m") == 1800
    assert parse_duration("8h") == 8 * 3600
    assert parse_duration("2d") == 2 * 86400
    with pytest.raises(ConfigError):
        parse_duration("8 hours")


def test_box_names():
    for good in ("zach-dev", "ab", "a2", "zach-a2n", "a" + "b" * 38 + "c"):
        assert check_name(good) == good
    too_long = "a" * 41
    for bad in ("Dev", "2dev", "a_b", "", "a", "dev-", "zach.dev", too_long, "workspaces", "plugin-cache"):
        with pytest.raises(ConfigError):
            check_name(bad)
    with pytest.raises(ConfigError, match="reserved"):
        check_name("workspaces")


@pytest.fixture
def calls(monkeypatch):
    """Record what each command would do, without AWS."""
    seen = []
    monkeypatch.setattr(cli, "Context", lambda verbose=False, account=None: f"ctx({verbose}, {account})")
    for name in ("create", "destroy", "hold"):
        monkeypatch.setattr(cli, name, lambda *a, _n=name: seen.append((_n, *a)) or 0)
    monkeypatch.setattr(cli.os, "execvp", lambda *a: seen.append(("execvp", *a)))
    return seen


def test_commands_parse(calls):
    run = CliRunner().invoke
    assert run(app, ["create", "zach-dev", "--size", "t3.micro", "-v"]).exit_code == 0
    assert run(app, ["create", "zach-dev", "--account", "work"]).exit_code == 0
    assert run(app, ["destroy", "zach-dev", "--volume"]).exit_code == 0
    assert run(app, ["destroy", "alice-dev", "--force", "--account", "work"]).exit_code == 0
    assert run(app, ["hold", "zach-dev", "8h"]).exit_code == 0
    assert calls == [
        ("create", "ctx(True, None)", "zach-dev", "t3.micro"),
        ("create", "ctx(False, work)", "zach-dev", None),
        ("destroy", "ctx(False, None)", "zach-dev", True, False),
        ("destroy", "ctx(False, work)", "alice-dev", False, True),
        ("hold", "ctx(False, None)", "zach-dev", "8h"),
    ]


def test_ssh_refreshes_entry_and_passes_extra_args_through(calls, monkeypatch):
    monkeypatch.setattr(cli, "Context", lambda account=None: f"ctx({account})")
    state = {"Name": "running"}
    monkeypatch.setattr(cli, "connect", lambda ctx, name, quiet: calls.append(("connect", ctx, name)) or {"State": state})
    r = CliRunner().invoke(app, ["ssh", "alice-dev", "-N", "-L", "8080:localhost:8080"])
    assert r.exit_code == 0, r.output
    assert calls == [
        ("connect", "ctx(None)", "alice-dev"),
        ("execvp", "ssh", ["ssh", "alice-dev", "-N", "-L", "8080:localhost:8080"]),
    ]
    calls.clear()
    # --account is barn's (anywhere after the name); the rest still goes to ssh.
    assert CliRunner().invoke(app, ["ssh", "alice-dev", "-N", "--account", "work", "-a"]).exit_code == 0
    assert calls == [("connect", "ctx(work)", "alice-dev"), ("execvp", "ssh", ["ssh", "alice-dev", "-N", "-a"])]
    calls.clear()
    state["Name"] = "pending"  # just started: reachable in a moment
    assert CliRunner().invoke(app, ["ssh", "alice-dev"]).exit_code == 0
    assert calls[-1][0] == "execvp"
    calls.clear()
    state["Name"] = "stopped"
    assert CliRunner().invoke(app, ["ssh", "alice-dev"]).exit_code == 1
    assert calls == [("connect", "ctx(None)", "alice-dev")]


def test_help_lists_every_command():
    out = CliRunner().invoke(app, ["--help"]).output
    for name in ("create", "destroy", "start", "stop", "list", "connect", "ssh", "sync", "doctor", "hold"):
        assert name in out
    assert "manager" in out.lower() and "anyone" in out


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
    (tmp_path / "config.toml").write_text(
        'default_account = "personal"\nuser = "zach"\n[aws_profiles]\npersonal = "admin"\nwork = "dev"\n'
    )
    (tmp_path / "accounts" / "personal.toml").write_text(ACCOUNT + '[extra_tags]\n"cost-center" = "42"\n')
    (tmp_path / "accounts" / "work.toml").write_text(ACCOUNT)
    s = load_settings(tmp_path)
    assert s.default_size == "m7i.xlarge" and s.shared_home_path is None
    assert (s.aws_profile("personal"), s.aws_profile("work")) == ("admin", "dev")
    with pytest.raises(ConfigError, match=r'add `other = "<profile>"` under \[aws_profiles\]'):
        s.aws_profile("other")
    assert account_names(tmp_path) == ["personal", "work"]
    a = load_account(tmp_path, s.default_account)
    assert a.name == "personal" and a.extra_tags == {"cost-center": "42"}
    assert a.ami_ssm_parameter == UBUNTU_AMI and a.manager_role_arn == ""
    assert s.guest_keys == []


def test_load_settings_rejects_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    (tmp_path / "config.toml").write_text('default_account = "a"\ntypo = 1\n')
    with pytest.raises(ConfigError):
        load_settings(tmp_path)
    # Old keys in a file already at 0.2.0 (so not migrated): the error says what replaced them.
    (tmp_path / "config.toml").write_text('config_version = "0.2.0"\naccount = "a"\naws_profile = "p"\n')
    with pytest.raises(ConfigError, match="`account` is now `default_account`.*aws_profiles"):
        load_settings(tmp_path)
    (tmp_path / "config.toml").write_text('default_account = "../x"\n')
    with pytest.raises(ConfigError, match="bad account name"):
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
    # Before managers and guests (docs/plans/PLAN.md 23.3): the error names the old key.
    (tmp_path / "accounts" / "a.toml").write_text(ACCOUNT + 'developer_role_arn = "arn:r"\n')
    with pytest.raises(ConfigError, match="developer_role_arn"):
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
    assert 'user = "zach"' in config and 'default_account = "default"' in config
    from barn.migrate import installed

    assert f'config_version = "{installed()}"' in config
    assert f'[aws_profiles]\ndefault = "{PLACEHOLDER}"' in config

    required = [what for needed, _, what in todo(tmp_path) if needed]
    assert any("aws_profiles.default" in w for w in required) and any("state_bucket" in w for w in required)
    with pytest.raises(ConfigError, match="fill in aws_profiles.default"):
        load_settings(tmp_path)

    (tmp_path / "config.toml").write_text("edited")
    assert all(a == "kept" for _, a, _ in scaffold(tmp_path))
    assert (tmp_path / "config.toml").read_text() == "edited"


def test_init_copies_account_file(tmp_path, monkeypatch):
    from barn.setup import scaffold, todo

    src = tmp_path / "sandbox.toml"
    src.write_text(ACCOUNT)
    home = tmp_path / "home"
    scaffold(home, aws_profile="dev", account_file=src)
    assert (home / "accounts" / "sandbox.toml").read_text() == ACCOUNT
    config = (home / "config.toml").read_text()
    assert 'default_account = "sandbox"' in config and '[aws_profiles]\nsandbox = "dev"' in config
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    assert load_account(home, load_settings(home).default_account).security_group_id == "sg-1"

    # A second account file is added next to the first; config.toml is kept,
    # and the to-do list asks for the new account's profile.
    other = tmp_path / "work.toml"
    other.write_text(ACCOUNT)
    done = {p.relative_to(home).as_posix(): a for p, a, _ in scaffold(home, account_file=other)}
    assert done["accounts/work.toml"] == "copied" and done["config.toml"] == "kept"
    assert load_settings(home).default_account == "sandbox"
    assert any('add `work = "<profile>"` under [aws_profiles]' in w for _, _, w in todo(home))
    with pytest.raises(ConfigError, match="no such account file"):
        scaffold(tmp_path / "other", account_file=tmp_path / "missing.toml")
