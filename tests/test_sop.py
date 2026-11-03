"""SOP rules changed at runtime (fincrime/engines/sop.py) on the file book:
the shipped values read as before, only the team lead changes one, a change
is kept through a restart and undone by the reset, and it applies at once:
to the proof-of-address checks, to the SLA clocks, and to the open cases the
AI Agent decided, checked again on their stored read.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_sop
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-sop-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime.engines import ai_agent, sla, sop, workflow  # noqa: E402
from fincrime.utils import dates  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_ai_agent import Stub, perfect_read  # noqa: E402

LEAH = "leah.fernandez@straive.com"


def flash_text(response):
    return " | ".join(re.findall(r'role="status"[^>]*>\s*<span[^>]*>[^<]*</span>\s*<span>([^<]+)</span>',
                                 response.data.decode("utf-8")))


class SopRulesTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        cls.book_before = fcc.CASE_BOOK
        fcc.CASE_BOOK = "file"

    @classmethod
    def tearDownClass(cls):
        fcc.CASE_BOOK = cls.book_before
        StateStore.clear()
        fcc._load_seed()

    def setUp(self):
        StateStore.clear()
        fcc._load_seed()

    def login(self, key):
        self.c = fcc.app.test_client()
        self.assertEqual(self.c.post("/login", data={"analyst": key, "password": "1234"}).status_code, 302)

    def change(self, key, *values):
        data = {"key": key, "value": list(values) if len(values) != 1 else values[0]}
        return self.c.post("/sop/change", data=data, follow_redirects=True)

    def test_the_shipped_values_read_as_before(self):
        self.assertEqual(sop.poa_age_label(), "Issued within the last 6 months")
        self.assertEqual(sop.resubmit_poa_reasons(), fcc.vocab.RESUBMIT_POA_REASONS)
        self.assertEqual(ai_agent.policy(), ai_agent.POLICY)
        self.assertIn("issued within the last 6 months (except an Aadhaar card", ai_agent.POLICY)
        self.assertEqual((sop.poa_age_text(91), sop.poa_age_text(365), sop.poa_age_text(31), sop.poa_age_text(45)),
                         ("3 months", "12 months", "1 month", "45 days"))

    def test_only_the_team_lead_changes_a_rule_and_it_is_kept(self):
        self.login("amara.iyer")
        self.assertIn('id="sop-rules"', self.c.get("/sop").data.decode("utf-8"))      # everyone reads them
        self.assertIn("needs QC access", flash_text(self.change("poa_max_age_days", "365")))
        self.assertEqual(sop.value("poa_max_age_days"), 183)
        self.login("leah.fernandez")
        said = flash_text(self.change("poa_max_age_days", "365"))
        self.assertIn("Proof of address: issued within: 183 days → 365 days. It applies at once", said)
        self.assertEqual((sop.value("poa_max_age_days"), sop.version()), (365, 1))
        self.assertEqual(fcc.SOP_SETTINGS["history"][-1]["by"], LEAH)
        page = self.c.get("/sop").data.decode("utf-8")
        self.assertIn("365 days", page)
        self.assertIn('id="sop-history"', page)
        for value, needle in (("0", "between 30 and 730"), ("abc", "whole number"), ("365", "is already 365 days")):
            self.assertIn(needle, flash_text(self.change("poa_max_age_days", value)))
        self.assertIn("not one the console knows", flash_text(self.change("sanctioned_countries", "XX")))
        self.assertIn("keep at least 1", flash_text(self.c.post("/sop/change", data={"key": "accepted_poa_types"},
                                                                  follow_redirects=True)))
        self.assertIn("Unknown SOP rule", flash_text(self.change("nonsense", "1")))
        fcc._load_seed()                                                  # a restart keeps it
        self.assertEqual((sop.value("poa_max_age_days"), sop.version()), (365, 1))
        self.c.post("/reset")                                             # the reset goes back to the shipped values
        self.assertEqual((sop.value("poa_max_age_days"), sop.version(), fcc.SOP_SETTINGS["history"]), (183, 0, []))

    def test_a_change_applies_to_the_checks_at_once(self):
        c = next(x for x in fcc.KYC_CASES if x["tenant"] == "ttlive" and x["status"] == "DataConfirming"
                 and (workflow.poa_docs(x) or [{}])[0].get("kind") == "utility_bill"
                 and 100 <= (dates.now() - dates.parse(workflow.poa_docs(x)[0]["issued_on"])).days <= 180)
        age = workflow.poa_checks(c)[0]
        self.assertEqual((age["key"], age["label"], age["ok"]), ("poa_age", "Issued within the last 6 months", True))
        with mock.patch.dict(fcc.SOP_SETTINGS["values"], {"poa_max_age_days": 91}):
            age = workflow.poa_checks(c)[0]
            self.assertEqual((age["label"], age["ok"]), ("Issued within the last 3 months", False))
            self.assertEqual(sop.resubmit_poa_reasons()[0], "The proof of address is older than 3 months")
        country = c["documents"][-1].get("country") or c["client"]["nationality"]
        with mock.patch.dict(fcc.SOP_SETTINGS["values"], {"sanctioned_countries": [country]}):
            failed = {x["key"] for x in workflow.poa_checks(c) if not x["ok"]}
            self.assertIn("poa_country", failed)
        with mock.patch.dict(fcc.SOP_SETTINGS["values"], {"accepted_poa_types": ["bank_statement"]}):
            self.assertIn("poa_type", {x["key"] for x in workflow.poa_checks(c) if not x["ok"]})

    def test_a_new_sla_moves_every_open_deadline(self):
        c = next(x for x in fcc.KYC_CASES if x["tenant"] == "ttlive" and x["status"] == "DataConfirming")
        self.assertEqual(sla.active_due(c), (c["sla_due_at"], "L1"))     # the shipped 48 hours, from ingestion
        with mock.patch.dict(fcc.SOP_SETTINGS["values"], {"sla_hours_l1": 72}):
            self.assertEqual(sla.active_due(c)[0], dates.add_hours(c["created_at"], 72))

    def test_a_longer_age_limit_brings_back_a_case_waiting_for_a_new_proof_of_address(self):
        def only_old_poa(x):
            found = ai_agent.checks(x, perfect_read(x))[0]
            return (ai_agent.eligible(x) and x["screening"]["workflow"] == "auto-verified" and not x["current_reviewer"]
                    and [y["check"] for y in found if not y["ok"]] == ["Proof of address checks"]
                    and [f["key"] for f in workflow.poa_failures(x)] == ["poa_age"])
        c = next(x for x in fcc.KYC_CASES if only_old_poa(x))
        alert, wc = fcc._linked(c)
        rec = ai_agent.run_case(c, alert, wc, fcc._render_document, call=Stub(perfect_read(c)), sleep=lambda s: None)
        self.assertEqual((rec["decision"], c["status"], rec["sop"]), ("resubmit", "ResubmitPOA", 0))
        age = (dates.now() - dates.parse(workflow.poa_docs(c)[0]["issued_on"])).days
        self.login("leah.fernandez")
        said = flash_text(self.change("poa_max_age_days", str(max(age + 5, 200))))
        self.assertIn("verified", said)
        self.assertEqual((c["status"], c["ai_agent"]["decision"], c["ai_agent"]["sop"]), ("Verified", "verified", 1))
        self.assertIn("The SOP rules changed", " ".join(e["detail"] for e in c["audit"]))
        self.assertFalse(ai_agent.needs_sop_recheck(c))
        # a shorter one again: a closed case stays closed
        self.change("poa_max_age_days", "60")
        self.assertEqual(c["status"], "Verified")


if __name__ == "__main__":
    unittest.main()
