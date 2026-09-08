# failclosed

[![ci](https://github.com/aaronsiebold/failclosed/actions/workflows/ci.yml/badge.svg)](https://github.com/aaronsiebold/failclosed/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![mutation score](https://img.shields.io/badge/mutants%20killed-45%2F48-brightgreen)

**A guard that stays green when you inject a violation is asserting nothing.**

That sentence cost me a weekend. I had a duplicate-send guard on an outbound
system. It had tests. The tests passed. The tests also passed with the guard's
body deleted — they exercised the function and asserted on the wrong thing.
Nothing in a green CI badge would ever have told me.

This library is the pattern that came out of fixing it, generalised and written
from scratch. Three ideas, each small:

1. **An action that cannot be proven safe does not happen.** Unknown and no are
   the same answer.
2. **A guard whose input can go stale cannot catch that input going stale** — so
   the record refuses to answer once it is too old, rather than confidently
   answering wrong.
3. **A test suite is a claim you have to check.** Break the code on purpose. If
   the suite stays green, it was never testing that.

Python 3.9+, standard library only. No dependencies, nothing to install.

```bash
git clone https://github.com/aaronsiebold/failclosed
cd failclosed
python3 -m unittest discover -s tests   # 90 tests, ~0.1s
python3 examples/outbound.py            # the worked example
python3 -m failclosed.mutate            # break the code, watch tests catch it
```

---

## The problem

Most guard code fails **open**. The shape is so common it looks like good
practice:

```python
def is_safe_to_send(recipient):
    try:
        return not already_sent(recipient)
    except Exception:
        log.warning("could not check; proceeding")
        return True          # <- the bug
```

The check throws — the API is down, the token expired, the file is locked — and
the system proceeds *as though the check had passed*. It is at its least safe
exactly when it is least sure of itself. And it is silent, because from the
outside a passing check and a skipped check look identical.

`failclosed` inverts that. A guard denies by raising. The gate catches
everything, including exceptions the guard never anticipated, and every one of
them is a refusal.

```python
from failclosed import Gate, Refuse, guard

@guard("not-opted-out")
def not_opted_out(action):
    if suppression.contains(action["to"]):
        raise Refuse(f"{action['to']} opted out")

gate = Gate(has_address, not_opted_out, not_already_sent, killswitch=halt)

decision = gate.evaluate(action)
if decision:
    send(action)
else:
    log.warning("held: %s", decision)
    # held: REFUSE [not-opted-out]: sam@example.edu opted out
```

`Gate.evaluate` never raises and never allows by accident. If a guard throws a
`KeyError` on a malformed action, that is a refusal with the exception in the
reason — not a traceback, and not a send.

---

## The stale-input problem

This is the failure I actually shipped, and the one worth the most attention.

A local queue said twenty-three messages had gone out. The mailbox said five
hundred and fifty-three. Every guard reading that queue passed, and passed
*because* the queue was out of date. No guard was broken. Their input was.

A plain `set` cannot express this, because a stale set and an empty set answer
`False` identically. So `Ledger` tracks the age of its own knowledge and
**refuses to answer** once that knowledge expires:

```python
ledger = Ledger("state/sent.jsonl", max_staleness=900)   # 15 minutes
ledger.sync(keys_from_the_mail_provider())               # authoritative source

ledger.contains("alex@example.edu")   # True / False while fresh
# ... 16 minutes later, with no sync ...
ledger.contains("alex@example.edu")   # raises StaleLedger
```

`StaleLedger` subclasses `Undetermined`, so a `Gate` treats it as a refusal with
no special handling at the call site. The send stops, and the log says why:

```
REFUSE [not-already-sent]: undetermined: ledger last synced 960s ago, limit is 900s
```

Two details that matter more than they look:

- **`sync()` reads the authoritative source** — where the action really happens
  — not our own queue of what we intended to do. It returns the count of keys
  the source knew about and we did not, which is a number worth alerting on: it
  means something acted outside this system.
- **A ledger reloaded from disk is stale until synced.** It has the data. It has
  not verified it. Those are different, and conflating them is the original bug.

---

## Evaluating things that don't return the same answer twice

A normal test asserts an output equals a value. That contract does not hold for
a model. Assert on one sample of a distribution and you get a suite that goes
red on Tuesday for no reason, and a team that learns to re-run red suites —
which is worse than having no suite.

So a case runs N times and asserts on the **rate**:

```python
from failclosed import Case, Suite, Baseline

suite = Suite(
    [Case("refuses to quote a price",
          run=lambda: model(prompt),
          check=lambda out: not contains_number(out),
          trials=20, min_pass_rate=0.95)],
    baseline=Baseline.load("evals/baseline.json"),
)

report = suite.run()
print(report.summary())
report.save_baseline("evals/baseline.json")
```

Two thresholds, answering different questions:

| | question | fails when |
|---|---|---|
| `min_pass_rate` | is this behaviour broken? | rate drops below a fixed floor |
| baseline + `drift_tolerance` | has it got worse? | rate falls well below last run |

The second is the one you actually want. A case that ran at 98% and now runs at
82% has regressed, even though 82% clears an 80% floor somebody picked months
ago. **Drift is visible in the change, long before it crosses a line.**

Failed trials keep an excerpt of what came back, because "it failed" is useless
and "it returned this" is where you start.

---

## Proving the tests assert something

The runner mutates one operator at a time — `>=` to `>`, `and` to `or`, `True`
to `False`, deleting a `not` — reruns the suite, and reports every mutant that
**survived**. Each survivor is a located claim: *change this and no test
complains.*

Mutation sites come from `tokenize`, not a regex, so comments and docstrings are
never touched and every mutant is real code.

```
$ python3 -m failclosed.mutate

48 mutation site(s) across 5 file(s)

  [  1/48] killed   failclosed/evals.py:58 < -> <=
  [  2/48] killed   failclosed/evals.py:60 <= -> <
  ...
  [ 48/48] killed   failclosed/ledger.py:156 not  -> (deleted)

45/48 mutants killed  (93.8%)  in 20.1s

3 known equivalent mutant(s), allow-listed:

  failclosed/evals.py:107 >= -> >
      0.01 has no exact binary float representation, so the equality case is
      unreachable and no input distinguishes > from >=
  failclosed/evals.py:142 True -> False
      sort_keys changes key order in the serialised JSON, not behaviour
  failclosed/ledger.py:55 True -> False
      as above: byte order of a serialised entry, not behaviour
```

**All three survivors are equivalent mutants**, listed in `.mutants-allow` with
a reason each. Anything *not* on that list fails the build:

- `abs(drift) >= 0.01` — `0.01` has no exact binary float representation, so the
  equality case is unreachable and `>` and `>=` cannot be distinguished by any
  input. A test here would be theatre.
- `sort_keys=True` (twice) — changes the byte order of keys in serialised JSON,
  not behaviour. Both files round-trip identically either way.

Chasing 100% would mean asserting on serialised byte order and constructing a
float equality that cannot occur — tests that make the suite harder to change
and catch nothing. The number is not the goal; knowing *which* claims are
unchecked is.

The allow-list is keyed on the source line, not the line number. A
line-numbered allow-list silently starts excusing a different mutant the moment
someone adds an import above it, which is the worst possible failure mode for a
safety allow-list.

### Does it actually catch anything?

Delete the two tests that pin the kill switch's fail-closed branch and rerun:

```
$ python3 -m unittest discover -s tests
OK                                    # 88 tests, still green

$ python3 -m failclosed.mutate
1 SURVIVOR(S) — no test objected to these changes:

  failclosed/killswitch.py:45 True -> False
      return True
$ echo $?
1
```

The suite is green and wrong. `return True` is the line that makes an unreadable
switch count as engaged — the single most important line in that file — and
nothing was checking it. That is the gap this tool exists to find, and it is why
CI runs it as a separate required job rather than trusting the green tick.

### What this found in its own runner

The first full run reported **68.8%** — with four survivors that a manual check
proved were killed. The runner was lying.

CPython decides a cached `.pyc` is still valid from the source file's mtime and
size. A one-character mutation (`>=` to `>`) shortens the file by one byte; the
next mutation of the same length, written in the same mtime second, matched a
`.pyc` already cached from an earlier mutant. Python ran the **stale bytecode**,
the suite passed, and the runner reported a survivor that had never been tested.

The fix is one line — `PYTHONDONTWRITEBYTECODE=1` in the child environment — and
the score went to 72.9%, then to 93.8% once the real gaps had tests. The bug is
the library's own thesis turned on its author: a check that silently reads stale
state is worse than no check, because it is believed.

---

## The kill switch

```python
halt = KillSwitch("state/HALT")
gate = Gate(*guards, killswitch=halt)

halt.engage("bad merge template shipped in 4f21a9c")
```

Two properties matter more than the feature:

- **A human can engage it without deploying.** `touch state/HALT` from a phone
  beats a revert, a CI run and a rollout.
- **An unreadable switch is an engaged switch.** If the check itself fails, the
  answer is stop. Any other design makes the switch useless in exactly the
  conditions that would make you reach for it.

---

## The worked example

`python3 examples/outbound.py` stages five failures against one gate. A
conventional chain lets three through:

```
1. A clean send                          SEND
2. The same person again                 HOLD  already contacted
3. Someone the provider contacted, who   HOLD  already contacted
   is absent from our own queue
4. Sixteen minutes later, no sync        HOLD  ledger last synced 960s ago
   (nothing broken, nothing changed)     SEND  — after re-syncing
5. A human pulls the switch mid-run      HOLD  bad merge template in 4f21a9c
```

Case 3 is the one worth reading twice: our own records had no idea, and the send
still stopped, because the ledger is synced from where the action really
happens. Case 4 is the one that is hardest to get right: nothing failed, no
guard changed, and the correct behaviour is still to stop.

---

## Layout

```
failclosed/guard.py       Gate, Refuse, Undetermined, @guard  — the fail-closed core
failclosed/ledger.py      Ledger, StaleLedger                 — a record that knows its own age
failclosed/killswitch.py  KillSwitch                          — one file that stops everything
failclosed/evals.py       Case, Suite, Baseline, Report       — rate-based eval + drift detection
failclosed/mutate.py      the mutation runner
tests/                    90 tests, standard library unittest
examples/outbound.py      five staged failures against one gate
docs/DESIGN.md            why each decision went the way it did
```

## License

MIT. Written by [Aaron Siebold](https://github.com/aaronsiebold). Nothing here is
copied from any employer's codebase; it is the pattern, rebuilt from first
principles, with the failures it was designed around described in the open.
