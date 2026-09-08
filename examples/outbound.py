"""A sender that cannot contact the same person twice, even when things break.

Run it:

    python examples/outbound.py

The scenario is outbound email because that is where I hit these problems, but
nothing here is about email. It is about any automation that acts on the
outside world and therefore cannot take its own word for what it has already
done.

Five failures are staged in order. A conventional guard chain lets three of
them through. This one lets none through, and — the part that matters — it
stops for a *different, stated* reason each time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from failclosed import Gate, KillSwitch, Ledger, Refuse, guard  # noqa: E402


class Clock:
    """A hand-cranked clock so the example is deterministic."""

    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


clock = Clock()
ledger = Ledger(max_staleness=900, clock=clock)   # 15 minutes
suppression = {"noreply@example.edu", "optout@example.edu"}
halt = KillSwitch(str(Path(__file__).resolve().parent / ".halt"))


# --------------------------------------------------------------------------
# The guards. Cheap and categorical first — never query a ledger for someone
# who has already opted out.
# --------------------------------------------------------------------------

@guard("has-address")
def has_address(action):
    if not action.get("to"):
        raise Refuse("no recipient address")


@guard("not-opted-out")
def not_opted_out(action):
    if action["to"] in suppression:
        raise Refuse(f"{action['to']} opted out")


@guard("not-already-sent")
def not_already_sent(action):
    # `contains` raises StaleLedger — a subclass of Undetermined — when our
    # picture of what has been sent is too old to be worth consulting.
    if ledger.contains(action["to"]):
        raise Refuse(f"already contacted {action['to']}")


@guard("within-send-window")
def within_send_window(action):
    hour = action.get("local_hour")
    if hour is None:
        # We do not know the recipient's timezone. That is not permission.
        raise Refuse("recipient local time unknown")
    if not 8 <= hour < 18:
        raise Refuse(f"local time {hour}:00 is outside the 08:00-18:00 window")


gate = Gate(
    has_address,
    not_opted_out,
    not_already_sent,
    within_send_window,
    killswitch=halt,
)


def attempt(label, action):
    decision = gate.evaluate(action)
    mark = "SEND  " if decision else "HOLD  "
    print(f"  {mark} {label}")
    print(f"         {decision}")
    if decision:
        ledger.record(action["to"], subject=action.get("subject", ""))
    return decision


def main():
    halt.release()  # clean slate if a previous run left it engaged
    print(__doc__.strip().splitlines()[0])
    print("=" * 74)

    # The authoritative source is the mail provider's record of what was
    # actually sent — not our own queue of what we meant to send.
    already_sent_according_to_the_provider = ["dana@example.edu"]
    ledger.sync(already_sent_according_to_the_provider)
    print(f"\nsynced with provider: {ledger!r}\n")

    print("1. A clean send")
    attempt("alex@example.edu", {"to": "alex@example.edu", "local_hour": 10,
                                "subject": "Quick question"})

    print("\n2. The same person again — the ordinary duplicate")
    attempt("alex@example.edu", {"to": "alex@example.edu", "local_hour": 11,
                                "subject": "Following up"})

    print("\n3. Someone the provider already contacted, who is absent from our"
          "\n   own queue entirely")
    attempt("dana@example.edu", {"to": "dana@example.edu", "local_hour": 9,
                                "subject": "Quick question"})

    print("\n4. Sixteen minutes later. Nothing is broken and no guard has"
          "\n   changed — but the ledger's view of the world has expired, so it"
          "\n   refuses to answer rather than answering wrongly.")
    clock.advance(16 * 60)
    attempt("jordan@example.edu", {"to": "jordan@example.edu", "local_hour": 10,
                                  "subject": "Quick question"})

    print("\n   Re-synced against the provider, the same send proceeds:")
    ledger.sync(already_sent_according_to_the_provider)
    attempt("jordan@example.edu", {"to": "jordan@example.edu", "local_hour": 10,
                                  "subject": "Quick question"})

    print("\n5. A human pulls the switch mid-run")
    halt.engage("bad merge template shipped in 4f21a9c")
    attempt("sam@example.edu", {"to": "sam@example.edu", "local_hour": 10,
                               "subject": "Quick question"})
    halt.release()

    print("\n" + "=" * 74)
    print(f"ledger holds {len(ledger)} recipient(s): "
          f"{', '.join(sorted(e.key for e in ledger))}")
    print("\nNobody was contacted twice. Every hold names the guard that held it.")


if __name__ == "__main__":
    main()
