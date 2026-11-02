#!/usr/bin/env python3
"""Rewrite one barn block of an authorized_keys file (PLAN.md 23.4).

    authorized_keys.py FILE owner|guests KEYS_FILE

The file holds two marked blocks: `owner` (written by first boot from the
creator's key) and `guests` (written by `barn create` and `barn sync` from
the creator's guest_keys). Each run replaces only the named block, so keys
outside the blocks (added by hand) survive. A file with no barn blocks at all
is the pre-blocks layout (only the owner's key, written by an older first
boot); writing the owner block replaces it wholesale.

Stdlib only: first boot installs it on the box, and the CLI imports and also
sends it to the box over SSH.
"""

import os
import sys
import tempfile

BLOCKS = ("owner", "guests")
BEGIN = "# >>> barn {} >>>"
END = "# <<< barn {} <<<"


def _block(name, keys):
    return [BEGIN.format(name), *keys, END.format(name)]


def has_blocks(lines):
    return any(line.strip() == BEGIN.format(n) for n in BLOCKS for line in lines)


def _span(lines, name):
    """(start, end) line indexes of the block, end exclusive; None if absent.
    The block ends at the first end marker and starts at the last begin marker
    before it, so an orphaned begin marker (its end deleted by hand) stays plain
    text and never swallows the keys after it."""
    begin, end = BEGIN.format(name), END.format(name)
    stripped = [line.strip() for line in lines]
    for j, line in enumerate(stripped):
        if line == end:
            starts = [i for i in range(j) if stripped[i] == begin]
            return (starts[-1], j + 1) if starts else None
    return None


def set_block(text, name, keys):
    """text with block `name` holding exactly `keys`."""
    if name not in BLOCKS:
        raise ValueError(f"unknown block {name!r}")
    keys = [k.strip() for k in keys if k.strip() and not k.strip().startswith("#")]
    lines = text.splitlines()
    new = _block(name, keys)
    if not has_blocks(lines):
        # The old first boot wrote only the owner's key. Writing the owner block
        # replaces it; writing guests first keeps it, as the owner block, so a
        # later first boot replaces it instead of keeping it as a hand-added key.
        keep = [line for line in lines if line.strip()]
        lines = [] if name == "owner" or not keep else _block("owner", keep)
    span = _span(lines, name)
    if span:
        lines[span[0] : span[1]] = new
    elif name == "owner":
        lines = new + lines  # owner first
    else:
        lines = lines + new
    return "\n".join(lines) + "\n"


def read_keys(path):
    with open(path) as f:
        return f.read().splitlines()


def rewrite(path, name, keys):
    """Replace the block in path (created if missing), atomically, mode 0600."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    out = set_block(text, name, keys)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".authorized_keys.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(out)
        os.chmod(tmp, 0o600)
        try:
            st = os.stat(path)
            os.chown(tmp, st.st_uid, st.st_gid)
        except (FileNotFoundError, PermissionError):
            pass
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return out


def main(argv):
    if len(argv) != 4 or argv[2] not in BLOCKS:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    out = rewrite(argv[1], argv[2], read_keys(argv[3]))
    span = _span(out.splitlines(), argv[2])
    print(f"{argv[2]}: {span[1] - span[0] - 2} key(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
