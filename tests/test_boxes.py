"""Box names, the owner guard, connect, list and guest keys (docs/plans/PLAN.md 23), against a fake EC2."""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from barn import cli, ssh, sync, ui
from barn.config import ConfigError, load_settings
from barn.doctor import caller_role, is_manager
from barn.tf import Terraform

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAlice alice"
KEY_F = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFoo foo@laptop"
NOW = datetime.now(timezone.utc)


def tags(**kw):
    return [{"Key": k.replace("_", ":"), "Value": v} for k, v in kw.items()]


def instance(name, owner, iid, state="running", itype="t3.medium"):
    return {
        "InstanceId": iid,
        "InstanceType": itype,
        "State": {"Name": state},
        "LaunchTime": NOW - timedelta(hours=2),
        "Tags": [*tags(barn="true", barn_owner=owner), {"Key": "barn:name", "Value": name}],
    }


def volume(name, owner, size=100, device="/dev/sdf"):
    return {
        "Size": size,
        "Attachments": [{"Device": device}] if device else [],
        "Tags": [*tags(barn="true", barn_owner=owner), {"Key": "barn:name", "Value": name}],
    }


class FakeEc2:
    def __init__(self, instances=(), volumes=()):
        self._instances = list(instances)
        self._volumes = list(volumes)
        self.filters = []

    def _match(self, resource, filters):
        t = {x["Key"]: x["Value"] for x in resource.get("Tags", [])}
        for f in filters:
            if f["Name"].startswith("tag:"):
                if t.get(f["Name"][4:]) not in f["Values"]:
                    return False
            elif f["Name"] == "instance-state-name" and resource["State"]["Name"] not in f["Values"]:
                return False
        return True

    def get_paginator(self, method):
        fake = self

        class Pages:
            def paginate(self, Filters):
                fake.filters.append((method, Filters))
                if method == "describe_instances":
                    found = [i for i in fake._instances if fake._match(i, Filters)]
                    return [{"Reservations": [{"Instances": found}]}]
                return [{"Volumes": [v for v in fake._volumes if fake._match(v, Filters)]}]

        return Pages()


class FakeCtx(cli.Context):
    """A Context with fake settings, account and EC2; no files or AWS."""

    def __init__(self, ec2, user="zach", guest_keys=(), home=None):
        self.home = home
        self.settings = SimpleNamespace(
            user=user,
            aws_profile=f"barn-{user}-manager",
            private_key_path=f"/keys/{user}",
            guest_keys=list(guest_keys),
            shared_home_path=None,
            default_size="m7i.xlarge",
        )
        self.account = SimpleNamespace(region="us-east-1", name="sandbox", manager_role_arn="")
        self.aws = SimpleNamespace(client=lambda name, **kw: ec2)
        self.verbose = False
        self._roots_installed = True

    def tf(self, name, root):
        raise AssertionError(f"terraform must not run ({name}/{root})")


# --- names and state ---


def test_state_key_and_data_dir_use_the_box_name(tmp_path):
    t = Terraform(tmp_path, None, "zach-a2n", "volume")
    assert t.state_key == "env/zach-a2n/volume.tfstate"
    assert t.data_dir == tmp_path / "state" / "zach-a2n" / "volume"


def test_barn_conf_entry_uses_the_full_name(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh.Path, "home", lambda: tmp_path)
    ctx = FakeCtx(FakeEc2(), user="foo")
    cli.write_ssh_entry(ctx, "zach-dev", "i-0abc")
    conf = (tmp_path / ".ssh" / "barn.conf").read_text()
    assert "Host zach-dev\n" in conf and "HostKeyAlias zach-dev" in conf and "HostName i-0abc" in conf
    # The caller's own key and profile, not the owner's.
    assert "IdentityFile /keys/foo" in conf and "--profile barn-foo-manager" in conf


# --- the owner guard ---


def test_owners_and_guard():
    ec2 = FakeEc2(
        [instance("alice-dev", "alice", "i-1"), instance("zach-dev", "zach", "i-2")],
        [volume("alice-dev", "alice"), volume("zach-dev", "zach"), volume("zach-dev", "zach", 30, "/dev/sda1")],
    )
    ctx = FakeCtx(ec2)
    assert ctx.owners("alice-dev") == {"alice"}
    assert ctx.owners("zach-dev") == {"zach"}
    assert ctx.owners("new-box") == set()
    cli.check_owner(ctx, "zach-dev", "create")
    cli.check_owner(ctx, "new-box", "create")
    with pytest.raises(ConfigError, match="refusing to create alice-dev: it is owned by alice"):
        cli.check_owner(ctx, "alice-dev", "create")
    cli.check_owner(ctx, "alice-dev", "destroy", force=True)


