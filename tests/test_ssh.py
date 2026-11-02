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
    from multiprocessing import get_context

    names = [f"box-{i}" for i in range(12)]
    with get_context("fork").Pool(6) as pool:
        pool.starmap(_write, [(n, str(tmp_path)) for n in names])
    managed = (tmp_path / "barn.conf").read_text()
    assert all(f"Host {n}\n" in managed for n in names)


def _write(name, ssh_dir):
    ssh.write_host(name, ssh.host_entry(name, "i-1", "u", "k", "p", "r"), ssh_dir)
