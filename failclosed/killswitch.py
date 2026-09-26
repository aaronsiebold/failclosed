"""One file that stops everything, checked before every action.

Two properties matter more than the feature itself:

1. **A human can engage it without deploying.** `touch state/HALT` from a phone
   over SSH at 2am beats a revert, a CI run and a rollout.
2. **An unreadable switch is an engaged switch.** If the check itself fails —
   permissions, a disk that went away, a path that is somehow a directory — the
   answer is "stop", not "carry on". Every other design makes the switch
   useless in exactly the conditions that would make you reach for it.
"""

from __future__ import annotations

import os
import time
from typing import Optional

__all__ = ["KillSwitch"]


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

    def __init__(self, path: str) -> None:
        self.path = path

    @property
    def engaged(self) -> bool:
        # Not os.path.exists(): it catches OSError itself and answers False,
        # which would make an unreadable switch read as clear. lstat, so a
        # dangling symlink at the path still counts as present.
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            # Cannot determine -> treat as engaged. See module docstring.
            # ValueError: lstat rejects a path with a NUL byte before the OS.
            return True
        return True

    @property
    def reason(self) -> str:
        """The human's stated reason — the first line only.

        `engage()` also writes a timestamp line, and callers put this string
        into single-line logs and alerts, so returning the whole file would
        wrap every one of them.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError (a file that is not UTF-8)
            # and a path the OS rejects (NUL byte, lone surrogate), which
            # `engaged` answers True for. Unreadable reason -> "".
            return ""
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
