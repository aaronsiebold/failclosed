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
which is exactly how fail-open behavior gets in.

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

## When a missing kill switch counts as clear

`engaged` is presence-based, so the hard case is absence. A missing file reads
clear only when its absence is proven: the nearest directory above it that
exists is a real directory. `touch state/HALT` fails when `state/` is missing,
and `engage()` creates the directory first, so nobody can have engaged a switch
inside a directory that does not exist. A missing directory proves the switch
is off.

A symlink to nothing proves nothing. If `state/` points at a volume that is not
mounted, the switch may be sitting on that volume. That is the "disk that went
away" case, and it reads engaged. So does a symlink loop, a file where a
directory should be, and any directory that cannot be searched.

One case this cannot see: an unmounted mount point is an ordinary empty
directory, so a switch that lives on a network mount reads clear while the mount
is gone. If that matters, keep the switch on local disk.

## Why a bad kill-switch path fails at construction

No file can have an empty name or one with a NUL byte, and macOS cannot store
a name that is not UTF-8 text. `lstat` finds nothing at such a path, so the
switch reads clear, and `engage()` raises, so it can never read anything else.
The constructor raises `ValueError` instead. Linux can store a name that is not
UTF-8, but the rule is the same there, so one config means the same thing on
every machine.

Reading such a switch as engaged would also fail closed. I chose the error
because the fault is known before any filesystem is asked, and a typo in config
should stop the program at startup rather than hold every send at 2am with a
reason that points at a file nobody can find.

The constructor sees only the path it was given, and `path` is a plain
attribute. Setting it to `''` afterwards made a gate allow every action while
`engage()` raised. So `engaged`, `reason` and `engaged_at` apply the same rule
each time they run. `engaged` promises to raise nothing, so there a bad path
reads engaged, and the reason names the problem: `unusable path (empty)`.

## Why the kill switch checks the type before it reads

A FIFO at the switch path blocks `open()` until something writes to it, and
`/dev/zero` never runs out of bytes. The first left `Gate.evaluate` waiting
forever. The second read until memory ran out. Neither allowed anything, but a
gate that never answers is an outage of its own.

So `reason` stats the path and reads only a regular file. Anything else answers
with its type ("unusual file type (FIFO), not read"). The open uses
`O_NONBLOCK` and the type is checked again on the open descriptor, in case a
FIFO replaced the file between the two calls. The read stops at 1 KiB, because
the reason goes into one-line logs.

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

Two survivors remain and are documented in the README. Both are `sort_keys=True`
in a `json.dumps` call — key ordering, not behavior.

Killing them would mean asserting on serialized byte order — a test that makes
the suite harder to change and catches nothing. The number is a diagnostic, not
a target. What matters is being able to say which claims are unchecked and why.

There used to be a third: `abs(drift) >= 0.01`, excused because `0.01` has no
exact binary float representation, so no input could distinguish `>` from `>=`.
That reason was wrong. In binary, 1/50 is exactly twice 0.01, so
`1/50 - 0.01 == 0.01`, and a case that passes 1 of 50 trials against a 0.01
baseline lands on the boundary. A test kills that mutant now. The reason next to
an allow-list entry is a claim too, and nobody had checked that one.

## Why no dependencies

A reviewer can clone this and run it in one command with no virtualenv, no
install step and no version resolution. For a library this small, `pytest`
would buy nicer parametrization and cost that. `unittest` was already there.

The eval harness is the place a dependency would be most defensible — but its
job is to run a callable N times and count, and that is not a hard problem worth
importing a framework for.
