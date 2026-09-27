import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

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

    def test_a_directory_at_the_path_is_engaged_and_never_read(self):
        os.makedirs(self.path, exist_ok=True)
        self.assertTrue(self.switch.engaged)
        self.assertEqual(self.switch.reason, "unusual file type (directory), not read")



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
        # The constructor refuses such a path, so it gets here only if
        # `path` is changed afterwards. Still nothing proves it clear.
        switch = KillSwitch(os.path.join(self.dir, "HALT"))
        switch.path = os.path.join(self.dir, "HA\0LT")
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


class UnusablePathTests(unittest.TestCase):
    """A path no file can ever have is refused when the switch is built.

    Empty, a NUL byte, or not UTF-8 text: lstat finds nothing there, so the
    switch would read clear, and engage() raises, so it could never be
    engaged. A kill switch that can never be engaged must not exist.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")

    def test_construction_refuses_a_path_no_switch_can_be_at(self):
        class EmptyPathLike:
            def __fspath__(self):
                return ""

        d = self.dir
        for label, path in {
            "empty str": "",
            "empty bytes": b"",
            "empty PathLike": EmptyPathLike(),
            "NUL in str": os.path.join(d, "HA\0LT"),
            "NUL in bytes": os.path.join(os.fsencode(d), b"HA\0LT"),
            "lone surrogate": os.path.join(d, "HA\ud800LT"),
            "invalid UTF-8 bytes": os.path.join(os.fsencode(d), b"HA\xffLT"),
            "surrogate-escaped str": os.path.join(d, "HA\udcffLT"),
        }.items():
            with self.subTest(label), self.assertRaisesRegex(ValueError, "can never be engaged"):
                KillSwitch(path)

    def test_construction_refuses_something_that_is_not_a_path(self):
        # 0 matters most: os.lstat(0) would stat file descriptor 0.
        for value in (None, 0, 1.5):
            with self.subTest(repr(value)), self.assertRaises(TypeError):
                KillSwitch(value)

    def test_bytes_and_pathlike_paths_are_kept_as_text(self):
        from pathlib import Path

        halt = os.path.join(self.dir, "HALT")
        self.assertEqual(KillSwitch(os.fsencode(halt)).path, halt)
        switch = KillSwitch(Path(halt))
        self.assertEqual(switch.path, halt)
        switch.engage("from a Path")
        self.assertTrue(switch.engaged)
        self.assertEqual(switch.reason, "from a Path")


class PathChangedLaterTests(unittest.TestCase):
    """`path` is a plain attribute, so it can still be set to a bad value.

    The constructor cannot see that, so every accessor checks the path with
    the constructor's rule first. A path no switch can be at reads engaged,
    the reason says why, and a Gate in front of it refuses and raises
    nothing. Before that check, an empty or non-UTF-8 path set here read
    clear (lstat found nothing) while engage() raised, and the Gate allowed.
    """

    def setUp(self):
        d = tempfile.mkdtemp(prefix="failclosed-test-")
        self.good = os.path.join(d, "HALT")
        b = os.fsencode(d)

        class EmptyPathLike:
            def __fspath__(self):
                return ""

        class BrokenPathLike:
            def __fspath__(self):
                raise RuntimeError("config backend down")

        # label -> (path, why the reason gives)
        self.paths = {
            "empty str": ("", "empty"),
            "empty bytes": (b"", "empty"),
            "empty PathLike": (EmptyPathLike(), "empty"),
            "NUL in str": (os.path.join(d, "HA\0LT"), "contains a NUL byte"),
            "NUL in bytes": (os.path.join(b, b"HA\0LT"), "contains a NUL byte"),
            "lone surrogate": (os.path.join(d, "HA\ud800LT"), "not UTF-8 text"),
            "surrogate-escaped str": (os.path.join(d, "HA\udcffLT"), "not UTF-8 text"),
            "invalid UTF-8 bytes": (os.path.join(b, b"HA\xffLT"), "not UTF-8 text"),
            "CESU-8 bytes": (os.path.join(b, b"HA\xed\xa0\x80\xed\xb0\x80LT"), "not UTF-8 text"),
            "overlong bytes": (os.path.join(b, b"HA\xc0\xafLT"), "not UTF-8 text"),
            "None": (None, "not a path"),
            "0, which lstat reads as fd 0": (0, "not a path"),
            "a float": (1.5, "not a path"),
            "a PathLike that raises": (BrokenPathLike(), "not a path"),
        }

    def switch(self, path):
        s = KillSwitch(self.good)
        s.path = path
        return s

    def test_it_reads_engaged(self):
        for label, (path, _) in self.paths.items():
            with self.subTest(label):
                s = self.switch(path)
                self.assertIs(s.engaged, True)
                self.assertIn("ENGAGED", repr(s))

    def test_a_gate_in_front_of_it_refuses_and_says_why(self):
        for label, (path, why) in self.paths.items():
            with self.subTest(label):
                d = Gate(lambda a: None, killswitch=self.switch(path)).evaluate("x")
                self.assertFalse(d.allowed)
                self.assertEqual(d.guard, "killswitch")
                self.assertEqual(d.reason, f"kill switch engaged: unusable path ({why})")

    def test_reason_says_why(self):
        for label, (path, why) in self.paths.items():
            with self.subTest(label):
                self.assertEqual(self.switch(path).reason, f"unusable path ({why})")

    def test_engaged_at_is_none(self):
        for label, (path, _) in self.paths.items():
            with self.subTest(label):
                self.assertIsNone(self.switch(path).engaged_at)

    def test_a_good_path_set_later_works_like_one_given_at_construction(self):
        s = self.switch(os.fsencode(self.good))
        self.assertFalse(s.engaged)
        KillSwitch(self.good).engage("set later")
        self.assertTrue(s.engaged)
        self.assertEqual(s.reason, "set later")


class AbsentSwitchTests(unittest.TestCase):
    """Absent reads clear only when the absence is proven.

    A missing directory proves it: nobody can have created a file inside a
    directory that does not exist (`touch` would fail, and engage() makes
    the directory). A directory that is a symlink to nothing proves
    nothing: that is a disk that went away, with the switch possibly on it.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")

    def link(self, name, target):
        path = os.path.join(self.dir, name)
        try:
            os.symlink(os.path.join(self.dir, target), path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        return path

    def test_a_missing_chain_of_directories_reads_clear(self):
        switch = KillSwitch(os.path.join(self.dir, "a", "b", "c", "HALT"))
        self.assertFalse(switch.engaged)
        self.assertTrue(Gate(killswitch=switch).evaluate("x"))

    def test_a_relative_path_in_the_current_directory_reads_clear(self):
        here = os.getcwd()
        os.chdir(self.dir)
        try:
            switch = KillSwitch("HALT")
            self.assertFalse(switch.engaged)
            open("HALT", "w").close()
            self.assertTrue(switch.engaged)
        finally:
            os.chdir(here)

    def test_a_dangling_symlink_parent_reads_engaged(self):
        state = self.link("state", "unmounted-volume")
        switch = KillSwitch(os.path.join(state, "HALT"))
        self.assertTrue(switch.engaged)
        d = Gate(lambda a: None, killswitch=switch).evaluate("x")
        self.assertFalse(d.allowed)
        self.assertEqual(d.guard, "killswitch")

    def test_a_dangling_symlink_further_up_reads_engaged(self):
        state = self.link("state", "unmounted-volume")
        self.assertTrue(KillSwitch(os.path.join(state, "sub", "HALT")).engaged)

    def test_a_symlinked_parent_that_resolves_reads_clear(self):
        os.mkdir(os.path.join(self.dir, "real"))
        state = self.link("state", "real")
        switch = KillSwitch(os.path.join(state, "HALT"))
        self.assertFalse(switch.engaged)
        switch.engage("through the link")
        self.assertTrue(switch.engaged)

    def test_when_no_ancestor_can_be_seen_nothing_is_proven(self):
        # Not even / or . answers: the walk runs out, and that proves nothing.
        with mock.patch("os.lstat", side_effect=FileNotFoundError):
            self.assertTrue(KillSwitch(os.path.join(self.dir, "HALT")).engaged)
            self.assertTrue(KillSwitch("HALT").engaged)

    def test_a_file_where_the_parent_should_be_reads_engaged(self):
        open(os.path.join(self.dir, "state"), "w").close()
        self.assertTrue(KillSwitch(os.path.join(self.dir, "state", "HALT")).engaged)

    def test_a_symlink_loop_as_the_parent_reads_engaged(self):
        loop = self.link("loop", "loop")
        self.assertTrue(KillSwitch(os.path.join(loop, "HALT")).engaged)


def finishes(fn, seconds=30.0):
    """Run fn in a daemon thread: [its result], or [] if it never came back.

    A blocked open() cannot be interrupted, so the thread is the only way to
    turn "hangs forever" into a failing assertion instead of a stuck suite.
    """
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    return box


@unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
class NeverBlocksTests(unittest.TestCase):
    """Whatever sits at the switch path, the answer comes back.

    A FIFO blocks open() until a writer shows up, and a device like /dev/zero
    never runs out of bytes, so reading either one would hang the Gate in
    front of it. The type decides first; only a regular file is ever read.
    """

    FIFO = "unusual file type (FIFO), not read"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.fifo = os.path.join(self.dir, "HALT")
        os.mkfifo(self.fifo)

    def tearDown(self):
        # If a reader is stuck in open(), a writer arriving releases it.
        try:
            os.close(os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    def test_a_fifo_is_engaged_and_its_reason_comes_back_unread(self):
        switch = KillSwitch(self.fifo)
        self.assertTrue(switch.engaged)
        self.assertEqual(finishes(lambda: switch.reason), [self.FIFO])

    def test_a_gate_in_front_of_a_fifo_returns_a_refusal(self):
        gate = Gate(lambda a: None, killswitch=KillSwitch(self.fifo))
        out = finishes(lambda: gate.evaluate("x"))
        self.assertEqual(len(out), 1, "Gate.evaluate never returned")
        self.assertFalse(out[0].allowed)
        self.assertEqual(out[0].guard, "killswitch")
        self.assertEqual(out[0].reason, "kill switch engaged: " + self.FIFO)

    def test_a_symlink_to_a_fifo_is_judged_by_what_it_points_at(self):
        link = os.path.join(self.dir, "LINK")
        os.symlink(self.fifo, link)
        switch = KillSwitch(link)
        self.assertTrue(switch.engaged)
        self.assertEqual(finishes(lambda: switch.reason), [self.FIFO])

    def test_a_fifo_swapped_in_after_the_type_check_is_still_not_read(self):
        # Between the type check and the open, a regular file can become a
        # FIFO. Make stat lie to stage that race: the open must not block,
        # and the type seen on the open descriptor decides.
        real_stat = os.stat

        def says_regular(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path == self.fifo:
                return os.stat_result((stat.S_IFREG | 0o644,) + tuple(st)[1:])
            return st

        switch = KillSwitch(self.fifo)
        with mock.patch("os.stat", says_regular):
            self.assertEqual(finishes(lambda: switch.reason), [self.FIFO])

    def test_a_socket_is_named_by_type_without_being_opened(self):
        import socket

        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no UNIX sockets here")
        path = os.path.join(self.dir, "SOCK")
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(path)
        switch = KillSwitch(path)
        self.assertTrue(switch.engaged)
        self.assertEqual(switch.reason, "unusual file type (socket), not read")

    def test_a_device_is_engaged_and_never_read(self):
        if not stat.S_ISCHR(os.stat(os.devnull).st_mode):
            self.skipTest("os.devnull is not a character device here")
        switch = KillSwitch(os.devnull)
        self.assertTrue(switch.engaged)
        self.assertEqual(
            finishes(lambda: switch.reason),
            ["unusual file type (character device), not read"],
        )


class BoundedReadTests(unittest.TestCase):
    """The reason goes into single-line logs. A huge file must not."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="failclosed-test-")
        self.path = os.path.join(self.dir, "HALT")

    def write(self, data):
        with open(self.path, "wb") as fh:
            fh.write(data)

    def test_reason_is_at_most_the_first_kib(self):
        self.write(b"x" * 100_000)  # no newline anywhere
        self.assertEqual(KillSwitch(self.path).reason, "x" * 1024)

    def test_a_character_cut_at_the_limit_does_not_lose_the_reason(self):
        # 1 + 2*1000 bytes: the 1024-byte cut splits the 512th "é".
        self.write(("a" + "é" * 1000).encode("utf-8"))
        reason = KillSwitch(self.path).reason
        self.assertTrue(reason.startswith("aé"), reason[:10])
        self.assertEqual(reason, "a" + "é" * 511 + "\ufffd")


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
