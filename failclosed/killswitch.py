"""One file that stops everything, checked before every action.

Two properties matter more than the feature itself:

1. **A human can engage it without deploying.** `touch state/HALT` from a phone
   over SSH at 2am beats a revert, a CI run and a rollout.
2. **An unreadable switch is an engaged switch.** If the check itself fails —
   permissions, a disk that went away, a path that is somehow a directory — the
   answer is "stop", not "carry on". Every other design makes the switch
   useless in exactly the conditions that would make you reach for it.

Two consequences of the second rule:

* A missing file reads clear only when its absence is proven: the nearest
  directory above it that exists is a real directory. A missing directory
  proves it, because no one can create a file inside a directory that is not
  there. A directory that is a symlink to nothing proves nothing: that is a
  disk that went away, and the switch may be on it.
* A path no file can ever have (empty, a NUL byte, not UTF-8 text) is refused
  when the KillSwitch is built. Such a switch would read clear, and engage()
  could never change that.
"""

from __future__ import annotations

import os
import stat
import time
from typing import Optional, Union

__all__ = ["KillSwitch"]

#: `reason` reads at most this many bytes: it goes into one-line logs.
REASON_BYTES = 1024

_KINDS = {
    stat.S_IFDIR: "directory",
    stat.S_IFIFO: "FIFO",
    stat.S_IFSOCK: "socket",
    stat.S_IFCHR: "character device",
    stat.S_IFBLK: "block device",
}


def _usable(path: Union[str, bytes, os.PathLike]) -> str:
    """The path as text, or ValueError if no switch file could ever be there."""
    text = os.fsdecode(path)  # TypeError for anything that is not a path
    if not text:
        why = "it is empty"
    elif "\0" in text:
        why = "it contains a NUL byte"
    else:
        try:
            text.encode("utf-8")
            return text
        except UnicodeEncodeError:
            why = "it is not valid UTF-8 text"
    raise ValueError(f"kill switch path {path!r} can never be engaged: {why}")


def _absence_is_proven(path: str) -> bool:
    """`path` is absent. True if the nearest ancestor that exists is a directory.

    Raises OSError when that ancestor is a symlink to nothing (a volume that
    went away) or cannot be checked.
    """
    level = path
    while True:
        up = os.path.dirname(level) or os.curdir
        if up == level:
            raise FileNotFoundError(path)  # nothing above it exists
        level = up
        try:
            os.lstat(level)
        except FileNotFoundError:
            continue
        return stat.S_ISDIR(os.stat(level).st_mode)


def _unusual(mode: int) -> str:
    return f"unusual file type ({_KINDS.get(stat.S_IFMT(mode), 'unknown')}), not read"


class KillSwitch:
    """A presence-based halt flag.

        halt = KillSwitch("state/HALT")
        gate = Gate(*guards, killswitch=halt)

        halt.engage("bad template shipped in 4f21a9c")
        ...
        halt.release()

    `engaged` raises nothing and answers True whenever it cannot prove the
    switch is clear.
    """

    def __init__(self, path: Union[str, bytes, os.PathLike]) -> None:
        self.path: str = _usable(path)

    @property
    def engaged(self) -> bool:
        # Not os.path.exists(): it catches OSError itself and answers False,
        # which would make an unreadable switch read as clear. lstat, so a
        # dangling symlink at the path still counts as present.
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Cannot determine -> treat as engaged. See module docstring.
            # ValueError: a NUL byte or lone surrogate, if `path` was changed
            # after construction; lstat rejects those before the OS sees them.
            return True
        else:
            return True
        try:
            return not _absence_is_proven(self.path)
        except (OSError, ValueError):
            return True

    @property
    def reason(self) -> str:
        """The human's stated reason — the first line, at most `REASON_BYTES`.

        `engage()` also writes a timestamp line, and callers put this string
        into single-line logs and alerts, so returning the whole file would
        wrap every one of them.

        Only a regular file is read. A FIFO blocks `open()` until a writer
        arrives and a device like /dev/zero never ends, so either would hang
        the Gate in front of it. Anything else answers with its type instead.
        """
        try:
            mode = os.stat(self.path).st_mode
            if not stat.S_ISREG(mode):
                return _unusual(mode)
            # O_NONBLOCK: if a FIFO replaced the file since the stat, the
            # open returns at once, and the fstat below catches the swap.
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except (OSError, ValueError):
            # Absent, unreadable, or a path the OS rejects: no reason to give.
            # ValueError is a NUL byte or a lone surrogate in the path.
            return ""
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                return _unusual(mode)
            head = os.read(fd, REASON_BYTES)
        except OSError:
            return ""
        finally:
            os.close(fd)
        # "replace": a cut through a multi-byte character, or a file that is
        # not UTF-8, still shows the human's words rather than nothing.
        first = head.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        return "" if first.startswith("engaged ") else first

    @property
    def engaged_at(self) -> Optional[float]:
        try:
            return os.path.getmtime(self.path)
        except (OSError, ValueError):
            return None

    def engage(self, reason: str = "") -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(f"{reason}\nengaged {stamp}\n" if reason else f"engaged {stamp}\n")

    def release(self) -> None:
        """Clear the switch. A switch that was never engaged clears cleanly."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def __bool__(self) -> bool:
        return self.engaged

    def __repr__(self) -> str:
        return f"<KillSwitch {self.path} {'ENGAGED' if self.engaged else 'clear'}>"
