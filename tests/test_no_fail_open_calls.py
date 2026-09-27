"""No module in failclosed/ may ask the filesystem a question that cannot fail.

os.path.exists, lexists, isfile and isdir catch OSError themselves and answer
False, so "I could not look" comes back as "it is not there". That is the
fail-open shape this library exists to remove. The modules here call lstat,
stat or open and handle FileNotFoundError themselves instead.
"""

import ast
import os
import sys
import unittest

import failclosed
import failclosed.mutate  # noqa: F401 - the package does not import it itself

SWALLOWERS = {"exists", "lexists", "isfile", "isdir"}
PKG = os.path.dirname(os.path.abspath(failclosed.__file__))


def offenses(source, filename="<source>"):
    """(line, name) for every use of a swallowing predicate in `source`.

    Any attribute or import by these names counts, whatever it is reached
    through (os.path, an alias, pathlib), so comments and docstrings are
    the only places the words may appear.
    """
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Attribute) and node.attr in SWALLOWERS:
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.ImportFrom):
            found.extend((node.lineno, a.name) for a in node.names if a.name in SWALLOWERS)
    return found


def package_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        out.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".py"))
    return sorted(out)


class NoFailOpenCallsTests(unittest.TestCase):
    def test_no_module_calls_a_predicate_that_swallows_oserror(self):
        bad = []
        for path in package_files():
            with open(path, encoding="utf-8") as fh:
                for line, name in offenses(fh.read(), path):
                    bad.append(f"{os.path.relpath(path, PKG)}:{line} uses {name}")
        self.assertEqual(bad, [], "call lstat/stat/open and handle FileNotFoundError")

    def test_the_scan_covers_every_loaded_module(self):
        # A scan of the wrong directory finds nothing and passes. Every
        # failclosed module this process has imported must be in the scan.
        loaded = {
            os.path.abspath(m.__file__)
            for name, m in list(sys.modules.items())
            if (name == "failclosed" or name.startswith("failclosed."))
            and getattr(m, "__file__", None)
        }
        self.assertGreaterEqual(len(loaded), 2)
        self.assertLessEqual(loaded, set(package_files()))

    def test_the_scanner_catches_each_spelling(self):
        for source in (
            "import os\nos.path.exists(p)\n",
            "import os\nos.path.lexists(p)\n",
            "import os.path as osp\nosp.isfile(p)\n",
            "from os.path import isdir\n",
            "from os import path\npath.isdir(p)\n",
            "from pathlib import Path\nPath(p).exists()\n",
        ):
            with self.subTest(source):
                self.assertTrue(offenses(source))

    def test_the_scanner_ignores_comments_docstrings_and_strings(self):
        source = (
            '"""Not os.path.exists(): it swallows OSError."""\n'
            "# os.path.isfile(p)\n"
            'x = "os.path.isdir"\n'
        )
        self.assertEqual(offenses(source), [])


if __name__ == "__main__":
    unittest.main()
