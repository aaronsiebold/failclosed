"""Mutation testing: proving the tests would notice if the code were wrong.

A green suite tells you the tests ran. It does not tell you they asserted
anything. The check that separates the two is crude and decisive — break the
code on purpose and see whether the suite goes red:

    A guard that stays green when you inject a violation is asserting nothing.

That sentence came out of a real one. A duplicate-send guard had tests, the
tests passed, and the tests passed just as happily with the guard's body
deleted. They exercised the function and asserted on the wrong thing. Nobody
reviewing a green CI badge would ever have found it.

This runner flips one operator at a time — `>=` to `>`, `and` to `or`, `True`
to `False` — reruns the suite, and reports every mutant that *survived*. Each
survivor is a specific, located claim: change this and no test complains.

Mutation sites come from `tokenize`, not a regex, so comments and docstrings
are never touched and every mutant is real code.

    python -m failclosed.mutate                 # whole package
    python -m failclosed.mutate --target failclosed/guard.py
    python -m failclosed.mutate --list          # sites only, no runs
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["Mutation", "Survivor", "sites", "run"]

#: Operator swaps. Each flips one decision the code makes.
OP_MUTATIONS: Dict[str, str] = {
    ">=": ">",
    "<=": "<",
    ">": ">=",
    "<": "<=",
    "==": "!=",
    "!=": "==",
}
NAME_MUTATIONS: Dict[str, str] = {
    "and": "or",
    "or": "and",
    "True": "False",
    "False": "True",
}
#: `not x` -> `x`. Handled separately since it deletes rather than replaces.
NOT_TOKEN = "not"

DEFAULT_TEST_CMD = [sys.executable, "-m", "unittest", "discover", "-q", "-s", "tests"]

#: Set to "1" in every mutant's test run. A test that compares the source with
#: something outside it, like the README, must stand down when it sees this:
#: the source is wrong on purpose there, and a text mismatch is not a kill.
MUTANT_ENV = "FAILCLOSED_MUTANT_RUN"


@dataclass(frozen=True)
class Mutation:
    path: str          # relative to the repo root
    line: int
    col: int
    end_col: int
    original: str
    replacement: str

    @property
    def label(self) -> str:
        shown = self.replacement if self.replacement else "(deleted)"
        return f"{self.path}:{self.line} {self.original} -> {shown}"


@dataclass(frozen=True)
class Survivor:
    mutation: Mutation
    source_line: str

    @property
    def key(self) -> str:
        """Identity of a survivor, stable across edits elsewhere in the file.

        Deliberately not line-numbered: an allow-list keyed on line numbers
        silently starts excusing a different mutant the moment someone adds an
        import, which is the worst possible failure for a safety allow-list.
        """
        return f"{self.mutation.path} :: {self.source_line.strip()} :: {self.mutation.original}->{self.mutation.replacement}"


ALLOW_FILE = ".mutants-allow"


def load_allowed(root: str) -> Dict[str, str]:
    """Known equivalent mutants: `key  # why it cannot be killed`.

    Equivalent mutants are real — `sort_keys=True` changes byte order, not
    behavior, and no input distinguishes them. The choice is between an
    allow-list with a stated reason per entry and a permanently red build that
    everyone learns to ignore. The first is honest; the second is decoration.
    """
    path = os.path.join(root, ALLOW_FILE)
    allowed: Dict[str, str] = {}
    # Not os.path.exists(): it answers False for a file it cannot read.
    # Only a file that is truly absent means an empty allow-list.
    try:
        fh = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        return allowed
    with fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, _, why = line.partition("  # ")
            allowed[key.strip()] = why.strip() or "no reason given"
    return allowed


def sites(path: str, *, relative_to: Optional[str] = None) -> List[Mutation]:
    """Every mutation site in one file, found via tokenize."""
    rel = os.path.relpath(path, relative_to) if relative_to else path
    with open(path, "rb") as fh:
        source = fh.read()
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return []

    found: List[Mutation] = []
    for tok in tokens:
        # STRING and COMMENT tokens are skipped implicitly: we only look at
        # OP and NAME, so docstrings and comments can never be mutated.
        if tok.type == tokenize.OP and tok.string in OP_MUTATIONS:
            found.append(
                Mutation(rel, tok.start[0], tok.start[1], tok.end[1],
                         tok.string, OP_MUTATIONS[tok.string])
            )
        elif tok.type == tokenize.NAME:
            if tok.string in NAME_MUTATIONS:
                found.append(
                    Mutation(rel, tok.start[0], tok.start[1], tok.end[1],
                             tok.string, NAME_MUTATIONS[tok.string])
                )
            elif tok.string == NOT_TOKEN:
                # Drop the `not` and the space after it.
                found.append(
                    Mutation(rel, tok.start[0], tok.start[1], tok.end[1] + 1, "not ", "")
                )
    return found


def _child_env() -> Dict[str, str]:
    """Environment for a mutant test run.

    `PYTHONDONTWRITEBYTECODE` is not a tidiness preference — it is load-bearing.
    CPython decides a cached `.pyc` is still valid from the source's mtime and
    size. A one-character mutation (`>=` to `>`) shortens the file by one byte,
    and the next mutation of the same length, written in the same mtime second,
    matches a `.pyc` already cached from an earlier mutant. Python then runs the
    *stale* bytecode, the suite passes, and the runner reports a survivor that
    was never actually tested.

    This runner reported four false survivors that way before the fix — which is
    the library's own thesis turned on its author: a check that silently reads
    stale state is worse than no check, because it is believed.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[MUTANT_ENV] = "1"
    return env


