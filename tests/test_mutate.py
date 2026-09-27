"""Tests for the mutation runner.

The runner is the thing that decides whether every other test in this repo is
worth anything, so leaving it untested would be the joke telling itself. It is
excluded from *being* mutated (mutating the mutator while it runs is a mess
worth avoiding), which makes covering it here the only check it gets.
"""

import os
import tempfile
import textwrap
import unittest

from failclosed.mutate import (
    ALLOW_FILE,
    Mutation,
    Survivor,
    _apply,
    _child_env,
    _python_files,
    load_allowed,
    sites,
)


def write(tmp, name, source):
    path = os.path.join(tmp, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(source).lstrip())
    return path


class SiteDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="failclosed-mut-")

    def found(self, source):
        path = write(self.tmp, "sample.py", source)
        return sites(path, relative_to=self.tmp)

    def test_finds_comparison_operators(self):
        got = self.found("x = a >= b\n")
        self.assertEqual([(m.original, m.replacement) for m in got], [(">=", ">")])

    def test_finds_boolean_operators_and_literals(self):
        got = self.found("x = a and True\n")
        self.assertEqual(
            [(m.original, m.replacement) for m in got],
            [("and", "or"), ("True", "False")],
        )

    def test_finds_not_and_marks_it_for_deletion(self):
        got = self.found("x = not flag\n")
        self.assertEqual(got[0].original, "not ")
        self.assertEqual(got[0].replacement, "")

    def test_reports_one_site_per_occurrence_on_a_line(self):
        got = self.found("if 0.0 <= v <= 1.0:\n    pass\n")
        self.assertEqual(len(got), 2)
        self.assertNotEqual(got[0].col, got[1].col)

    # --- the central claim of using tokenize rather than a regex -----------

    def test_never_mutates_a_comment(self):
        self.assertEqual(self.found("x = 1  # a >= b and True\n"), [])

    def test_never_mutates_a_docstring(self):
        got = self.found('''
            def f():
                """Returns True when a >= b and c is not None."""
                return 1
        ''')
        self.assertEqual(got, [])

    def test_never_mutates_a_string_literal(self):
        self.assertEqual(self.found('msg = "a >= b and True"\n'), [])

    def test_mutates_code_on_a_line_that_also_has_a_comment(self):
        got = self.found("x = a >= b  # not True\n")
        self.assertEqual([(m.original, m.replacement) for m in got], [(">=", ">")])

    def test_unparseable_file_yields_no_sites_instead_of_raising(self):
        path = write(self.tmp, "broken.py", "def (:\n")
        self.assertEqual(sites(path, relative_to=self.tmp), [])

    def test_paths_are_relative_to_the_given_root(self):
        path = write(self.tmp, "pkg/mod.py", "x = a == b\n")
        self.assertEqual(sites(path, relative_to=self.tmp)[0].path, "pkg/mod.py")


class ApplyTests(unittest.TestCase):
    def test_replaces_exactly_the_token_span(self):
        lines = ["if a >= b:\n"]
        m = Mutation("f.py", 1, 5, 7, ">=", ">")
        self.assertEqual(_apply(lines, m), ["if a > b:\n"])

    def test_deletion_removes_the_token_and_its_space(self):
        lines = ["x = not flag\n"]
        m = Mutation("f.py", 1, 4, 8, "not ", "")
        self.assertEqual(_apply(lines, m), ["x = flag\n"])

    def test_leaves_other_lines_untouched(self):
        lines = ["a = 1\n", "if a >= 2:\n", "b = 3\n"]
        out = _apply(lines, Mutation("f.py", 2, 5, 7, ">=", ">"))
        self.assertEqual(out[0], "a = 1\n")
        self.assertEqual(out[2], "b = 3\n")

    def test_does_not_mutate_the_caller_s_list(self):
        lines = ["if a >= b:\n"]
        _apply(lines, Mutation("f.py", 1, 5, 7, ">=", ">"))
        self.assertEqual(lines, ["if a >= b:\n"])


