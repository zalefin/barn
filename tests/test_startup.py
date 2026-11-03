"""The startup script: config.toml's startup_script, or barn's default, run at first boot."""

import hashlib
import os
import random
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from barn import cli
from barn.config import STARTUP_DELIMITER, ConfigError, Settings, read_startup_script
from barn.doctor import startup_sha
from barn.setup import scaffold, todo
from barn.tf import PACKAGE


def settings(script=""):
    return Settings(default_account="a", user="zach", startup_script=script)


def test_no_startup_script_means_barns_default(tmp_path):
    assert read_startup_script(settings(), tmp_path) == ""
    assert settings().startup_script_path(tmp_path) is None


def test_startup_script_is_read_relative_to_the_barn_home(tmp_path):
    (tmp_path / "startup.sh").write_bytes(b"#!/bin/bash\r\napt-get install -y -q tmux\r\n")
    # LF on the box, even from a Windows checkout.
    assert read_startup_script(settings("startup.sh"), tmp_path) == "#!/bin/bash\napt-get install -y -q tmux\n"
    other = tmp_path / "elsewhere" / "boot.sh"
    other.parent.mkdir()
    other.write_text("true\n")
    assert read_startup_script(settings(str(other)), tmp_path / "home") == "true\n"


@pytest.mark.parametrize(
    "content, error",
    [
        (None, "no such file"),
        (b"  \n\n", "empty"),
        (b"\xff\xfe", "not UTF-8"),
        (f"cat <<'{STARTUP_DELIMITER}'\nx\n{STARTUP_DELIMITER}\n".encode(), STARTUP_DELIMITER),
    ],
)
def test_bad_startup_scripts_are_refused(tmp_path, content, error):
    if content is not None:
        (tmp_path / "startup.sh").write_bytes(content)
    with pytest.raises(ConfigError, match=error):
        read_startup_script(settings("startup.sh"), tmp_path)


def test_startup_script_must_fit_in_user_data(tmp_path):
    rng = random.Random(0)  # random text does not compress
    big = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(20000))
    (tmp_path / "startup.sh").write_text(f"# {big}\n")
    with pytest.raises(ConfigError, match="too big for EC2 user data"):
        read_startup_script(settings("startup.sh"), tmp_path)


def test_instance_vars_carry_the_startup_script(tmp_path):
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA zach\n")
    s = settings()
    s.ssh_public_key = str(key)
    account = SimpleNamespace(
        region="us-east-1", security_group_id="sg-1", execution_instance_profile="p",
        ami_ssm_parameter="/ami", kms_key_id="", extra_tags={},
    )
    ctx = SimpleNamespace(settings=s, account=account)
    vol = {"volume_id": "vol-1", "subnet_id": "subnet-1"}
    assert cli.instance_vars(ctx, "zach-dev", vol, "t3.micro")["startup_script"] == ""
    assert cli.instance_vars(ctx, "zach-dev", vol, "t3.micro", "true\n")["startup_script"] == "true\n"


def test_init_todo_for_the_startup_script(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "zach")
    scaffold(tmp_path)
    config = tmp_path / "config.toml"
    assert not [w for _, _, w in todo(tmp_path) if "startup" in w]

    (tmp_path / "startup.sh").unlink()
    assert any(needed and "missing" in w for needed, p, w in todo(tmp_path) if p == tmp_path / "startup.sh")

    # Without the key a box gets barn's default; nothing to do (0.6.0 migrated old configs).
    scaffold(tmp_path)
    config.write_text("\n".join(l for l in config.read_text().splitlines() if not l.startswith("startup_script")))
    assert not [w for _, _, w in todo(tmp_path) if "startup" in w]


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="bash heredoc on the box")
@pytest.mark.parametrize("text", ["true", "#!/bin/bash\necho hi\n", "x\n\n"])
def test_doctor_hash_matches_what_first_boot_writes(tmp_path, text):
    # First boot writes the script with a quoted heredoc, as Terraform renders it.
    out = tmp_path / "startup.sh"
    subprocess.run(["bash", "-c", f"cat > {out} <<'{STARTUP_DELIMITER}'\n{text}\n{STARTUP_DELIMITER}\n"], check=True)
    assert startup_sha(text) == hashlib.sha256(out.read_bytes()).hexdigest()
    assert startup_sha("") == "default" and startup_sha(None) == "unknown"


def test_the_default_ships_with_the_bootstrap():
    default = (PACKAGE / "bootstrap" / "startup-default.sh").read_text()
    for tool in ("cli.kiro.dev", "claude.ai/install.sh", "herdr.dev", "astral.sh/uv", "docker-ce", "neovim"):
        assert tool in default
    template = (PACKAGE / "bootstrap" / "user-data.sh.tftpl").read_text()
    assert "${startup_default}" in template and "${startup_script}" in template
    # The installs moved out of the fixed part.
    assert "cli.kiro.dev" not in template and "apt-get install" not in template
