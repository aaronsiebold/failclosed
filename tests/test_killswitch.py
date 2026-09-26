import os
import tempfile
import unittest

from failclosed import Gate, KillSwitch

# Root ignores directory permissions, so a chmod-000 directory is not a fault
# for it. Every other user gets the real error from the real filesystem.
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.path = os.path.join(self.dir, "state", "HALT")
        self.switch = KillSwitch(self.path)

    def test_absent_file_means_clear(self):
        self.assertFalse(self.switch.engaged)
        self.assertFalse(bool(self.switch))
        self.assertIsNone(self.switch.engaged_at)

    def test_engage_creates_the_file_and_records_the_reason(self):
        self.switch.engage("bad template shipped in 4f21a9c")
        self.assertTrue(self.switch.engaged)
        self.assertTrue(bool(self.switch))
        self.assertEqual(self.switch.reason, "bad template shipped in 4f21a9c")
        self.assertIsNotNone(self.switch.engaged_at)

    def test_engage_without_a_reason_still_engages(self):
        self.switch.engage()
        self.assertTrue(self.switch.engaged)
        self.assertEqual(self.switch.reason, "")

    def test_release_clears_it(self):
        self.switch.engage("temporary")
        self.switch.release()
        self.assertFalse(self.switch.engaged)

    def test_release_is_safe_when_never_engaged(self):
        self.switch.release()
        self.assertFalse(self.switch.engaged)

    def test_reason_is_empty_when_clear(self):
        self.assertEqual(self.switch.reason, "")

    def test_a_human_can_engage_it_with_touch(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        open(self.path, "w").close()
        self.assertTrue(self.switch.engaged)
        self.assertEqual(self.switch.reason, "")

    def test_repr_shows_state(self):
        self.assertIn("clear", repr(self.switch))
        self.switch.engage("x")
        self.assertIn("ENGAGED", repr(self.switch))

    def test_unreadable_reason_does_not_raise(self):
        # A directory where a file is expected: exists() is True, read fails.
        os.makedirs(self.path, exist_ok=True)
        self.assertTrue(self.switch.engaged)
        self.assertEqual(self.switch.reason, "")



class FailClosedTests(unittest.TestCase):
    """The branch that matters most: a switch we cannot read is engaged."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.switch = KillSwitch(os.path.join(self.dir, "HALT"))

    # No mocks here on purpose. os.path.exists() catches OSError itself and
    # answers False, so a mock that makes it raise tests a call that never
    # happens. A directory the process cannot search is the real fault.

    @unittest.skipIf(AS_ROOT, "root ignores directory permissions")
    def test_an_unreadable_switch_reports_engaged(self):
        self.switch.engage("halt")
        os.chmod(self.dir, 0)
        try:
            self.assertTrue(self.switch.engaged)
        finally:
            os.chmod(self.dir, 0o700)

    @unittest.skipIf(AS_ROOT, "root ignores directory permissions")
    def test_a_switch_that_cannot_be_checked_is_truthy(self):
        # No HALT file at all, but nothing can prove that: still engaged.
        os.chmod(self.dir, 0)
        try:
            self.assertTrue(bool(self.switch))
        finally:
            os.chmod(self.dir, 0o700)

    def test_a_dangling_symlink_is_present_so_engaged(self):
        try:
            os.symlink(os.path.join(self.dir, "missing-target"), self.switch.path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        self.assertTrue(self.switch.engaged)

    def test_a_path_the_os_rejects_is_engaged_and_raises_nothing(self):
        # os.lstat raises ValueError, not OSError, for an embedded NUL byte.
        # Nothing can prove such a switch clear, and `engaged` raises nothing.
        switch = KillSwitch(os.path.join(self.dir, "HA\0LT"))
        self.assertTrue(switch.engaged)
        self.assertTrue(bool(switch))
        self.assertIn("ENGAGED", repr(switch))

    def test_engaged_at_is_none_when_unreadable(self):
        from unittest import mock

        with mock.patch("os.path.getmtime", side_effect=OSError("gone")):
            self.assertIsNone(self.switch.engaged_at)

    def test_engaging_twice_does_not_raise(self):
        self.switch.engage("first")
        self.switch.engage("second")
        self.assertIn("second", self.switch.reason)

    def test_releasing_twice_does_not_raise(self):
        self.switch.engage("x")
        self.switch.release()
        self.switch.release()
        self.assertFalse(self.switch.engaged)


class PathTheOSRejectsTests(unittest.TestCase):
    """os.* raises ValueError, not OSError, for a path it cannot encode.

    A NUL byte (in str or bytes) and a lone surrogate both do it. `engaged`
    answers True for them, so every accessor a caller reads next must answer
    too, and a Gate in front of the switch must refuse rather than raise.
    """

    def setUp(self):
        d = tempfile.mkdtemp(prefix="failclosed-test-")
        self.paths = {
            "NUL in str": os.path.join(d, "HA\0LT"),
            "lone surrogate": os.path.join(d, "HA\ud800LT"),
            "NUL in bytes": os.path.join(os.fsencode(d), b"HA\0LT"),
        }

    def test_a_gate_in_front_of_it_refuses_and_raises_nothing(self):
        for label, path in self.paths.items():
            with self.subTest(label):
                d = Gate(lambda a: None, killswitch=KillSwitch(path)).evaluate("x")
                self.assertFalse(d.allowed)
                self.assertEqual(d.guard, "killswitch")
                self.assertEqual(d.reason, "kill switch engaged: no reason recorded")

    def test_reason_is_empty(self):
        for label, path in self.paths.items():
            with self.subTest(label):
                self.assertEqual(KillSwitch(path).reason, "")

    def test_engaged_at_is_none(self):
        for label, path in self.paths.items():
            with self.subTest(label):
                self.assertIsNone(KillSwitch(path).engaged_at)


class ReasonFormattingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.switch = KillSwitch(os.path.join(self.dir, "HALT"))

    def test_reason_is_a_single_line_without_the_timestamp(self):
        self.switch.engage("bad template shipped in 4f21a9c")
        reason = self.switch.reason
        self.assertEqual(reason, "bad template shipped in 4f21a9c")
        self.assertNotIn("\n", reason)
        self.assertNotIn("engaged", reason)

    def test_reason_is_empty_when_engaged_without_one(self):
        self.switch.engage()
        self.assertEqual(self.switch.reason, "")

    def test_reason_is_empty_for_a_switch_created_by_touch(self):
        open(self.switch.path, "w").close()
        self.assertEqual(self.switch.reason, "")


if __name__ == "__main__":
    unittest.main()
