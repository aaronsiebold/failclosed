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
* A path no switch file can be at (empty, a NUL byte, not UTF-8 text) is
  refused when the KillSwitch is built. Such a switch would read clear, and
  engage() could never change that. `path` stays a plain attribute, so each
  check applies the same rule again: a bad path set later reads engaged, and
  `reason` says why.
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


def _flaw(text: str) -> str:
    """Why no switch file can be at `text`, or "" if one can.

    Empty or NUL: no file has that name. Not UTF-8: macOS cannot store the
    name, and it is refused everywhere so a config means the same thing on
    every machine.
    """
    if not text:
        return "empty"
    if "\0" in text:
        return "contains a NUL byte"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "not UTF-8 text"
    return ""


def _checked(path: object) -> tuple[str, str]:
    """(`path` as text, its flaw). What `path` holds now, checked again.

    `path` is a plain attribute, so a caller can set it to anything after
    the constructor has checked it.
    """
    try:
        text = os.fsdecode(path)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - not a path, or a __fspath__ that raised
        return "", "not a path"
    return text, _flaw(text)


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
        text = os.fsdecode(path)  # TypeError for anything that is not a path
        why = _flaw(text)
        if why:
            raise ValueError(f"kill switch path {path!r} can never be engaged ({why})")
        self.path: str = text

    @property
    def engaged(self) -> bool:
        path, why = _checked(self.path)
        if why:
            return True
        # Not os.path.exists(): it catches OSError itself and answers False,
        # which would make an unreadable switch read as clear. lstat, so a
        # dangling symlink at the path still counts as present.
        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Cannot determine -> treat as engaged. See module docstring.
            # ValueError should not get past _checked; if it does, the
            # answer is still "engaged", never an exception.
            return True
        else:
            return True
        try:
            return not _absence_is_proven(path)
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
        the Gate in front of it. Anything else answers with its type instead,
        and a path no switch can be at answers with what is wrong with it.
        """
        path, why = _checked(self.path)
        if why:
            return f"unusable path ({why})"
        try:
            mode = os.stat(path).st_mode
            if not stat.S_ISREG(mode):
                return _unusual(mode)
            # O_NONBLOCK: if a FIFO replaced the file since the stat, the
            # open returns at once, and the fstat below catches the swap.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except (OSError, ValueError):
            # Absent or unreadable: no reason to give.
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
        path, why = _checked(self.path)
        if why:
            return None
        try:
            return os.path.getmtime(path)
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
