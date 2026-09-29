"""failclosed — guards that refuse when they cannot prove an action is safe.

    from failclosed import Gate, Refuse, guard, Ledger, KillSwitch

    ledger = Ledger("state/sent.jsonl")
    halt = KillSwitch("state/HALT")

    @guard("not-already-sent")
    def not_already_sent(action):
        if ledger.contains(action.key):      # raises StaleLedger if stale
            raise Refuse(f"already sent to {action.key}")

    gate = Gate(not_already_sent, killswitch=halt)
    if gate.evaluate(action):
        send(action)

The design rule everything here follows: an action that cannot be proven safe
does not happen. Unknown and no are the same answer.
"""

from .guard import Decision, Gate, Guard, Refuse, Undetermined, guard
from .ledger import Entry, Ledger, StaleLedger
from .killswitch import KillSwitch
from .evals import Baseline, Case, CaseResult, Report, Suite

__version__ = "0.2.0"

__all__ = [
    "Decision", "Gate", "Guard", "Refuse", "Undetermined", "guard",
    "Ledger", "StaleLedger", "Entry",
    "KillSwitch",
    "Case", "CaseResult", "Suite", "Report", "Baseline",
]
