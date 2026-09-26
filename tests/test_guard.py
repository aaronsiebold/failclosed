import logging
import unittest

from failclosed import Decision, Gate, Refuse, Undetermined, guard


class Action:
    def __init__(self, key="alex@example.com"):
        self.key = key


def allows(action):
    """A guard that permits everything."""
    return None


@guard("always-refuses")
def refuses(action):
    raise Refuse("policy says no")


@guard("cannot-tell")
def undetermined(action):
    raise Undetermined("upstream unavailable")


@guard("explodes")
def explodes(action):
    raise RuntimeError("kaboom")


class DecisionTests(unittest.TestCase):
    def test_allow_is_truthy_and_refuse_is_falsy(self):
        self.assertTrue(bool(Decision(True, "fine")))
        self.assertFalse(bool(Decision(False, "nope")))

    def test_str_names_the_deciding_guard(self):
        self.assertEqual(str(Decision(False, "nope", guard="g")), "REFUSE [g]: nope")
        self.assertEqual(str(Decision(True, "fine")), "ALLOW: fine")


class GateTests(unittest.TestCase):
    def test_empty_gate_allows(self):
        self.assertTrue(Gate().evaluate(Action()))

    def test_allows_when_every_guard_passes(self):
        d = Gate(allows, allows).evaluate(Action())
        self.assertTrue(d.allowed)
        self.assertEqual(d.cleared, ("allows", "allows"))

    def test_refuse_denies_and_reports_reason_and_guard(self):
        d = Gate(allows, refuses).evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "policy says no")
        self.assertEqual(d.guard, "always-refuses")

    def test_undetermined_denies(self):
        d = Gate(undetermined).evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertIn("upstream unavailable", d.reason)
        self.assertEqual(d.guard, "cannot-tell")

    # --- the property the whole library exists for -----------------------

    def test_unexpected_exception_denies_rather_than_passes(self):
        with self.assertLogs("failclosed", level=logging.ERROR):
            d = Gate(explodes).evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertIn("RuntimeError", d.reason)
        self.assertIn("kaboom", d.reason)
        self.assertEqual(d.guard, "explodes")

    def test_evaluate_never_raises(self):
        with self.assertLogs("failclosed", level=logging.ERROR):
            self.assertIsInstance(Gate(explodes).evaluate(Action()), Decision)

    def test_short_circuits_on_first_refusal(self):
        ran = []
        gate = Gate(
            lambda a: ran.append("first"),
            refuses,
            lambda a: ran.append("third"),
        )
        self.assertFalse(gate.evaluate(Action()))
        self.assertEqual(ran, ["first"])

    def test_cleared_lists_only_guards_that_ran_and_passed(self):
        d = Gate(allows, refuses, allows).evaluate(Action())
        self.assertEqual(d.cleared, ("allows",))

    # --- kill switch ------------------------------------------------------

    def test_engaged_killswitch_denies_before_any_guard_runs(self):
        ran = []

        class Engaged:
            engaged = True
            reason = "bad template"

        gate = Gate(lambda a: ran.append("guard"), killswitch=Engaged())
        d = gate.evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertIn("bad template", d.reason)
        self.assertEqual(d.guard, "killswitch")
        self.assertEqual(ran, [])

    def test_clear_killswitch_allows(self):
        class Clear:
            engaged = False

        self.assertTrue(Gate(allows, killswitch=Clear()).evaluate(Action()))

    def test_unreadable_killswitch_denies(self):
        class Broken:
            @property
            def engaged(self):
                raise OSError("permission denied")

        d = Gate(allows, killswitch=Broken()).evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertIn("unreadable", d.reason)
        self.assertEqual(d.guard, "killswitch")

    def test_engaged_killswitch_whose_reason_raises_still_denies(self):
        # evaluate() never raises: a switch that is engaged but cannot say why
        # is still engaged.
        class Engaged:
            engaged = True

            @property
            def reason(self):
                raise RuntimeError("switch backend down")

        d = Gate(allows, killswitch=Engaged()).evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertEqual(d.guard, "killswitch")
        self.assertIn("reason unreadable (RuntimeError: switch backend down)", d.reason)

    def test_engaged_killswitch_without_reason_still_denies(self):
        class Engaged:
            engaged = True
            reason = ""

        d = Gate(killswitch=Engaged()).evaluate(Action())
        self.assertFalse(d.allowed)
        self.assertIn("no reason recorded", d.reason)

    # --- plumbing ---------------------------------------------------------

    def test_add_appends_guards_and_len_counts_them(self):
        gate = Gate(allows)
        self.assertEqual(len(gate), 1)
        gate.add(refuses, allows)
        self.assertEqual(len(gate), 3)
        self.assertFalse(gate.evaluate(Action()))

    def test_guard_names_come_from_the_decorator_then_the_function(self):
        self.assertEqual(Gate(refuses, allows).guards, ("always-refuses", "allows"))

    def test_evaluate_all_returns_one_decision_per_action(self):
        gate = Gate(lambda a: None if a.key == "ok" else _refuse())
        out = gate.evaluate_all([Action("ok"), Action("no"), Action("ok")])
        self.assertEqual([bool(d) for d in out], [True, False, True])


def _refuse():
    raise Refuse("not ok")



class ImmutabilityTests(unittest.TestCase):
    def test_a_decision_cannot_be_edited_after_the_fact(self):
        import dataclasses

        d = Decision(False, "refused")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.allowed = True


if __name__ == "__main__":
    unittest.main()
