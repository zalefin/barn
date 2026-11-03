"""What differs between Linux and Windows, in one place. barn runs on both;
the boxes are always Linux, so anything sent to a box uses LF line endings."""

import os
import time
from contextlib import contextmanager

WINDOWS = os.name == "nt"

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


@contextmanager
def locked(path, timeout=60):
    """Hold an exclusive lock on the file at path (created if missing)."""
    with open(path, "a+", encoding="utf-8") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)  # released when f closes
            yield
            return
        # Windows: lock the file's first byte, retrying while another barn holds it.
        f.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def replace(src, dst, attempts=50):
    """os.replace, retried on Windows while another process (ssh reading
    barn.conf, an editor) has dst open."""
    for i in range(attempts):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if not WINDOWS or i == attempts - 1:
                raise
            time.sleep(0.1)


def write_text(path, text):
    """Write a text file as UTF-8 with LF line endings on every platform."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_text(path):
    return path.read_text(encoding="utf-8")


def lf(data):
    """data (bytes) with CRLF line endings made LF, unless it looks binary."""
    if b"\r\n" not in data or b"\0" in data:
        return data
    return data.replace(b"\r\n", b"\n")
