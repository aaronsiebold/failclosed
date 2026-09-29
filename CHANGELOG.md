# Changelog

Notable changes to failclosed. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-28

Most fixes in this release are one bug in different places: a check that could
not look answered as if it had looked and found nothing.

### Changed

- **Breaking:** `KillSwitch(path)` refuses a path no switch file can be at. An
  empty path (`''`, `b''`), a path with a NUL byte and a path that is not UTF-8
  text (a lone surrogate, invalid UTF-8 bytes) raise `ValueError`. A value that
  is not a path (`None`, `0`, `1.5`) raises `TypeError`. In 0.1.0 the first
  group built a switch that read clear forever, because `engage()` could not
  create the file; `KillSwitch(0)` checked file descriptor 0, and
  `KillSwitch(None).engaged` raised.
- **Breaking:** `Ledger(path)` and `Baseline.load(path)` raise `OSError` when
  they cannot read the file, unless it is missing. In 0.1.0 a file inside a
  directory the process could not search loaded as empty: the ledger forgot
  every recorded send, and drift detection turned off. A missing file still
  starts empty.
- `KillSwitch.path` is always a `str`. Bytes and path-like arguments are
  decoded with `os.fsdecode`.
- A bad `path` assigned after construction reads engaged. `reason` says why
  (`unusable path (empty)`, `(contains a NUL byte)`, `(not UTF-8 text)` or
  `(not a path)`) and `engaged_at` is `None`.
- `KillSwitch.reason` reads only a regular file, and at most `REASON_BYTES`
  (1024) of it. A directory, FIFO, socket or device answers
  `unusual file type (<kind>), not read` and is never opened. A first line
  that is not valid UTF-8 comes back with replacement characters instead of
  as `""`.
- The mutation runner raises on an unreadable `.mutants-allow` instead of
  treating it as an empty allow-list.

### Fixed

- `KillSwitch.engaged` read clear when the switch could not be checked.
  `os.path.exists` catches `OSError` and answers `False`, so a switch inside a
  directory the process could not search let every action through. `engaged`
  now calls `os.lstat`, and only a proven absence reads clear.
- A dangling symlink at the switch path read clear. It reads engaged.
- A switch whose parent directory is a symlink to nothing (a volume that went
  away) read clear. It reads engaged. A plain missing directory still reads
  clear.
- `KillSwitch.reason` could hang the gate in front of it: `open()` on a FIFO
  blocks until a writer arrives, and a device like `/dev/zero` never ends.
- `Gate.evaluate` could raise, against its own docstring. It refuses instead:
  a kill switch whose `reason` raises gives
  `kill switch engaged: reason unreadable (<type>: <message>)`, and an
  exception whose `__str__` raises, or a guard whose name lookup raises, gives
  `gate could not finish (<type>)` with guard `gate`.

### Added

- `failclosed.killswitch.REASON_BYTES`, the most `reason` reads.
- `failclosed.mutate.MUTANT_ENV`. The runner sets `FAILCLOSED_MUTANT_RUN=1` in
  every mutant's test run, so a test that compares the source with outside
  text, like the README, can stand down.
- Tests that fail on any `os.path` or `pathlib` predicate that swallows
  `OSError` (`exists`, `isfile`, `is_dir` and the rest) inside `failclosed/`,
  check every number in the README against the repo, and keep the version in
  `pyproject.toml`, `failclosed.__version__` and this file in step.
- CI runs on Python 3.14.

## [0.1.0] - 2026-09-08

- First release: `Gate` and `@guard`, `Ledger`, `KillSwitch`, the eval harness with drift detection, and the mutation runner.
