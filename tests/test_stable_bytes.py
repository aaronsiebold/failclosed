"""On-disk JSON has one canonical byte order, so a committed baseline or ledger
diffs cleanly. These pin `sort_keys=True`, which no round-trip test can see."""
import os
import tempfile
import unittest

from failclosed import Entry
from failclosed.evals import Baseline, CaseResult


class StableBytesTests(unittest.TestCase):
    def test_ledger_entry_keys_are_sorted_at_every_level(self):
        e = Entry(key="k", at=1.0, meta={"z": 1, "a": 2})
        self.assertEqual(e.as_json(), '{"at": 1.0, "key": "k", "meta": {"a": 2, "z": 1}}')

    def test_baseline_file_keys_are_sorted(self):
        results = [CaseResult(n, passes=1, trials=1, min_pass_rate=0.5) for n in ("b", "a")]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "base.json")
            Baseline().save(path, results)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertLess(text.index('"cases"'), text.index('"recorded_at"'))
        self.assertLess(text.index('"a"'), text.index('"b"'))


if __name__ == "__main__":
    unittest.main()
