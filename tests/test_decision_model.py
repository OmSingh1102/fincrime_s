"""The decision model's evaluation (fincrime/decision_model.py) and the
Model evaluation page: what the model is shown, the split, the baselines,
the scoring and the page. No network: no model is called here.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_decision_model
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-eval-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime import decision_model as dm  # noqa: E402


class FactsAndSplitTests(unittest.TestCase):

    def test_the_model_never_sees_the_analysts_answers(self):
        for row in list(dm.rows().values())[::25]:
            shown = dm.facts(row)
            for col in ("disposition_status", "compliance_action_regulatory_notes", "normalisation_resolution_applied",
                        "discrepancy_analysis_dob_address", "pain_point_detail", "screening_impact", "case_analyst"):
                self.assertNotIn(row[col], shown, f"{row['kyc_case_id']}: {col} shown")
            for leak in ("Exact Match", "yrs diff", "transposed", "Year only", "Risk"):
                self.assertNotIn(leak, shown)
            self.assertNotIn(row["name_variance_type"], shown)
            self.assertIn(row["world_check_entity_subject_name"], shown)

    def test_the_split_is_fixed_stratified_and_disjoint(self):
        a, b = dm.split(), dm.split()
        self.assertEqual(a, b)
        self.assertEqual((len(a["test"]), len(a["train"])), (200, 800))
        self.assertFalse(set(a["test"]) & set(a["train"]))
        self.assertTrue(set(a["tune"]) <= set(a["train"]))
        by_outcome = {}
        for cid in dm.rows():
            by_outcome.setdefault(dm.outcome(dm.rows()[cid]), []).append(cid)
        for key, ids in by_outcome.items():
            self.assertEqual(sum(i in set(a["test"]) for i in ids), round(len(ids) * dm.TEST_SHARE), key)

    def test_examples_come_from_the_library_and_never_the_case_itself(self):
        sp, rows = dm.split(), dm.rows()
        library = [rows[i] for i in sp["train"]]
        case = rows[sp["tune"][0]]                    # a training case: it is in the library
        picked = dm.neighbours(case, library, 6)
        self.assertEqual(len(picked), 6)
        self.assertNotIn(case["kyc_case_id"], [p["kyc_case_id"] for p in picked])
        self.assertTrue(all(p["kyc_case_id"] in set(sp["train"]) for p in picked))
        self.assertNotIn("Analysts' outcome", dm.prompt(case, library, 0))
        self.assertEqual(dm.prompt(case, library, 6).count("Analysts' outcome"), 6)

    def test_the_sop_change_note_stays_out_of_the_prompt(self):
        for v in dm.sop_versions():
            text = dm.system(v)
            self.assertNotIn("<!--", text)
            self.assertIn("Variance reference", text)
        self.assertTrue(dm.sop_changes("v2"))
        self.assertEqual(dm.sop_changes("v1"), [])

    def test_answers_are_held_to_the_allowed_labels(self):
        ref = dm.reference()["name"]
        self.assertEqual(dm._closest("None - consistent across documents", ref), dm.NONE)
        self.assertEqual(dm._closest("true_match", dm.OUTCOME_KEYS), "true_match")
        self.assertEqual(dm._closest("maybe", dm.OUTCOME_KEYS), "maybe")


class BaselineAndScoringTests(unittest.TestCase):

    def test_the_rules_clear_only_a_conflicting_date(self):
        rows = dm.rows().values()
        other = next(r for r in rows if dm.signals(r)["date of birth"] == "other date")
        same = next(r for r in rows if dm.signals(r)["date of birth"] == "same date")
        self.assertEqual(dm.rules_decide(other)["outcome"], "clear")
        self.assertEqual(dm.rules_decide(same)["outcome"], dm.PERSON)

    def test_the_tree_learns_the_date_of_birth(self):
        sp, rows = dm.split(), dm.rows()
        tree = dm.train_tree([rows[i] for i in sp["train"]])
        self.assertEqual(dm.tree_predict(tree, dict(dm.signals(next(iter(rows.values()))), **{"date of birth": "same date"})),
                         "true_match")
        self.assertTrue(any("date of birth" in line for line in dm.tree_lines(tree)))

    def test_scoring(self):
        per_case = [
            {"case": "a", "truth": "clear", "outcome": "clear", "name_truth": dm.NONE, "name_variance": dm.NONE,
             "address_truth": dm.NONE, "address_variance": dm.NONE, "confidence": 95, "tokens_in": 1000, "tokens_out": 100, "seconds": 2},
            {"case": "b", "truth": "true_match", "outcome": "clear", "name_truth": dm.NONE, "name_variance": "X",
             "address_truth": dm.NONE, "address_variance": dm.NONE, "confidence": 92, "tokens_in": 1000, "tokens_out": 100, "seconds": 4},
            {"case": "c", "truth": "escalate", "outcome": "request_documents", "name_truth": dm.NONE, "name_variance": dm.NONE,
             "address_truth": dm.NONE, "address_variance": dm.NONE, "confidence": 60, "tokens_in": 1000, "tokens_out": 100, "seconds": 6},
            {"case": "d", "truth": "clear", "error": "RuntimeError: 500", "name_truth": dm.NONE, "address_truth": dm.NONE, "seconds": 1},
        ]
        s = dm.summarise(per_case, (1.0, 10.0))
        self.assertEqual((s["cases"], s["errors"]), (4, 1))
        self.assertEqual(s["accuracy"], 33.3)
        self.assertEqual(s["settled_accuracy"], 66.7)          # escalate and ask the customer are both "open"
        self.assertEqual((s["missed_true_matches"], s["wrongly_cleared"]), (1, 1))
        self.assertEqual(s["confident_clears"], {"n": 2, "wrong": 1})
        self.assertEqual(s["outcome_matrix"]["cells"]["true_match"]["clear"], 1)
        self.assertEqual(s["outcome_matrix"]["cells"]["clear"]["failed"], 1)
        self.assertEqual(s["name_accuracy"], 66.7)
        self.assertEqual(s["cost_per_case"], 0.002)
        mv = dm.matrix_view(s["outcome_matrix"], dm.SHORT.get, dm._cleared_wrongly)
        kinds = {r["label"]: [c["kind"] for c in r["cells"]] for r in mv["rows"]}
        self.assertEqual(kinds["True match"][0], "danger")
        self.assertEqual(kinds["Clear"][0], "hit")


if __name__ == "__main__":
    unittest.main()