def test_guard_sees_a_volume_without_a_box():
    ctx = FakeCtx(FakeEc2([], [volume("alice-dev", "alice", device=None)]))
    with pytest.raises(ConfigError, match="owned by alice"):
        cli.check_owner(ctx, "alice-dev", "create")


def test_create_and_destroy_refuse_another_owners_box():
    ctx = FakeCtx(FakeEc2([instance("alice-dev", "alice", "i-1")]))
    with pytest.raises(ConfigError, match="refusing to create alice-dev"):
        cli.create(ctx, "alice-dev")
    with pytest.raises(ConfigError, match="--force"):
        cli.destroy(ctx, "alice-dev")


def test_sync_refuses_another_owners_box():
    ctx = FakeCtx(FakeEc2([instance("alice-dev", "alice", "i-1")]))
    with pytest.raises(ConfigError, match="refusing to sync alice-dev: it is owned by alice; it would overwrite"):
        sync.sync(ctx, "alice-dev")


def test_sync_needs_a_running_box(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh.Path, "home", lambda: tmp_path)
    with pytest.raises(ConfigError, match="no box named"):
        sync.sync(FakeCtx(FakeEc2()), "zach-dev")
    with pytest.raises(ConfigError, match="zach-dev is stopped"):
        sync.sync(FakeCtx(FakeEc2([instance("zach-dev", "zach", "i-1", state="stopped")])), "zach-dev")


def test_sync_pushes_guest_keys_even_while_agents_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh.Path, "home", lambda: tmp_path)
    ctx = FakeCtx(FakeEc2([instance("zach-dev", "zach", "i-new")]), guest_keys=[KEY_A], home=tmp_path)
    scripts = []

    def run(alias, script, timeout=120):
        scripts.append((alias, script))
        busy = "pgrep" in script
        return subprocess.CompletedProcess([], 0, "123 claude\n" if busy else "guests: 1 key(s)\n", "")

    monkeypatch.setattr(ssh, "run", run)
    assert sync.sync(ctx, "zach-dev") == 1  # agents running: no file copy
    assert [a for a, _ in scripts] == ["zach-dev", "zach-dev"]
    assert "authorized_keys guests" in scripts[0][1] and KEY_A in scripts[0][1]
    assert "pgrep" in scripts[1][1]
    # The SSH entry was refreshed to the current instance first.
    assert "HostName i-new" in (tmp_path / ".ssh" / "barn.conf").read_text()


# --- connect and list ---


def test_connect_writes_entry_and_reports_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ssh.Path, "home", lambda: tmp_path)
    ctx = FakeCtx(FakeEc2([instance("zach-dev", "zach", "i-9", state="stopped")]), user="foo")
    inst = cli.connect(ctx, "zach-dev")
    assert inst["InstanceId"] == "i-9"
    assert "HostName i-9" in (tmp_path / ".ssh" / "barn.conf").read_text()
    err = capsys.readouterr().err
    assert "zach-dev is stopped" in err and "barn start zach-dev" in err
    with pytest.raises(ConfigError, match="no box named 'nope'"):
        cli.connect(ctx, "nope")
    with pytest.raises(ConfigError, match="bad box name"):
        cli.connect(ctx, "Bad_Name")


def test_list_shows_every_box_with_owner():
    ec2 = FakeEc2(
        [
            instance("alice-dev", "alice", "i-1"),
            instance("zach-dev", "zach", "i-2", state="stopped"),
            # Made before box names: no barn:name tag, not listed.
            {**instance("x", "zach", "i-3"), "Tags": tags(barn="true", barn_owner="zach")},
        ],
        [volume("alice-dev", "alice"), volume("zach-dev", "zach", 50), volume("zach-a2n", "zach", device=None),
         volume("alice-dev", "alice", 30, "/dev/sda1")],
    )
    ctx = FakeCtx(ec2)
    found = cli.boxes(ctx)
    assert sorted(found) == ["alice-dev", "zach-a2n", "zach-dev"]
    assert found["alice-dev"]["volume_gb"] == 100 and found["alice-dev"]["owner"] == "alice"
    assert found["zach-a2n"] == {"volume_gb": 100, "owner": "zach"}
    # Every barn box in the account: filtered by tag:barn only, never by owner.
    assert all(f["Name"] != "tag:barn:owner" for _, fs in ec2.filters for f in fs)

    from rich.console import Console

    console = Console(width=140, record=True, theme=ui.THEME)
    console.print(cli.box_table(found, "zach", 140))
    text = console.export_text()
    assert "OWNER" in text and "zach (you)" in text and "alice (you)" not in text
    assert "3 boxes (2 yours) · 1 running" in text


