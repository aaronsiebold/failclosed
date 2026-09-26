"""An append-only record of what actually happened, that knows when it is stale.

The bug this is shaped around:

    A guard whose input can go stale cannot catch that input going stale.

A local queue file says twenty-three messages went out. The mailbox says five
hundred and fifty-three. Every guard reading the queue passes, and passes
*because* it is out of date — the check is most confident precisely when its
picture of the world is most wrong. Nothing about the guard's logic is broken.
Its input is.

So the ledger does two things a plain set does not:

1. It is refreshed from the authoritative source — the place the action really
   happens — not from our own intent to act.
2. It refuses to answer once that refresh is older than `max_staleness`.
   `contains()` raises rather than returning False, because a confident False
   from a stale ledger is the failure mode, not the error case.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Optional

from .guard import Undetermined

__all__ = ["Ledger", "StaleLedger", "Entry"]

#: Fifteen minutes. Long enough to survive a slow sync, short enough that a
#: dead sync job surfaces within one send window rather than overnight.
DEFAULT_MAX_STALENESS = 15 * 60.0


class StaleLedger(Undetermined):
    """The ledger's view of the world is too old to answer questions with.

    Subclasses `Undetermined`, so a `Gate` treats it as a refusal without any
    special handling at the call site.
    """


@dataclass(frozen=True)
class Entry:
    key: str
    at: float
    meta: Dict[str, Any]

    def as_json(self) -> str:
        return json.dumps({"key": self.key, "at": self.at, "meta": self.meta}, sort_keys=True)

    @staticmethod
    def from_json(line: str) -> "Entry":
        raw = json.loads(line)
        return Entry(key=raw["key"], at=float(raw["at"]), meta=raw.get("meta") or {})


class Ledger:
    """Keys that have already been acted on, plus the age of that knowledge.

        ledger = Ledger("state/sent.jsonl")
        ledger.sync(keys_from_the_mail_provider())   # authoritative source

        @guard("not-already-sent")
        def not_already_sent(action):
            if ledger.contains(action.key):          # raises if stale
                raise Refuse(f"already sent to {action.key}")

    `path` may be None for an in-memory ledger, which is what the tests use.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        max_staleness: float = DEFAULT_MAX_STALENESS,
        clock=time.time,
    ) -> None:
        self.path = path
        self.max_staleness = max_staleness
        self._clock = clock
        self._keys: Dict[str, Entry] = {}
        self._synced_at: Optional[float] = None
        if path:
            # Not os.path.exists(): it answers False for a file it cannot
            # read, and an unreadable ledger loaded as empty forgets every
            # send. Only a file that is truly absent starts empty.
            try:
                self._load()
            except FileNotFoundError:
                pass

    # ---------------------------------------------------------------- state

    def _load(self) -> None:
        assert self.path is not None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = Entry.from_json(line)
                self._keys[entry.key] = entry

    def _append(self, entry: Entry) -> None:
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(entry.as_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------ freshness

    @property
    def synced_at(self) -> Optional[float]:
        return self._synced_at

    @property
    def age(self) -> Optional[float]:
        """Seconds since the last sync, or None if never synced."""
        if self._synced_at is None:
            return None
        return self._clock() - self._synced_at

    @property
    def is_fresh(self) -> bool:
        age = self.age
        return age is not None and age <= self.max_staleness

    def require_fresh(self) -> None:
        """Raise `StaleLedger` unless the ledger was synced recently enough."""
        age = self.age
        if age is None:
            raise StaleLedger(
                "ledger has never been synced against the authoritative source"
            )
        if age > self.max_staleness:
            raise StaleLedger(
                f"ledger last synced {age:.0f}s ago, limit is {self.max_staleness:.0f}s"
            )

    # ------------------------------------------------------------- contents

    def sync(self, keys: Iterable[str], *, now: Optional[float] = None) -> int:
        """Replace the ledger's view with the authoritative one.

        Returns the number of keys the source knew about that we did not — a
        non-zero value here means something acted outside this system, which is
        worth alerting on rather than quietly absorbing.
        """
        stamp = self._clock() if now is None else now
        unseen = 0
        for key in keys:
            if key not in self._keys:
                unseen += 1
                entry = Entry(key=key, at=stamp, meta={"source": "sync"})
                self._keys[key] = entry
                self._append(entry)
        self._synced_at = stamp
        return unseen

    def contains(self, key: str) -> bool:
        """True if `key` has already been acted on.

        Raises `StaleLedger` when the ledger is too old to be trusted. That is
        the whole point: callers cannot accidentally read staleness as absence.
        """
        self.require_fresh()
        return key in self._keys

    def record(self, key: str, **meta: Any) -> Entry:
        """Note that `key` has now been acted on."""
        entry = Entry(key=key, at=self._clock(), meta=meta)
        self._keys[key] = entry
        self._append(entry)
        return entry

    def get(self, key: str) -> Optional[Entry]:
        return self._keys.get(key)

    def __len__(self) -> int:
        return len(self._keys)

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._keys.values())

    def __repr__(self) -> str:
        age = self.age
        when = "never" if age is None else f"{age:.0f}s ago"
        return f"<Ledger {len(self._keys)} keys, synced {when}>"

    # ------------------------------------------------------------- helpers

    @classmethod
    def temporary(cls, **kwargs: Any) -> "Ledger":
        """A file-backed ledger in a temp directory. Convenient for examples."""
        path = os.path.join(tempfile.mkdtemp(prefix="failclosed-"), "ledger.jsonl")
        return cls(path, **kwargs)
