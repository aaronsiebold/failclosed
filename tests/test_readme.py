"""The README's numbers are claims, so this test checks them against the repo.

Each one is derived here, never typed by hand:

* every "N tests" is the size of the suite unittest discovers, and the demo
  names real tests and skips as many as it says;
* the mutation badge and the sample run's header, score and allow-listed
  block come from the mutation sites in failclosed/ and .mutants-allow;
* every failclosed/<file>.py:<line> label is a real mutation site, at that
  line, making that change, and the line printed under it is what the runner
  prints there.

The score is exact when CI's mutants job is green: that job fails on any
survivor not in .mutants-allow, so killed = sites - allow-listed sites.

What this cannot check without running the suite under a mutant: that the
demo's tests are the only ones that kill its survivor. That was checked by
running the demo when it was written.

Inside a mutant run it stands down. The source is wrong on purpose there, so a
README that disagrees with it proves nothing about the tests, and counting that
failure as a kill would inflate the score: the first run with this file in
place reported 56/56, the two equivalent mutants and the demo's survivor all
"killed" by a text comparison.
"""

import os
import re
import unittest

from failclosed.mutate import MUTANT_ENV, Survivor, _python_files, load_allowed, sites

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
WORDS = {w: i for i, w in enumerate((
    "zero", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve"))}


def read_readme():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        return fh.read()


def test_names(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from test_names(t)
        else:
            yield t.id().rsplit(".", 1)[-1]


def mutation_facts(root=ROOT):
    """(sites in run order, number of files, allow-listed sites, key of each site)."""
    files = _python_files(root, "failclosed")
    muts = [m for f in files for m in sites(f, relative_to=root)]
    lines = {}
    for m in muts:
        if m.path not in lines:
            with open(os.path.join(root, m.path), encoding="utf-8") as fh:
                lines[m.path] = fh.read().split("\n")
    key = {m: Survivor(m, lines[m.path][m.line - 1]).key for m in muts}
    return muts, len(files), lines, key


class ReadmeClaimsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get(MUTANT_ENV) == "1":
            raise unittest.SkipTest("inside a mutant run: the source is wrong on purpose")
        cls.readme = read_readme()
        suite = unittest.defaultTestLoader.discover(TESTS, top_level_dir=TESTS)
        cls.total = suite.countTestCases()
        cls.names = list(test_names(suite))
        cls.muts, cls.nfiles, cls.lines, cls.key = mutation_facts()
        cls.allowed = load_allowed(ROOT)
        cls.excused = [m for m in cls.muts if cls.key[m] in cls.allowed]

    # ------------------------------------------------------------ test counts

    def demo(self):
        start = self.readme.index("### Does it actually catch anything?")
        end = self.readme.index("\n### ", start + 1)
        return self.readme[start:end]

    def test_every_suite_size_matches_discovery(self):
        claims = re.findall(r"(\d+) tests\b", self.readme)
        self.assertGreaterEqual(len(claims), 3, "the README's test counts went missing")
        for n in claims:
            self.assertEqual(int(n), self.total, f"README says {n} tests")

    def test_the_demo_skips_real_tests_and_says_how_many(self):
        demo = self.demo()
        skipped = re.findall(r"`(test_\w+)`", demo)
        self.assertTrue(skipped, "the demo names no tests to skip")
        for name in skipped:
            self.assertEqual(self.names.count(name), 1, f"{name}: want exactly one such test")
        word = re.search(r"Skip the (\w+) tests", demo).group(1)
        self.assertEqual(WORDS[word], len(skipped), f"'{word}' tests, but {len(skipped)} named")
        shown = int(re.search(r"OK \(skipped=(\d+)\)", demo).group(1))
        self.assertEqual(shown, len(skipped))

    # -------------------------------------------------------- mutation score

    def test_the_allow_list_names_only_live_sites(self):
        live = set(self.key.values())
        for k in self.allowed:
            self.assertIn(k, live, "allow-list entry matches no mutation site")

    def test_badge_header_and_score(self):
        n, a = len(self.muts), len(self.excused)
        k = n - a
        badge = re.search(r"mutants%20killed-(\d+)%2F(\d+)", self.readme)
        self.assertEqual((int(badge.group(1)), int(badge.group(2))), (k, n))
        header = re.search(r"(\d+) mutation site\(s\) across (\d+) file\(s\)", self.readme)
        self.assertEqual((int(header.group(1)), int(header.group(2))), (n, self.nfiles))
        score = re.search(r"(\d+)/(\d+) mutants killed  \((\d+\.\d)%\)", self.readme)
        self.assertEqual(score.groups(), (str(k), str(n), f"{k / n * 100:.1f}"))
        known = re.search(r"(\d+) known equivalent mutant\(s\), allow-listed", self.readme)
        self.assertEqual(int(known.group(1)), a)

    def test_sample_lines_match_the_run_order(self):
        rows = re.findall(r"^\s*\[\s*(\d+)/(\d+)\] (killed|SURVIVED)\s+(.+)$",
                          self.readme, re.MULTILINE)
        self.assertTrue(rows, "no sample run lines found")
        for i, n, verdict, label in rows:
            m = self.muts[int(i) - 1]
            self.assertEqual(int(n), len(self.muts))
            self.assertEqual(label, m.label, f"line [{i}/{n}]")
            self.assertEqual(verdict == "SURVIVED", m in self.excused, label)

    # ---------------------------------------------------------- file:line refs

    def test_every_cited_site_is_real_and_prints_what_the_runner_prints(self):
        lines = self.readme.split("\n")
        cited = 0
        for i, text in enumerate(lines):
            for ref in re.finditer(r"(failclosed/[\w/]+\.py):(\d+) ", text):
                cited += 1
                here = [m for m in self.muts
                        if m.path == ref.group(1) and m.line == int(ref.group(2))]
                match = [m for m in here if text[ref.start():].startswith(m.label)]
                self.assertTrue(match, f"README line {i + 1}: no mutation site {ref.group(0)!r}")
                if text.strip() != match[0].label:
                    continue  # a sample run line; its order is checked above
                # A label alone on its line is followed by what the runner
                # prints under it: the allow-list reason, or the source line.
                m = match[0]
                want = (self.allowed[self.key[m]] if m in self.excused
                        else self.lines[m.path][m.line - 1].strip())
                self.assertEqual(lines[i + 1].strip(), want, f"README line {i + 2}")
        self.assertGreater(cited, 0, "the README cites no mutation sites")


class StandsDownTests(unittest.TestCase):
    def test_it_stands_down_inside_a_mutant_run(self):
        before = os.environ.get(MUTANT_ENV)
        os.environ[MUTANT_ENV] = "1"
        try:
            with self.assertRaises(unittest.SkipTest):
                ReadmeClaimsTests.setUpClass()
        finally:
            if before is None:
                del os.environ[MUTANT_ENV]
            else:
                os.environ[MUTANT_ENV] = before


if __name__ == "__main__":
    unittest.main()
