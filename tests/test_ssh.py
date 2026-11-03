import os
import subprocess

from barn import ssh


def test_write_host_adds_include_and_replaces_entries(tmp_path):
    (tmp_path / "config").write_text("Host other\n  User x\n")
    ssh.write_host("zach-dev", ssh.host_entry("zach-dev", "i-1", "zach", "~/.ssh/k", "p", "us-east-1"), tmp_path)
    ssh.write_host("zach-two", ssh.host_entry("zach-two", "i-2", "zach", "~/.ssh/k", "p", "us-east-1"), tmp_path)
    ssh.write_host("zach-dev", ssh.host_entry("zach-dev", "i-3", "zach", "~/.ssh/k", "p", "us-east-1"), tmp_path)

    config = (tmp_path / "config").read_text()
    assert config.splitlines()[0] == "Include barn.conf"
    assert config.count("Include barn.conf") == 1
    assert "Host other" in config

    managed = (tmp_path / "barn.conf").read_text()
    assert "HostName i-3" in managed and "HostName i-1" not in managed and "HostName i-2" in managed
    assert "--document-name AWS-StartSSHSession" in managed
    assert "HostKeyAlias zach-dev" in managed


def test_write_host_none_removes(tmp_path):
    ssh.write_host("a-b", ssh.host_entry("a-b", "i-1", "a", "k", "p", "r"), tmp_path)
    ssh.write_host("a-b", None, tmp_path)
    assert "Host a-b" not in (tmp_path / "barn.conf").read_text()



def test_write_host_concurrent_writers_keep_every_entry(tmp_path):
    from multiprocessing import get_all_start_methods, get_context

    names = [f"box-{i}" for i in range(12)]
    method = "fork" if "fork" in get_all_start_methods() else "spawn"  # Windows: spawn
    with get_context(method).Pool(6) as pool:
        pool.starmap(_write, [(n, str(tmp_path)) for n in names])
    managed = (tmp_path / "barn.conf").read_text()
    assert all(f"Host {n}\n" in managed for n in names)


def _write(name, ssh_dir):
    ssh.write_host(name, ssh.host_entry(name, "i-1", "u", "k", "p", "r"), ssh_dir)


def test_ssh_path_suits_ssh_config_on_windows():
    assert ssh.ssh_path("/home/zach/.ssh/id_ed25519") == "/home/zach/.ssh/id_ed25519"
    assert ssh.ssh_path(r"C:\Users\zach\.ssh\id_ed25519") == "C:/Users/zach/.ssh/id_ed25519"
    assert ssh.ssh_path(r"C:\Users\Zach Lefin\.ssh\id_ed25519") == '"C:/Users/Zach Lefin/.ssh/id_ed25519"'


def test_lock_works_without_fcntl(tmp_path, monkeypatch):
    """Windows has no fcntl: compat imports, and locks barn.conf with msvcrt."""
    import importlib
    import sys
    import types

    from barn import compat

    calls = []
    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, n: calls.append((mode, n)))
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl raises ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    try:
        win = importlib.reload(compat)
        assert win.fcntl is None
        ssh.write_host("a-b", ssh.host_entry("a-b", "i-1", "a", "k", "p", "r"), tmp_path)
        assert "Host a-b" in (tmp_path / "barn.conf").read_text()
        assert calls == [(2, 1), (0, 1)]
    finally:
        monkeypatch.undo()
        importlib.reload(compat)
    assert compat.fcntl is not None or os.name == "nt"


def test_windows_proxy_command_goes_through_powershell(monkeypatch):
    from barn import compat

    monkeypatch.setattr(compat, "WINDOWS", True)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    entry = ssh.host_entry("a-b", "i-1", "ubuntu", r"C:\Users\Zach Lefin\.ssh\id_ed25519", "prof", "us-east-1")
    assert 'IdentityFile "C:/Users/Zach Lefin/.ssh/id_ed25519"' in entry
    assert (
        r'ProxyCommand C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe "aws ssm start-session --target %h '
        '--document-name AWS-StartSSHSession --parameters portNumber=%p --profile prof --region us-east-1"'
    ) in entry
    monkeypatch.setattr(compat, "WINDOWS", False)
    assert "ProxyCommand aws ssm start-session --target %h" in ssh.host_entry("a-b", "i-1", "u", "k", "p", "r")


def test_scripts_go_to_the_box_with_lf(monkeypatch):
    """A text-mode pipe on Windows would turn \\n into \\r\\n; run sends bytes."""
    seen = {}

    def fake_run(cmd, input=None, **kw):
        seen["input"] = input
        return subprocess.CompletedProcess(cmd, 0, "ok\r\nü".encode(), b"")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    r = ssh.run("a-b", "echo 1\r\necho 2\n")
    assert seen["input"] == b"echo 1\necho 2\n"
    assert r.stdout == "ok\nü"