def _apply(source_lines: List[str], m: Mutation) -> List[str]:
    out = list(source_lines)
    line = out[m.line - 1]
    out[m.line - 1] = line[: m.col] + m.replacement + line[m.end_col :]
    return out


def _python_files(root: str, package: str) -> List[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, package)):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py") and name != "mutate.py":
                found.append(os.path.join(dirpath, name))
    return found


def run(
    root: str = ".",
    package: str = "failclosed",
    *,
    target: Optional[str] = None,
    test_cmd: Sequence[str] = DEFAULT_TEST_CMD,
    timeout: float = 60.0,
    verbose: bool = True,
) -> Tuple[int, List[Survivor]]:
    """Run every mutant. Returns (killed, survivors).

    The repo is copied to a temp directory per run, so the working tree is
    never modified — a mutation runner that can leave your source broken is
    not one you will use.
    """
    root = os.path.abspath(root)
    files = [os.path.join(root, target)] if target else _python_files(root, package)
    mutations: List[Mutation] = []
    for f in files:
        mutations.extend(sites(f, relative_to=root))

    if verbose:
        print(f"{len(mutations)} mutation site(s) across {len(files)} file(s)\n")

    killed = 0
    survivors: List[Survivor] = []
    started = time.perf_counter()

    workdir = tempfile.mkdtemp(prefix="failclosed-mutate-")
    sandbox = os.path.join(workdir, "repo")
    shutil.copytree(
        root, sandbox,
        ignore=shutil.ignore_patterns("__pycache__", ".git", ".mypy_cache", "*.pyc"),
    )
    try:
        originals: Dict[str, List[str]] = {}
        for m in mutations:
            if m.path not in originals:
                with open(os.path.join(sandbox, m.path), "r", encoding="utf-8") as fh:
                    originals[m.path] = fh.readlines()

        for i, m in enumerate(mutations, 1):
            full = os.path.join(sandbox, m.path)
            pristine = originals[m.path]
            try:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.writelines(_apply(pristine, m))
                try:
                    proc = subprocess.run(
                        list(test_cmd), cwd=sandbox, timeout=timeout,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=_child_env(),
                    )
                    survived = proc.returncode == 0
                except subprocess.TimeoutExpired:
                    # A hang is a detected mutant: behavior changed observably.
                    survived = False
            finally:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.writelines(pristine)

            if survived:
                survivors.append(Survivor(m, pristine[m.line - 1].rstrip()))
                mark = "SURVIVED"
            else:
                killed += 1
                mark = "killed"
            if verbose:
                print(f"  [{i:>3}/{len(mutations)}] {mark:<8} {m.label}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    allowed = load_allowed(root)
    known = [s for s in survivors if s.key in allowed]
    unexpected = [s for s in survivors if s.key not in allowed]

    if verbose:
        elapsed = time.perf_counter() - started
        total = killed + len(survivors)
        score = (killed / total * 100) if total else 100.0
        print(f"\n{killed}/{total} mutants killed  ({score:.1f}%)  in {elapsed:.1f}s")
        if known:
            print(f"\n{len(known)} known equivalent mutant(s), allow-listed:\n")
            for s in known:
                print(f"  {s.mutation.label}")
                print(f"      {allowed[s.key]}")
        if unexpected:
            print(f"\n{len(unexpected)} SURVIVOR(S) — no test objected to these changes:\n")
            for s in unexpected:
                print(f"  {s.mutation.label}")
                print(f"      {s.source_line.strip()}")
            print(
                f"\nEither write a test that fails when this changes, or add it to "
                f"{ALLOW_FILE} with a reason:\n"
            )
            for s in unexpected:
                print(f"  {s.key}  # why this cannot be killed")
        elif not known:
            print("\nNo survivors.")
    return killed, unexpected


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="failclosed.mutate", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--package", default="failclosed", help="package to mutate")
    ap.add_argument("--target", help="single file to mutate, relative to root")
    ap.add_argument("--list", action="store_true", dest="list_only",
                    help="list mutation sites without running the suite")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    if args.list_only:
        root = os.path.abspath(args.root)
        files = [os.path.join(root, args.target)] if args.target else _python_files(root, args.package)
        n = 0
        for f in files:
            for m in sites(f, relative_to=root):
                print(m.label)
                n += 1
        print(f"\n{n} site(s)")
        return 0

    _, survivors = run(args.root, args.package, target=args.target, timeout=args.timeout)
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
