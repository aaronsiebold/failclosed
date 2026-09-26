import json
import os
import random
import tempfile
import unittest

from failclosed import Baseline, Case, Report, Suite
from failclosed.evals import CaseResult


def flaky(rate, seed=0):
    """A stand-in for a model: returns 'ok' `rate` of the time, seeded."""
    rng = random.Random(seed)

    def run():
        return "ok" if rng.random() < rate else "wrong"

    return run


def is_ok(output):
    return output == "ok"


class CaseValidationTests(unittest.TestCase):
    def test_trials_must_be_positive(self):
        with self.assertRaises(ValueError):
            Case("x", run=flaky(1.0), check=is_ok, trials=0)

    def test_pass_rate_must_be_a_probability(self):
        with self.assertRaises(ValueError):
            Case("x", run=flaky(1.0), check=is_ok, min_pass_rate=1.5)
        with self.assertRaises(ValueError):
            Case("x", run=flaky(1.0), check=is_ok, min_pass_rate=-0.1)


class RateTests(unittest.TestCase):
    def test_a_deterministic_pass_scores_one(self):
        report = Suite([Case("always", run=lambda: "ok", check=is_ok, trials=8)]).run()
        r = report.results[0]
        self.assertEqual(r.passes, 8)
        self.assertEqual(r.pass_rate, 1.0)
        self.assertTrue(r.ok)
        self.assertTrue(report.ok)

    def test_a_deterministic_failure_scores_zero(self):
        report = Suite([Case("never", run=lambda: "wrong", check=is_ok, trials=8)]).run()
        r = report.results[0]
        self.assertEqual(r.passes, 0)
        self.assertEqual(r.pass_rate, 0.0)
        self.assertFalse(r.ok)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 1)

    def test_a_rate_above_the_floor_passes(self):
        # Seeded, so this is deterministic: measured at 0.9 over 100 trials.
        case = Case("mostly", run=flaky(0.9, seed=1), check=is_ok,
                    trials=100, min_pass_rate=0.8)
        r = Suite([case]).run().results[0]
        self.assertGreaterEqual(r.pass_rate, 0.8)
        self.assertTrue(r.meets_floor)
        self.assertTrue(r.ok)

    def test_a_rate_below_the_floor_fails(self):
        case = Case("rarely", run=flaky(0.4, seed=1), check=is_ok,
                    trials=100, min_pass_rate=0.8)
        r = Suite([case]).run().results[0]
        self.assertLess(r.pass_rate, 0.8)
        self.assertFalse(r.meets_floor)
        self.assertFalse(r.ok)

    def test_exactly_at_the_floor_passes(self):
        r = CaseResult(name="edge", passes=8, trials=10, min_pass_rate=0.8)
        self.assertEqual(r.pass_rate, 0.8)
        self.assertTrue(r.meets_floor)

    def test_one_trial_below_the_floor_fails(self):
        r = CaseResult(name="edge", passes=7, trials=10, min_pass_rate=0.8)
        self.assertFalse(r.meets_floor)

    def test_failures_are_captured_for_inspection(self):
        report = Suite([Case("never", run=lambda: "wrong", check=is_ok, trials=3)]).run()
        self.assertEqual(report.results[0].failures, ["wrong", "wrong", "wrong"])

    def test_only_the_first_five_failures_are_kept(self):
        report = Suite([Case("never", run=lambda: "wrong", check=is_ok, trials=20)]).run()
        self.assertEqual(len(report.results[0].failures), 5)


class ErrorHandlingTests(unittest.TestCase):
    def test_a_raising_run_counts_as_a_failed_trial(self):
        def boom():
            raise RuntimeError("model timeout")

        r = Suite([Case("raises", run=boom, check=is_ok, trials=4)]).run().results[0]
        self.assertEqual(r.passes, 0)
        self.assertEqual(len(r.errors), 4)
        self.assertIn("model timeout", r.errors[0])
        self.assertFalse(r.ok)

    def test_a_raising_check_counts_as_a_failed_trial(self):
        def bad_check(output):
            raise ValueError("bad checker")

        r = Suite([Case("bad", run=lambda: "ok", check=bad_check, trials=3)]).run().results[0]
        self.assertEqual(r.passes, 0)
        self.assertIn("check raised", r.errors[0])

    def test_one_bad_case_does_not_stop_the_others(self):
        def boom():
            raise RuntimeError("x")

        report = Suite([
            Case("bad", run=boom, check=is_ok, trials=2),
            Case("good", run=lambda: "ok", check=is_ok, trials=2),
        ]).run()
        self.assertEqual(len(report.results), 2)
        self.assertTrue(report.results[1].ok)