# --- guest keys ---


def test_guest_keys_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("BARN_SETTINGS", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'account = "a"\naws_profile = "p"\nguest_keys = ["{KEY_A}", " {KEY_F} "]\n')
    assert load_settings(tmp_path).guest_keys == [KEY_A, KEY_F]
    cfg.write_text('account = "a"\naws_profile = "p"\nguest_keys = ["not a key"]\n')
    with pytest.raises(ConfigError, match="guest_keys"):
        load_settings(tmp_path)
    cfg.write_text(f'account = "a"\naws_profile = "p"\nguest_keys = "{KEY_A}"\n')
    with pytest.raises(ConfigError, match="guest_keys must be a list"):
        load_settings(tmp_path)


def test_authorized_keys_script_runs(tmp_path):
    """The script create and sync send over SSH, run locally as if on the box."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    ak = home / ".ssh" / "authorized_keys"
    ak.write_text("# >>> barn owner >>>\nssh-ed25519 AAAAowner zach\n# <<< barn owner <<<\nssh-rsa AAAAhand added\n")
    env = {**os.environ, "HOME": str(home)}

    def push(keys):
        r = subprocess.run(["bash", "-s"], input=ssh.authorized_keys_script("guests", keys), text=True,
                           capture_output=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert push([KEY_A, KEY_F]) == "guests: 2 key(s)\n"
    text = ak.read_text()
    assert KEY_A in text and KEY_F in text and "AAAAowner" in text and "AAAAhand" in text
    assert ak.stat().st_mode & 0o777 == 0o600
    push([KEY_A])  # revoke foo
    text = ak.read_text()
    assert KEY_F not in text and KEY_A in text and "AAAAowner" in text and "AAAAhand" in text
    with pytest.raises(ValueError):
        ssh.authorized_keys_script("guests", ["ssh-ed25519 AAAA x\nBARN_KEYS\nrm -rf ~"])


# --- doctor ---


def test_doctor_detects_the_manager_role():
    mgr = "arn:aws:iam::123456789012:role/barn-manager"
    assert caller_role("arn:aws:sts::123456789012:assumed-role/barn-manager/zach") == "barn-manager"
    assert caller_role("arn:aws:iam::123456789012:user/zach") is None
    assert is_manager("arn:aws:sts::123456789012:assumed-role/barn-manager/zach", mgr)
    assert is_manager("arn:aws:sts::1:assumed-role/barn-manager/zach", "arn:aws:iam::1:role/team/barn-manager")
    assert not is_manager("arn:aws:sts::123456789012:assumed-role/barn-guest/foo", mgr)
    assert not is_manager("arn:aws:iam::123456789012:user/zach", mgr)
    assert not is_manager("arn:aws:sts::123456789012:assumed-role/barn-manager/zach", "")


# --- policies ---


def test_guest_policy_is_the_managers_ssh_statements_plus_reads():
    import json
    from pathlib import Path

    docs = Path(__file__).parent.parent / "docs"
    manager = json.loads((docs / "manager-policy.json").read_text())["Statement"]
    guest = json.loads((docs / "guest-policy.json").read_text())["Statement"]
    ssh_stmts = [s for s in manager if s["Sid"].startswith("StartSshSession")]
    assert len(ssh_stmts) == 2 and all(s in guest for s in ssh_stmts)
    reads = [s for s in guest if s not in ssh_stmts]
    assert len(reads) == 1 and reads[0]["Resource"] == "*"
    assert sorted(reads[0]["Action"]) == ["ec2:DescribeInstances", "ec2:DescribeVolumes", "ssm:DescribeInstanceInformation"]
    assert any(s["Sid"] == "Doctor" and s["Resource"] == "MANAGER_ROLE_ARN" for s in manager)
