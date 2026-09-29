"""The version is written in pyproject.toml and in failclosed/__init__.py.

Nothing ties the two together, so this test does. It also checks that the
newest release in CHANGELOG.md is the version being shipped.
"""

import os
import re
import unittest

import failclosed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class VersionTests(unittest.TestCase):
    def test_pyproject_and_the_package_agree(self):
        # No tomllib before Python 3.11. The [project] table ends at the next header.
        project = read("pyproject.toml").split("\n[project]\n", 1)[1].split("\n[", 1)[0]
        declared = re.findall(r'^version\s*=\s*"([^"]*)"', project, re.MULTILINE)
        self.assertEqual(declared, [failclosed.__version__])

    def test_the_changelog_leads_with_this_version(self):
        releases = re.findall(r"^## \[(\d[^\]]*)\]", read("CHANGELOG.md"), re.MULTILINE)
        self.assertTrue(releases, "CHANGELOG.md lists no releases")
        self.assertEqual(releases[0], failclosed.__version__)


if __name__ == "__main__":
    unittest.main()
