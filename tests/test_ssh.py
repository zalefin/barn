from agent_platform import ssh


def test_write_host_adds_include_and_replaces_entries(tmp_path):
    (tmp_path / "config").write_text("Host other\n  User x\n")
    ssh.write_host("zach-dev", ssh.host_entry("zach-dev", "i-1", "zach", "~/.ssh/k", "p", "us-east-1"), tmp_path)
    ssh.write_host("zach-two", ssh.host_entry("zach-two", "i-2", "zach", "~/.ssh/k", "p", "us-east-1"), tmp_path)
    ssh.write_host("zach-dev", ssh.host_entry("zach-dev", "i-3", "zach", "~/.ssh/k", "p", "us-east-1"), tmp_path)

    config = (tmp_path / "config").read_text()
    assert config.splitlines()[0] == "Include agent-platform.conf"
    assert config.count("Include agent-platform.conf") == 1
    assert "Host other" in config

    managed = (tmp_path / "agent-platform.conf").read_text()
    assert "HostName i-3" in managed and "HostName i-1" not in managed and "HostName i-2" in managed
    assert "--document-name AWS-StartSSHSession" in managed
    assert "HostKeyAlias zach-dev" in managed


def test_write_host_none_removes(tmp_path):
    ssh.write_host("a-b", ssh.host_entry("a-b", "i-1", "a", "k", "p", "r"), tmp_path)
    ssh.write_host("a-b", None, tmp_path)
    assert "Host a-b" not in (tmp_path / "agent-platform.conf").read_text()


def test_write_aws_profile_preserves_other_profiles(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("[profile admin]\nregion = us-east-1\n")
    for _ in range(2):
        ssh.write_aws_profile("agent-platform-personal", "arn:role", "admin", "us-east-1", "s", cfg)
    text = cfg.read_text()
    assert text.startswith("[profile admin]")
    assert text.count("[profile agent-platform-personal]") == 1
    assert "source_profile = admin" in text
