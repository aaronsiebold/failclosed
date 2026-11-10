"""Regression tests for the Greptile audit findings (audit PR #1)."""

import os
import sys
import tempfile
import textwrap
import unittest

from failclosed import Case, Gate, Ledger, Refuse, StaleLedger
from failclosed.evals import CaseResult
from failclosed.mutate import BaselineError, run, sites


class AsyncGuardTests(unittest.TestCase):
    def test_async_guard_that_refuses_does_not_fail_open(self):
        async def always_refuse(action):
            raise Refuse("no")

        d = Gate(always_refuse).evaluate("x")
        self.assertFalse(d.allowed)
        self.assertIn("awaitable", d.reason)


class ClockBackwardTests(unittest.TestCase):
    def test_clock_moving_backward_is_stale_not_fresh(self):
        t = [1000.0]
        ledger = Ledger(max_staleness=60, clock=lambda: t[0])
        ledger.sync(["a"])
        t[0] = 1000.0 - 3600
        self.assertFalse(ledger.is_fresh)
        with self.assertRaises(StaleLedger):
            ledger.require_fresh()


class AppendFailureTests(unittest.TestCase):
    def _failing_once(self, ledger):
        real = ledger._append
        state = {"n": 0}

        def flaky(entry):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("disk full")
            real(entry)

        ledger._append = flaky

    def test_sync_retry_after_failed_append_still_persists_the_key(self):
        path = os.path.join(tempfile.mkdtemp(), "l.jsonl")
        ledger = Ledger(path)
        self._failing_once(ledger)
        with self.assertRaises(OSError):
            ledger.sync(["k"])
        ledger.sync(["k"])
        self.assertIn("k", Ledger(path)._keys)

    def test_record_failed_append_is_not_remembered(self):
        ledger = Ledger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        self._failing_once(ledger)
        with self.assertRaises(OSError):
            ledger.record("k")
        self.assertIsNone(ledger.get("k"))


class MutateTests(unittest.TestCase):
    def test_failing_baseline_is_an_error_not_a_perfect_score(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "pkg"))
        with open(os.path.join(root, "pkg", "m.py"), "w") as fh:
            fh.write("def f(x):\n    return x + 1\n")
        with self.assertRaises(BaselineError):
            run(root, "pkg", test_cmd=[sys.executable, "-c", "raise SystemExit(1)"],
                verbose=False)

    def test_not_mutation_keeps_valid_syntax_for_not_paren(self):
        root = tempfile.mkdtemp()
        p = os.path.join(root, "m.py")
        with open(p, "w") as fh:
            fh.write("def f(flag):\n    return not(flag)\n")
        from failclosed.mutate import _apply
        found = [m for m in sites(p, relative_to=root) if m.original.startswith("not")]
        self.assertEqual(len(found), 1)
        out = "".join(_apply(open(p).readlines(), found[0]))
        compile(out, "m.py", "exec")


class EvalNumberTests(unittest.TestCase):
    def test_nan_drift_tolerance_is_rejected(self):
        for bad in (float("nan"), -0.1, float("inf")):
            with self.assertRaises(ValueError):
                Case("x", run=lambda: 1, check=bool, drift_tolerance=bad)

    def test_zero_tolerance_is_allowed_and_strict(self):
        Case("x", run=lambda: 1, check=bool, drift_tolerance=0.0)
        r = CaseResult("x", passes=7, trials=10, min_pass_rate=0.5,
                       baseline_rate=0.8, drift_tolerance=0.0)
        self.assertTrue(r.regressed)

    def test_nan_baseline_counts_as_regressed(self):
        r = CaseResult("x", passes=8, trials=10, min_pass_rate=0.5,
                       baseline_rate=float("nan"))
        self.assertTrue(r.regressed)

    def test_drop_exactly_equal_to_tolerance_is_allowed(self):
        r = CaseResult("x", passes=7, trials=10, min_pass_rate=0.5,
                       baseline_rate=0.8, drift_tolerance=0.1)
        self.assertFalse(r.regressed)


if __name__ == "__main__":
    unittest.main()
