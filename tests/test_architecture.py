"""The model benchmark's scoring (file book, no
network: the benchmark's model calls are not made here).

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_architecture
"""
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-arch-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime import benchmark  # noqa: E402


class ArchitectureTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        cls.book_before = fcc.CASE_BOOK
        fcc.CASE_BOOK = "file"
        StateStore.clear()
        fcc._load_seed()

    @classmethod
    def tearDownClass(cls):
        fcc.CASE_BOOK = cls.book_before
        StateStore.clear()
        fcc._load_seed()

    def test_the_right_read_scores_full_marks(self):
        sample = benchmark.sample(fcc.KYC_CASES, 8)
        self.assertEqual(len(sample), 8)
        self.assertEqual(sum(benchmark.spoiled(c) for c in sample), 2)
        for c in sample:
            row = benchmark.score(c, benchmark.truth(c))
            if not row["spoiled"]:
                self.assertTrue(all(row["fields"].values()), (c["id"], row["fields"]))
                self.assertTrue(row["decision_agrees"])

    def test_a_misread_and_a_missed_defect_cost_marks(self):
        sample = benchmark.sample(fcc.KYC_CASES, 8)
        clean = next(c for c in sample if not benchmark.spoiled(c))
        read = copy.deepcopy(benchmark.truth(clean))
        read["id"]["dob"] = "1900-01-01"
        row = benchmark.score(clean, read)
        self.assertFalse(row["fields"]["id.dob"])
        self.assertFalse(row["model_pass"])                               # the wrong date of birth fails the check
        bad = next(c for c in sample if benchmark.spoiled(c))
        self.assertFalse(benchmark.score(bad, benchmark.truth(bad))["flagged"])          # a reader that saw no problem
        flagged = copy.deepcopy(benchmark.truth(bad))
        flagged["id"].update(readable=False, problems=["blurred"])
        self.assertTrue(benchmark.score(bad, flagged)["flagged"])
        summary = benchmark.summarise(benchmark.MODELS[0], [dict(benchmark.score(clean, read), tokens_in=1_000_000, tokens_out=0, seconds=1)])
        self.assertEqual(summary["cost_per_case"], 5.0)                  # $5 per million input tokens
        self.assertEqual(summary["field_accuracy"], 90.0)                # nine of ten fields


if __name__ == "__main__":
    unittest.main()