class DriftTests(unittest.TestCase):
    def test_no_baseline_means_no_drift(self):
        r = Suite([Case("x", run=lambda: "ok", check=is_ok, trials=4)]).run().results[0]
        self.assertIsNone(r.drift)
        self.assertFalse(r.regressed)

    def test_a_drop_beyond_tolerance_is_a_regression_even_above_the_floor(self):
        # 0.82 clears a 0.80 floor but is 16 points below a 0.98 baseline.
        r = CaseResult(name="x", passes=82, trials=100, min_pass_rate=0.8,
                       baseline_rate=0.98, drift_tolerance=0.10)
        self.assertTrue(r.meets_floor)
        self.assertTrue(r.regressed)
        self.assertFalse(r.ok)
        self.assertAlmostEqual(r.drift, -0.16, places=6)

    def test_a_drop_within_tolerance_is_not_a_regression(self):
        r = CaseResult(name="x", passes=93, trials=100, min_pass_rate=0.8,
                       baseline_rate=0.98, drift_tolerance=0.10)
        self.assertFalse(r.regressed)
        self.assertTrue(r.ok)

    def test_exactly_at_tolerance_is_not_a_regression(self):
        r = CaseResult(name="x", passes=88, trials=100, min_pass_rate=0.8,
                       baseline_rate=0.98, drift_tolerance=0.10)
        self.assertAlmostEqual(r.drift, -0.10, places=6)
        self.assertFalse(r.regressed)

    def test_improvement_is_never_a_regression(self):
        r = CaseResult(name="x", passes=100, trials=100, min_pass_rate=0.8,
                       baseline_rate=0.50, drift_tolerance=0.10)
        self.assertGreater(r.drift, 0)
        self.assertFalse(r.regressed)

    def test_suite_surfaces_regressions_in_the_report(self):
        baseline = Baseline({"x": 1.0})
        suite = Suite([Case("x", run=lambda: "wrong", check=is_ok, trials=10)], baseline)
        report = suite.run()
        self.assertEqual(len(report.regressions), 1)
        self.assertFalse(report.ok)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.path = os.path.join(self.dir, "nested", "baseline.json")

    def test_missing_baseline_file_loads_empty(self):
        self.assertEqual(Baseline.load(os.path.join(self.dir, "nope.json")).rates, {})

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions"
    )
    def test_an_unreadable_baseline_raises_instead_of_loading_empty(self):
        # An empty baseline turns drift detection off without a word. A real
        # unreadable directory, not a mock: os.path.exists() would say False.
        Suite([Case("a", run=lambda: "ok", check=is_ok, trials=2)]).run().save_baseline(self.path)
        parent = os.path.dirname(self.path)
        os.chmod(parent, 0)
        try:
            with self.assertRaises(OSError):
                Baseline.load(self.path)
        finally:
            os.chmod(parent, 0o700)

    def test_saved_baseline_round_trips(self):
        report = Suite([
            Case("a", run=lambda: "ok", check=is_ok, trials=4),
            Case("b", run=lambda: "wrong", check=is_ok, trials=4),
        ]).run()
        report.save_baseline(self.path)

        loaded = Baseline.load(self.path)
        self.assertEqual(loaded.get("a"), 1.0)
        self.assertEqual(loaded.get("b"), 0.0)
        self.assertIsNone(loaded.get("missing"))

    def test_saved_file_is_readable_json_with_a_timestamp(self):
        Suite([Case("a", run=lambda: "ok", check=is_ok, trials=2)]).run().save_baseline(self.path)
        with open(self.path) as fh:
            raw = json.load(fh)
        self.assertIn("recorded_at", raw)
        self.assertEqual(raw["cases"]["a"]["trials"], 2)