class SurvivorKeyTests(unittest.TestCase):
    def key_for(self, line_no, source_line):
        m = Mutation("f.py", line_no, 0, 2, ">=", ">")
        return Survivor(m, source_line).key

    def test_key_ignores_the_line_number(self):
        # An allow-list keyed on line numbers starts excusing a different
        # mutant as soon as someone adds an import.
        self.assertEqual(self.key_for(10, "    if a >= b:"),
                         self.key_for(99, "    if a >= b:"))

    def test_key_ignores_surrounding_whitespace(self):
        self.assertEqual(self.key_for(1, "  if a >= b:"),
                         self.key_for(1, "        if a >= b:"))

    def test_key_distinguishes_different_source_lines(self):
        self.assertNotEqual(self.key_for(1, "if a >= b:"),
                            self.key_for(1, "if c >= d:"))

    def test_key_distinguishes_different_mutations_on_one_line(self):
        line = "if a >= b:"
        a = Survivor(Mutation("f.py", 1, 0, 2, ">=", ">"), line).key
        b = Survivor(Mutation("f.py", 1, 0, 2, ">=", "<"), line).key
        self.assertNotEqual(a, b)

    def test_key_distinguishes_the_same_line_in_different_files(self):
        line = "if a >= b:"
        a = Survivor(Mutation("one.py", 1, 0, 2, ">=", ">"), line).key
        b = Survivor(Mutation("two.py", 1, 0, 2, ">=", ">"), line).key
        self.assertNotEqual(a, b)


class AllowListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="failclosed-allow-")

    def write_allow(self, body):
        with open(os.path.join(self.tmp, ALLOW_FILE), "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(body).lstrip())

    def test_missing_file_allows_nothing(self):
        self.assertEqual(load_allowed(self.tmp), {})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads anything")
    def test_an_unreadable_file_raises_instead_of_allowing_nothing(self):
        # Allowing nothing would still fail the build, but it would blame
        # every allow-listed mutant instead of naming the real fault.
        self.write_allow("a.py :: x = True :: True->False  # reason\n")
        path = os.path.join(self.tmp, ALLOW_FILE)
        os.chmod(path, 0)
        try:
            with self.assertRaises(PermissionError):
                load_allowed(self.tmp)
        finally:
            os.chmod(path, 0o600)

    def test_parses_an_entry_and_its_reason(self):
        self.write_allow("a.py :: x = True :: True->False  # byte order only\n")
        allowed = load_allowed(self.tmp)
        self.assertEqual(allowed["a.py :: x = True :: True->False"], "byte order only")

    def test_skips_comments_and_blank_lines(self):
        self.write_allow("""
            # a header comment

               # an indented comment
            a.py :: x = True :: True->False  # reason
        """)
        self.assertEqual(len(load_allowed(self.tmp)), 1)

    def test_an_entry_without_a_reason_is_recorded_as_such(self):
        self.write_allow("a.py :: x = True :: True->False\n")
        self.assertEqual(list(load_allowed(self.tmp).values()), ["no reason given"])

    def test_the_real_allow_list_covers_only_documented_entries(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowed = load_allowed(root)
        self.assertTrue(allowed, "the repo's allow-list should not be empty")
        for key, why in allowed.items():
            self.assertNotEqual(why, "no reason given", f"{key} has no stated reason")
            self.assertGreater(len(why), 20, f"{key}: reason is too thin to review")


class EnvironmentTests(unittest.TestCase):
    def test_bytecode_writing_is_disabled_for_mutant_runs(self):
        # Load-bearing, not tidiness: a cached .pyc whose source matches on
        # mtime and size is reused, so a mutant can run stale bytecode and be
        # reported as a survivor it never was.
        self.assertEqual(_child_env()["PYTHONDONTWRITEBYTECODE"], "1")

    def test_the_rest_of_the_environment_is_inherited(self):
        os.environ["FAILCLOSED_TEST_MARKER"] = "present"
        try:
            self.assertEqual(_child_env()["FAILCLOSED_TEST_MARKER"], "present")
        finally:
            del os.environ["FAILCLOSED_TEST_MARKER"]


class FileDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="failclosed-files-")
        write(self.tmp, "pkg/__init__.py", "")
        write(self.tmp, "pkg/a.py", "x = 1\n")
        write(self.tmp, "pkg/mutate.py", "x = 1\n")
        write(self.tmp, "pkg/__pycache__/a.py", "x = 1\n")
        write(self.tmp, "pkg/notes.txt", "hello\n")

    def test_collects_python_files_only(self):
        found = [os.path.basename(f) for f in _python_files(self.tmp, "pkg")]
        self.assertIn("a.py", found)
        self.assertNotIn("notes.txt", found)

    def test_excludes_the_runner_itself(self):
        found = [os.path.basename(f) for f in _python_files(self.tmp, "pkg")]
        self.assertNotIn("mutate.py", found)

    def test_excludes_pycache(self):
        self.assertFalse(any("__pycache__" in f for f in _python_files(self.tmp, "pkg")))


if __name__ == "__main__":
    unittest.main()
