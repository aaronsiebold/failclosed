# Design notes

Why each decision went the way it did, including the ones I got wrong first.

---

## Why exceptions rather than booleans

A guard could return `True`/`False`. It doesn't, because a boolean has no room
for the third answer, and the third answer is the whole problem:

| | meaning | what a boolean does |
|---|---|---|
| allow | proven safe | `True` |
| refuse | proven unsafe | `False` |
| **undetermined** | **cannot tell** | **silently becomes one of the above** |

Whichever way you map "cannot tell", you are wrong half the time. Map it to
`True` and an outage becomes a send. Map it to `False` and a transient blip
halts a campaign with no explanation, so somebody adds a retry that swallows it,
and you are back to the first case with extra steps.

Raising keeps the three states distinct and carries a *reason* with the refusal.
`Refuse` and `Undetermined` are separate types purely so a human reading the log
can tell "policy said no" from "we couldn't find out" — the gate treats both as
denial.

The gate then catches `Exception` broadly, which normally deserves a code
review objection. Here it is the entire point. A guard that raises `KeyError` on
a malformed action has not passed. The alternative — letting it propagate — puts
the decision in the hands of whatever `try` block happens to be up the stack,
which is exactly how fail-open behaviour gets in.

## Why the ledger refuses instead of returning False

The first version had `contains()` return `False` when the ledger was stale, and
a separate `is_fresh` property callers were supposed to check. That is a
`strcpy` API: correct only if every caller remembers, and silent when they
don't.

Making the common path raise means the mistake is impossible rather than merely
discouraged. `StaleLedger` subclasses `Undetermined` so it needs no handling at
the call site — it flows through the gate as a refusal on its own.

`is_fresh` still exists, for the case where a caller genuinely wants to branch
on freshness. It is just no longer load-bearing.

## Why sync() returns a count

```python
unseen = ledger.sync(keys_from_the_provider())
```

If the authoritative source knows about keys we don't, something acted outside
this system: a second sender, a manual send, a replayed job. That number is the
earliest available signal for a whole class of incidents, and throwing it away
would be a waste. It is a return value rather than a log line because a caller
should be able to alert on it.

## Why the freshness window is fifteen minutes

Long enough to survive a slow or briefly failed sync. Short enough that a dead
sync job surfaces inside one working session rather than overnight. It is a
default, not a law — pass `max_staleness` per ledger.

The direction of the tradeoff is the point: a window too short causes visible,
annoying, self-correcting refusals; a window too long causes invisible wrong
answers. Prefer the annoying failure.

## Why mutation sites come from tokenize

The first version used regular expressions over the source. It generated
mutants inside docstrings and comments, all of which survived — every one a
false positive burying the real signal, and the real signal is the only thing
the tool is for.

`tokenize` yields `COMMENT` and `STRING` as distinct token types. Looking only
at `OP` and `NAME` tokens means comments and docstrings are unreachable by
construction rather than by filtering, and each mutation gets exact
line/column bounds instead of a substring match that might hit the wrong
occurrence.

## Why the runner copies the repo

An in-place mutation runner can leave your working tree broken — a crash, a
`Ctrl-C`, a bug in the restore path. Once that happens once, you stop running
it, and a tool you don't run has no value. Copying to a temp directory makes
the failure mode "the run is lost", which costs eleven seconds.

## The bug in the runner

The first full run reported 68.8%, and four of the survivors were killed when I
checked them by hand. That gap is the interesting part of this repo.

CPython validates a cached `.pyc` against the source file's **mtime and size**.
Mutating `>=` to `>` shortens the file by one byte. The next mutation of the
same length, written within the same mtime second, matched a `.pyc` cached from
an earlier mutant — so Python ran the *previous* mutant's bytecode, the suite
passed, and the runner reported a survivor that had never actually been tested.

The fix is `PYTHONDONTWRITEBYTECODE=1` in the child environment.

Two things worth keeping from it:

1. **The tool that checks your assumptions has assumptions.** I only caught it
   because a survivor looked wrong — `meets_floor` had an explicit boundary
   test. If the tests had been weaker, the false result would have been
   plausible and I would have "fixed" a non-problem.
2. **It is the same bug the library is about.** A check silently reading stale
   state, reporting confidently, and being believed. I wrote the ledger to stop
   exactly this and then shipped it in the mutation runner.

## Why not chase 100% mutation score

Three survivors remain and are documented in the README. Two are `sort_keys=True`
in a `json.dumps` call — key ordering, not behaviour. One is
`abs(drift) >= 0.01`, where `0.01` has no exact binary float representation, so
no input can distinguish `>` from `>=`.

Killing them would mean asserting on serialised byte order and constructing a
float equality that cannot occur — tests that make the suite harder to change
and catch nothing. The number is a diagnostic, not a target. What matters is
being able to say which claims are unchecked and why.

## Why no dependencies

A reviewer can clone this and run it in one command with no virtualenv, no
install step and no version resolution. For a library this small, `pytest`
would buy nicer parametrisation and cost that. `unittest` was already there.

The eval harness is the place a dependency would be most defensible — but its
job is to run a callable N times and count, and that is not a hard problem worth
importing a framework for.