class ReportTests(unittest.TestCase):
    def test_mean_pass_rate_across_cases(self):
        report = Suite([
            Case("a", run=lambda: "ok", check=is_ok, trials=4),
            Case("b", run=lambda: "wrong", check=is_ok, trials=4),
        ]).run()
        self.assertAlmostEqual(report.mean_pass_rate, 0.5)

    def test_empty_report_has_zero_mean_and_is_ok(self):
        report = Report(results=[])
        self.assertEqual(report.mean_pass_rate, 0.0)
        self.assertTrue(report.ok)

    def test_summary_states_the_rate_and_the_verdict(self):
        report = Suite([Case("a", run=lambda: "ok", check=is_ok, trials=4)]).run()
        text = report.summary()
        self.assertIn("PASS", text)
        self.assertIn("4/4", text)
        self.assertIn("100%", text)

    def test_summary_flags_a_case_below_the_floor(self):
        report = Suite([Case("a", run=lambda: "wrong", check=is_ok, trials=4)]).run()
        self.assertIn("FAIL", report.summary())
        self.assertIn("below floor", report.summary())

    def test_summary_flags_a_regression(self):
        suite = Suite([Case("a", run=lambda: "wrong", check=is_ok, trials=4)],
                      Baseline({"a": 1.0}))
        self.assertIn("regressed", suite.run().summary())

    def test_long_output_is_truncated_in_failure_excerpts(self):
        long = "x" * 500
        report = Suite([Case("a", run=lambda: long, check=is_ok, trials=1)]).run()
        self.assertLessEqual(len(report.results[0].failures[0]), 120)



class BoundaryTests(unittest.TestCase):
    """Cases pinned to exactly-representable floats, so the boundary is real."""

    def test_min_pass_rate_of_zero_is_allowed(self):
        Case("x", run=lambda: "ok", check=is_ok, min_pass_rate=0.0)

    def test_min_pass_rate_of_one_is_allowed(self):
        Case("x", run=lambda: "ok", check=is_ok, min_pass_rate=1.0)

    def test_trials_of_one_is_allowed(self):
        Case("x", run=lambda: "ok", check=is_ok, trials=1)

    def test_drift_exactly_at_tolerance_is_not_a_regression(self):
        # 7/8 = 0.875 and 0.125 are both exact in binary floating point,
        # so drift is exactly -0.125 and the comparison is unambiguous.
        r = CaseResult(name="x", passes=7, trials=8, min_pass_rate=0.5,
                       baseline_rate=1.0, drift_tolerance=0.125)
        self.assertEqual(r.drift, -0.125)
        self.assertFalse(r.regressed)

    def test_drift_one_step_past_tolerance_is_a_regression(self):
        r = CaseResult(name="x", passes=6, trials=8, min_pass_rate=0.5,
                       baseline_rate=1.0, drift_tolerance=0.125)
        self.assertEqual(r.drift, -0.25)
        self.assertTrue(r.regressed)

    def test_summary_does_not_cry_regression_on_a_healthy_case(self):
        report = Suite([Case("a", run=lambda: "ok", check=is_ok, trials=4)]).run()
        line = report.results[0].summary()
        self.assertIn("PASS", line)
        self.assertNotIn("below floor", line)
        self.assertNotIn("regressed", line)

    def test_summary_omits_the_floor_note_when_the_floor_is_met(self):
        suite = Suite([Case("a", run=lambda: "ok", check=is_ok, trials=4)],
                      Baseline({"a": 1.0}))
        self.assertNotIn("below floor", suite.run().results[0].summary())

    def test_text_exactly_at_the_excerpt_limit_is_not_truncated(self):
        from failclosed.evals import _excerpt

        exact = "y" * 120
        self.assertEqual(_excerpt(exact), exact)
        self.assertNotIn("…", _excerpt(exact))

    def test_text_one_char_over_the_limit_is_truncated(self):
        from failclosed.evals import _excerpt

        over = "y" * 121
        self.assertEqual(len(_excerpt(over)), 120)
        self.assertTrue(_excerpt(over).endswith("…"))


class ImmutabilityTests(unittest.TestCase):
    def test_saving_a_baseline_twice_overwrites_cleanly(self):
        d = tempfile.mkdtemp(prefix="failclosed-test-")
        path = os.path.join(d, "nested", "baseline.json")
        report = Suite([Case("a", run=lambda: "ok", check=is_ok, trials=2)]).run()
        report.save_baseline(path)
        report.save_baseline(path)  # directory now exists; must not raise
        self.assertEqual(Baseline.load(path).get("a"), 1.0)


if __name__ == "__main__":
    unittest.main()
