import os
import tempfile
import unittest

from failclosed import Gate, Ledger, Refuse, StaleLedger, guard


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FreshnessTests(unittest.TestCase):
    def test_never_synced_ledger_refuses_to_answer(self):
        ledger = Ledger(max_staleness=60)
        self.assertIsNone(ledger.age)
        self.assertFalse(ledger.is_fresh)
        with self.assertRaises(StaleLedger) as ctx:
            ledger.contains("anyone")
        self.assertIn("never been synced", str(ctx.exception))

    def test_fresh_ledger_answers(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync(["a@example.com"])
        self.assertTrue(ledger.is_fresh)
        self.assertTrue(ledger.contains("a@example.com"))
        self.assertFalse(ledger.contains("b@example.com"))

    def test_ledger_goes_stale_and_stops_answering(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync(["a@example.com"])

        clock.advance(59)
        self.assertTrue(ledger.is_fresh)
        self.assertFalse(ledger.contains("b@example.com"))

        clock.advance(2)  # now 61s, past the limit
        self.assertFalse(ledger.is_fresh)
        with self.assertRaises(StaleLedger):
            ledger.contains("b@example.com")

    def test_boundary_exactly_at_limit_is_still_fresh(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync([])
        clock.advance(60)
        self.assertEqual(ledger.age, 60)
        self.assertTrue(ledger.is_fresh)
        ledger.require_fresh()  # must not raise

    def test_one_second_past_the_limit_is_stale(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync([])
        clock.advance(61)
        self.assertFalse(ledger.is_fresh)
        with self.assertRaises(StaleLedger):
            ledger.require_fresh()

    def test_resync_restores_freshness(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync([])
        clock.advance(120)
        self.assertFalse(ledger.is_fresh)
        ledger.sync([])
        self.assertTrue(ledger.is_fresh)


class ContentTests(unittest.TestCase):
    def test_sync_reports_keys_the_source_knew_and_we_did_not(self):
        ledger = Ledger(max_staleness=60, clock=FakeClock())
        self.assertEqual(ledger.sync(["a", "b"]), 2)
        self.assertEqual(ledger.sync(["a", "b"]), 0)
        self.assertEqual(ledger.sync(["a", "b", "c"]), 1)

    def test_record_marks_a_key_as_acted_on(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync([])
        self.assertFalse(ledger.contains("a"))
        ledger.record("a", campaign="q3")
        self.assertTrue(ledger.contains("a"))
        self.assertEqual(ledger.get("a").meta["campaign"], "q3")

    def test_len_and_iteration(self):
        ledger = Ledger(max_staleness=60, clock=FakeClock())
        ledger.sync(["a", "b"])
        self.assertEqual(len(ledger), 2)
        self.assertEqual({e.key for e in ledger}, {"a", "b"})

    def test_get_returns_none_for_unknown_key(self):
        self.assertIsNone(Ledger().get("nobody"))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.path = os.path.join(self.dir, "nested", "ledger.jsonl")

    def test_entries_survive_a_reload(self):
        clock = FakeClock()
        first = Ledger(self.path, max_staleness=60, clock=clock)
        first.sync(["a@example.com"])
        first.record("b@example.com", note="manual")

        second = Ledger(self.path, max_staleness=60, clock=clock)
        second.sync([])  # reloaded from disk, then freshened
        self.assertTrue(second.contains("a@example.com"))
        self.assertTrue(second.contains("b@example.com"))
        self.assertEqual(second.get("b@example.com").meta["note"], "manual")

    def test_a_reloaded_ledger_is_still_stale_until_synced(self):
        clock = FakeClock()
        Ledger(self.path, max_staleness=60, clock=clock).sync(["a"])
        reloaded = Ledger(self.path, max_staleness=60, clock=clock)
        # It has the data, but it has not verified it against the source.
        self.assertEqual(len(reloaded), 1)
        with self.assertRaises(StaleLedger):
            reloaded.contains("a")

    def test_temporary_ledger_is_writable(self):
        ledger = Ledger.temporary(max_staleness=60)
        ledger.sync(["a"])
        ledger.record("b")
        self.assertTrue(os.path.exists(ledger.path))


# Root ignores directory permissions, so a chmod-000 directory is not a fault
# for it. Every other user gets the real error from the real filesystem.
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class UnreadableLedgerTests(unittest.TestCase):
    """A ledger file that exists but cannot be read must not load as empty.

    An empty ledger plus one sync of an empty provider page reads as fresh and
    says nobody was ever contacted: a duplicate send that every guard allows.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.path = os.path.join(self.dir, "ledger.jsonl")
        Ledger(self.path).record("alice@example.com")

    @unittest.skipIf(AS_ROOT, "root ignores directory permissions")
    def test_an_unreadable_ledger_refuses_to_load(self):
        os.chmod(self.dir, 0)
        try:
            with self.assertRaises(OSError):
                Ledger(self.path)
        finally:
            os.chmod(self.dir, 0o700)

    def test_a_missing_ledger_file_still_starts_empty(self):
        self.assertEqual(len(Ledger(os.path.join(self.dir, "absent", "l.jsonl"))), 0)
        self.assertEqual(len(Ledger(self.path)), 1)


class IntegrationTests(unittest.TestCase):
    def test_stale_ledger_makes_the_gate_refuse(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync([])

        @guard("not-already-sent")
        def not_already_sent(action):
            if ledger.contains(action["key"]):
                raise Refuse("already sent")

        gate = Gate(not_already_sent)
        self.assertTrue(gate.evaluate({"key": "new@example.com"}))

        clock.advance(3600)
        decision = gate.evaluate({"key": "new@example.com"})
        self.assertFalse(decision.allowed)
        self.assertIn("undetermined", decision.reason)
        self.assertEqual(decision.guard, "not-already-sent")

    def test_known_key_is_refused_while_fresh(self):
        clock = FakeClock()
        ledger = Ledger(max_staleness=60, clock=clock)
        ledger.sync(["seen@example.com"])

        @guard("not-already-sent")
        def not_already_sent(action):
            if ledger.contains(action["key"]):
                raise Refuse("already sent")

        d = Gate(not_already_sent).evaluate({"key": "seen@example.com"})
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "already sent")



class EntryImmutabilityTests(unittest.TestCase):
    def test_an_entry_cannot_be_edited_after_the_fact(self):
        import dataclasses

        from failclosed import Entry

        e = Entry(key="a", at=1.0, meta={})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.key = "b"

    def test_recording_twice_into_a_nested_path_does_not_raise(self):
        d = tempfile.mkdtemp(prefix="failclosed-test-")
        ledger = Ledger(os.path.join(d, "deep", "nest", "ledger.jsonl"),
                        max_staleness=60, clock=FakeClock())
        ledger.record("a")
        ledger.record("b")  # directory now exists; must not raise
        self.assertEqual(len(ledger), 2)


if __name__ == "__main__":
    unittest.main()
