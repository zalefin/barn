"""config.toml versions and automatic migrations (src/barn/migrate.py)."""

import tomllib

import pytest

from barn import migrate as m
from barn.config import ConfigError, load_settings

OLD = """\
# barn settings for the sandbox account. Copy this directory's contents.
account = "sandbox"               # accounts/sandbox.toml
aws_profile = "barn-zach-manager" # assumes barn-manager
user = "zach"                     # barn:owner of boxes you create
guest_keys = [
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlice alice",
  # "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOld old",
]
idle_hours = 4
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    (tmp_path / "config.toml").write_text(OLD)
    return tmp_path


def test_semver_and_registry():
    assert m.parse("0.2.0") < m.parse("0.10.0") < m.parse("1.0.0")
    for bad in ("0.2", "v0.2.0", "0.2.0-rc1", "01.2.3", None):
        with pytest.raises(ConfigError):
            m.parse(bad)
    # A migration at a version above the package's would never run: bump pyproject.toml.
    assert all(v <= m.parse(m.installed()) for v, _, _ in m.MIGRATIONS)
    assert [v for v, _, _ in m.MIGRATIONS] == sorted(v for v, _, _ in m.MIGRATIONS)


def test_0_2_0_migrates_an_old_config_and_state(home):
    box = home / "state" / "zach-a2n" / "instance"
    box.mkdir(parents=True)
    (box / "apply.log").write_text("log")
    (home / "state" / "plugin-cache").mkdir()

    assert m.migrate(home, home / "config.toml", now="0.2.0") == ["0.2.0"]
    text = (home / "config.toml").read_text()
    data = tomllib.loads(text)
    assert data["config_version"] == "0.2.0"
    assert data["default_account"] == "sandbox" and "account" not in data and "aws_profile" not in data
    assert data["aws_profiles"] == {"sandbox": "barn-zach-manager"}
    assert len(data["guest_keys"]) == 1
    # Comments and layout survive; the version goes first, after the header comment.
    assert text.splitlines()[1].startswith('config_version = "0.2.0"')
    assert 'default_account = "sandbox"               # accounts/sandbox.toml' in text
    assert (home / "config.toml.0.1.0.bak").read_text() == OLD
    # Terraform data dirs move under the account; the provider cache stays.
    assert (home / "state" / "sandbox" / "zach-a2n" / "instance" / "apply.log").read_text() == "log"
    assert not (home / "state" / "zach-a2n").exists() and (home / "state" / "plugin-cache").is_dir()

    # Done once: a second run changes nothing.
    assert m.migrate(home, home / "config.toml", now="0.2.0") == []
    assert (home / "config.toml").read_text() == text


def test_0_3_0_scopes_guest_keys_to_the_default_account(home):
    assert m.migrate(home, home / "config.toml", now="0.3.0") == ["0.2.0", "0.3.0"]
    text = (home / "config.toml").read_text()
    data = tomllib.loads(text)
    assert data["guest_keys"] == {"sandbox": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlice alice"]}
    assert data["idle_hours"] == 4 and data["config_version"] == "0.3.0"
    # The array moves whole, comments included, into the table at the end.
    assert text.endswith(
        '[guest_keys]\nsandbox = [\n  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlice alice",\n'
        '  # "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOld old",\n]\n'
    )
    assert m.migrate(home, home / "config.toml", now="0.3.0") == []

    # From 0.2.0 with no guests: the empty list just goes.
    (home / "config.toml").write_text('config_version = "0.2.0"\ndefault_account = "a"\nguest_keys = []\n')
    m.migrate(home, home / "config.toml", now="0.3.0")
    assert "guest_keys" not in tomllib.loads((home / "config.toml").read_text())


def test_load_settings_migrates_automatically(home, capsys):
    s = load_settings(home)
    assert s.default_account == "sandbox" and s.aws_profile("sandbox") == "barn-zach-manager"
    assert list(s.guest_keys) == ["sandbox"]
    assert "migrated config.toml from barn 0.1.0" in capsys.readouterr().err


def test_a_hand_migrated_config_only_gets_its_version(home):
    hand = OLD.replace("account =", "default_account =").replace(
        'aws_profile = "barn-zach-manager" # assumes barn-manager\n', ""
    ) + '\n[aws_profiles]\nsandbox = "barn-zach-manager"\n'
    (home / "config.toml").write_text(hand)
    m.migrate(home, home / "config.toml", now="0.2.0")
    text = (home / "config.toml").read_text()
    assert text.replace('config_version = "0.2.0"', "").count("=") == hand.count("=")
    assert tomllib.loads(text)["aws_profiles"] == {"sandbox": "barn-zach-manager"}


def test_only_newer_migrations_run_in_order(home, monkeypatch):
    ran = []
    fake = [
        (m.parse(v), v, lambda home, text, v=v: (ran.append(v) or text, []))
        for v in ("0.2.0", "0.3.0", "0.3.1", "1.0.0")
    ]
    monkeypatch.setattr(m, "MIGRATIONS", fake)
    (home / "config.toml").write_text('config_version = "0.2.0"\ndefault_account = "a"\n')
    assert m.migrate(home, home / "config.toml", now="0.3.1") == ["0.3.0", "0.3.1"]
    assert ran == ["0.3.0", "0.3.1"]
    assert tomllib.loads((home / "config.toml").read_text())["config_version"] == "0.3.1"
    assert (home / "config.toml.0.2.0.bak").exists()


def test_no_pending_migration_leaves_the_file_alone(home):
    text = 'config_version = "0.2.0"\ndefault_account = "a"\n'
    (home / "config.toml").write_text(text)
    assert m.migrate(home, home / "config.toml", now="0.2.5") == []
    assert (home / "config.toml").read_text() == text


def test_newer_or_bad_config_version_is_an_error(home):
    (home / "config.toml").write_text('config_version = "9.0.0"\ndefault_account = "a"\n')
    with pytest.raises(ConfigError, match="from barn 9.0.0, newer than this barn"):
        load_settings(home)
    (home / "config.toml").write_text('config_version = "latest"\ndefault_account = "a"\n')
    with pytest.raises(ConfigError, match="config_version"):
        load_settings(home)


def test_text_helpers():
    text = '# c\na = "1"  # keep\n\n[t]\na = "table"\n'
    assert m.rename_key(text, "a", "b") == '# c\nb = "1"  # keep\n\n[t]\na = "table"\n'
    assert m.remove_key(text, "a") == '# c\n\n[t]\na = "table"\n'
    assert tomllib.loads(m.add_table_entries(text, "t", {"x": "y", "a": "no"}))["t"] == {"a": "table", "x": "y"}
    assert tomllib.loads(m.add_table_entries(text, "u", {"k": "v"}))["u"] == {"k": "v"}
    assert m.set_key(text, "v", "1.0.0").startswith('# c\nv = "1.0.0"\na = "1"')
    multi = 'k = [\n  "x",  # one\n]\nb = 2\n'
    assert m.pop_key(multi, "k") == ("b = 2\n", 'k = [\n  "x",  # one\n]\n')
    assert m.pop_key(multi, "missing") == (multi, None)
