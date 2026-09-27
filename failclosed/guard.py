"""The gate: an action runs only if every guard affirmatively allows it.

The distinction this module exists to make:

    A guard that returns False when it cannot answer is a bug.
    A guard that *raises* when it cannot answer is the design.

Most guard code fails open. The check throws, the exception is swallowed a
frame or two up, and the action proceeds as though the check had passed. The
system is at its least safe exactly when it is least sure of itself. Here, an
unexpected exception inside a guard is a refusal — the same as an explicit
`Refuse` — because "I don't know" and "no" have to be the same answer for
anything that touches the outside world.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence

__all__ = [
    "Decision",
    "Refuse",
    "Undetermined",
    "Gate",
    "Guard",
    "guard",
]

log = logging.getLogger("failclosed")


class Refuse(Exception):
    """Raised by a guard to deny an action for a known, stated reason."""


class Undetermined(Exception):
    """Raised by a guard that cannot reach a verdict.

    Distinct from `Refuse` only in what it tells a human reading the log. The
    gate treats both as a denial, deliberately: an action whose safety is
    unknown is not a safe action.
    """


@dataclass(frozen=True)
class Decision:
    """The gate's verdict. Falsy when the action must not proceed."""

    allowed: bool
    reason: str
    guard: Optional[str] = None
    #: Guards that allowed the action before the deciding one ran.
    cleared: Sequence[str] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:
        verdict = "ALLOW" if self.allowed else "REFUSE"
        where = f" [{self.guard}]" if self.guard else ""
        return f"{verdict}{where}: {self.reason}"


#: A guard is any callable taking the action and returning None.
#: It denies by raising `Refuse` or `Undetermined`.
Guard = Callable[[Any], None]


def guard(name: str) -> Callable[[Guard], Guard]:
    """Name a guard so refusals are traceable to the check that caused them.

        @guard("recipient-opted-out")
        def not_opted_out(action):
            if suppression.contains(action.recipient):
                raise Refuse("recipient opted out")
    """

    def decorate(fn: Guard) -> Guard:
        fn.guard_name = name  # type: ignore[attr-defined]
        return fn

    return decorate


def _name_of(g: Guard) -> str:
    return getattr(g, "guard_name", None) or getattr(g, "__name__", repr(g))


class Gate:
    """Runs every guard in order and refuses on the first one that objects.

    Guards run in the order given, so put the cheap and the categorical first;
    there is no reason to query a ledger for a recipient who has opted out.

        gate = Gate(not_opted_out, not_already_sent, within_send_window)
        decision = gate.evaluate(action)
        if decision:
            send(action)
        else:
            log.warning("held: %s", decision)
    """

    def __init__(self, *guards: Guard, killswitch: Any = None) -> None:
        self._guards: List[Guard] = list(guards)
        self._killswitch = killswitch

    def __len__(self) -> int:
        return len(self._guards)

    @property
    def guards(self) -> Sequence[str]:
        return tuple(_name_of(g) for g in self._guards)

    def add(self, *guards: Guard) -> "Gate":
        self._guards.extend(guards)
        return self

    def evaluate(self, action: Any) -> Decision:
        """Return a `Decision`. Never raises, and never allows by accident."""
        try:
            return self._evaluate(action)
        except Exception as exc:  # noqa: BLE001 - a gate that cannot finish has not allowed
            # Reached only when describing a failure fails too: an exception
            # whose __str__ raises, or a guard whose name cannot be read.
            log.exception("gate could not finish; refusing")
            return Decision(False, f"gate could not finish ({type(exc).__name__})", guard="gate")

    def _evaluate(self, action: Any) -> Decision:
        if self._killswitch is not None:
            try:
                engaged = bool(self._killswitch.engaged)
            except Exception as exc:  # noqa: BLE001 - unreadable switch is engaged
                return Decision(
                    False,
                    f"kill switch unreadable ({exc.__class__.__name__}: {exc})",
                    guard="killswitch",
                )
            if engaged:
                try:
                    reason = getattr(self._killswitch, "reason", "") or "no reason recorded"
                except Exception as exc:  # noqa: BLE001 - engaged is engaged, reason or not
                    reason = f"reason unreadable ({exc.__class__.__name__}: {exc})"
                return Decision(False, f"kill switch engaged: {reason}", guard="killswitch")

        cleared: List[str] = []
        for g in self._guards:
            name = _name_of(g)
            try:
                g(action)
            except Refuse as exc:
                return Decision(False, str(exc) or "refused", guard=name, cleared=tuple(cleared))
            except Undetermined as exc:
                return Decision(
                    False,
                    f"undetermined: {exc}" if str(exc) else "undetermined",
                    guard=name,
                    cleared=tuple(cleared),
                )
            except Exception as exc:  # noqa: BLE001 - THE point of this module
                # An unexpected failure inside a check is not a passing check.
                log.exception("guard %s raised; refusing", name)
                return Decision(
                    False,
                    f"guard raised {exc.__class__.__name__}: {exc}",
                    guard=name,
                    cleared=tuple(cleared),
                )
            cleared.append(name)

        return Decision(True, f"cleared {len(cleared)} guard(s)", cleared=tuple(cleared))

    def evaluate_all(self, actions: Iterable[Any]) -> List[Decision]:
        return [self.evaluate(a) for a in actions]
