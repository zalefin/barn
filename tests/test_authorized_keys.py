import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "src" / "barn" / "bootstrap" / "authorized_keys.py"
spec = importlib.util.spec_from_file_location("authorized_keys", SCRIPT)
ak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ak)

OWNER = "ssh-ed25519 AAAAowner zach"
ALICE = "ssh-ed25519 AAAAalice alice"
FOO = "ssh-ed25519 AAAAfoo foo"
HAND = "ssh-rsa AAAAhand added-by-hand"


def test_first_boot_on_an_empty_home():
    assert ak.set_block("", "owner", [OWNER]) == f"# >>> barn owner >>>\n{OWNER}\n# <<< barn owner <<<\n"


def test_pre_blocks_layout_is_replaced_wholesale():
    old = "ssh-ed25519 AAAAoldowner zach\n"
    out = ak.set_block(old, "owner", [OWNER])
    assert "AAAAoldowner" not in out and OWNER in out


def test_guests_added_then_removed_keeps_owner_and_hand_added_keys():
    text = ak.set_block("", "owner", [OWNER])
    text += HAND + "\n"
    text = ak.set_block(text, "guests", [ALICE, FOO])
    assert text.splitlines() == [
        "# >>> barn owner >>>", OWNER, "# <<< barn owner <<<", HAND,
        "# >>> barn guests >>>", ALICE, FOO, "# <<< barn guests <<<",
    ]
    text = ak.set_block(text, "guests", [ALICE])
    assert FOO not in text and ALICE in text and OWNER in text and HAND in text
    text = ak.set_block(text, "guests", [])
    assert ALICE not in text and "# >>> barn guests >>>\n# <<< barn guests <<<" in text


def test_first_boot_keeps_guests_and_hand_added_keys():
    """A recreated box: the volume already has both blocks and a hand-added key."""
    text = ak.set_block(ak.set_block("", "owner", ["ssh-ed25519 AAAAold zach"]), "guests", [ALICE]) + HAND + "\n"
    out = ak.set_block(text, "owner", [OWNER])
    assert "AAAAold" not in out and OWNER in out and ALICE in out and HAND in out
    # Idempotent.
    assert ak.set_block(out, "owner", [OWNER]) == out


def test_guests_only_file_gets_an_owner_block_on_top():
    text = ak.set_block("", "guests", [ALICE])
    out = ak.set_block(text, "owner", [OWNER])
    assert out.splitlines()[:3] == ["# >>> barn owner >>>", OWNER, "# <<< barn owner <<<"]
    assert ALICE in out


def test_blank_and_comment_lines_in_key_input_are_dropped():
    out = ak.set_block("", "guests", ["", "# alice", ALICE, "  "])
    assert out.splitlines() == ["# >>> barn guests >>>", ALICE, "# <<< barn guests <<<"]


@pytest.mark.skipif(os.name == "nt", reason="box-side script: chown and modes are Linux")
def test_command_line(tmp_path):
    keys = tmp_path / "keys"
    keys.write_text(OWNER + "\n")
    target = tmp_path / "authorized_keys"
    target.write_text("ssh-ed25519 AAAAoldowner zach\n")
    r = subprocess.run([sys.executable, str(SCRIPT), str(target), "owner", str(keys)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "owner: 1 key(s)\n"
    assert target.read_text() == f"# >>> barn owner >>>\n{OWNER}\n# <<< barn owner <<<\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".authorized_keys.")]
    bad = subprocess.run([sys.executable, str(SCRIPT), str(target), "other", str(keys)], capture_output=True)
    assert bad.returncode == 2


def test_guests_pushed_onto_a_pre_blocks_file_wrap_the_old_owner_key():
    """A box first booted by old barn: its only key is the owner's. It must not
    become a permanent 'hand-added' key that survives key rotation."""
    old = "ssh-ed25519 AAAAoldowner zach\n"
    text = ak.set_block(old, "guests", [ALICE])
    assert text.splitlines() == [
        "# >>> barn owner >>>", "ssh-ed25519 AAAAoldowner zach", "# <<< barn owner <<<",
        "# >>> barn guests >>>", ALICE, "# <<< barn guests <<<",
    ]
    rotated = ak.set_block(text, "owner", [OWNER])
    assert "AAAAoldowner" not in rotated and OWNER in rotated and ALICE in rotated


def test_orphaned_begin_marker_does_not_swallow_keys():
    """Someone deleted the guests end marker by hand; the next pushes must keep HAND."""
    text = ak.set_block(ak.set_block("", "owner", [OWNER]), "guests", [ALICE])
    text = text.replace("# <<< barn guests <<<\n", "") + HAND + "\n"
    text = ak.set_block(text, "guests", [FOO])
    text = ak.set_block(text, "guests", [FOO])
    assert HAND in text and OWNER in text
    assert text.count(FOO) == 1
