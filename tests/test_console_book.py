"""The console on the reconciliation-log book (CONSOLE_CASES=file): the 1,000
cases, what the AI screening rules did at intake set against what the file
records, the documents, Data Confirm, the paged lists, routing and search.

The other test modules run on the scripted demo book; this one switches the
app to the file book for its class and back afterwards. The book is built
once for the class, so each test works on its own cases.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_console_book
"""
import os
import re
import sys
import tempfile
import time
import unittest
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-book-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime.data import loader, recon_book  # noqa: E402

AMARA, JON, MARIO = "amara.iyer@straive.com", "jon.miranda@straive.com", "mario.kim@straive.com"


def flash_text(response):
    return " | ".join(re.findall(r'role="status"[^>]*>\s*<span[^>]*>[^<]*</span>\s*<span>([^<]+)</span>',
                                 response.data.decode("utf-8")))


class ConsoleBookTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        cls.book_before = fcc.CASE_BOOK
        fcc.CASE_BOOK = "file"
        StateStore.clear()
        started = time.perf_counter()
        fcc._load_seed()
        cls.build_seconds = time.perf_counter() - started

    @classmethod
    def tearDownClass(cls):
        fcc.CASE_BOOK = cls.book_before
        StateStore.clear()
        fcc._load_seed()

    def setUp(self):
        self.c = fcc.app.test_client()
        self.K = fcc.KYC_BY_ID

    # helpers ---------------------------------------------------------------
    def login(self, key, tenant=None):
        r = self.c.post("/login", data={"analyst": key, "password": "1234"})
        self.assertEqual(r.status_code, 302)
        if tenant:
            self.c.post("/tenant", data={"tenant": tenant, "next": "/kyc/list"})

    def post(self, path, **data):
        return self.c.post(path, data=data, follow_redirects=True)

    def confirm(self, case, **fields):
        self.post(f"/kyc/{case['id']}/reserve")
        c = case["client"]
        data = {"name": c["legal_name"], "name_ok": "1", "dob": c["dob"], "dob_ok": "1", "nationality": c["nationality"],
                "nationality_ok": "1", "poa_ok": "1", "summary": "Checked against the passport and the proof of address."}
        data.update(fields)
        return self.post(f"/kyc/{case['id']}/data_confirm", **data)

    def alert_of(self, case):
        return fcc.ALERT_BY_ID[case["screening"]["alert_case_id"]]

    # the book ------------------------------------------------------------------
    def test_book_shape(self):
        self.assertEqual((len(fcc.KYC_CASES), len(fcc.ALERT_CASES), len(fcc.WC_CASES)), (1000, 1000, 1000))
        ids = [c["id"] for c in fcc.KYC_CASES]
        self.assertEqual(len(set(ids)), 1000)
        self.assertEqual(set(ids), {f"KYC-2026-{n}" for n in range(3011, 4011)})
        self.assertEqual(Counter(c["tenant"] for c in fcc.KYC_CASES), {"ttlive": 943, "ttshop": 57})
        self.assertEqual({c["status"] for c in fcc.KYC_CASES}, {"DataConfirming"})
        self.assertTrue(all(c.get("recon") and c["poa_required"] and c["screening"]["hit"] for c in fcc.KYC_CASES))
        self.assertEqual(len({m["wc_id"] for w in fcc.WC_CASES for m in w["matches"]}), 1000)
        self.assertEqual([a["key"] for a in fcc.ANALYSTS],
                         ["amara.iyer", "jon.miranda", "mario.kim", "nisha.rao", "ryan.lee", "leah.fernandez"])
        self.assertLess(self.build_seconds, 6.0)
        c = self.K["KYC-2026-3011"]
        self.assertEqual((c["client"]["legal_name"], c["client"]["id_number"], c["client"]["nationality"], c["client"]["dob"]),
                         ("NGUYEN NGOC PHUONG", "C3048862", "VN", "2001-07-06"))
        self.assertEqual(c["client"]["native_name"], "NGUYỄN NGỌC PHƯƠNG")
        self.assertEqual(c["recon"]["disposition"], "False Positive - Cleared")
        m = fcc.WC_BY_ID[c["screening"]["alert_case_id"]]["matches"][0]
        self.assertEqual((m["wc_id"], m["name"], m["dobs"], m["types"]), ("WC-554473", "NGUYEN, Ngoc Phuong", ["1967-09-29"], ["RE"]))
        # the starter set: routed at intake, everybody below capacity
        held = Counter(c["current_reviewer"] for c in fcc.KYC_CASES if c["current_reviewer"])
        self.assertEqual(held, {AMARA: 3, JON: 3, MARIO: 3})
        for a in fcc.ANALYSTS:
            if a["capacity"]:
                self.assertLess(fcc.routing.load(a["email"]), a["capacity"], a["key"])
        self.assertEqual(loader.load_seed("demo")[1].__len__(), 59)
        self.assertEqual((loader.book_from("DEMO"), loader.book_from("nonsense"), loader.book_from(None)), ("demo", "screening", "screening"))

    def test_rules_at_intake_against_the_file(self):
        """The rules clear only what the file's analysts also cleared; every
        exact or transposed date of birth and every company stays human."""
        # read off the screening cases, which later Data Confirms in this class leave alone
        auto = [c for c in fcc.KYC_CASES if self.alert_of(c)["status"] == "Closed"
                and self.alert_of(c)["case_outcome"]["by"] == "System User"]
        held = [c for c in fcc.KYC_CASES if c not in auto]
        self.assertEqual(len(auto), 592)
        self.assertEqual({c["recon"]["outcome"] for c in auto}, {"FP"})
        self.assertEqual(Counter(c["recon"]["outcome"] for c in held), {"TP": 171, "Pending": 128, "Escalated": 72, "FP": 37})
        for marker, n in (("transposed", 22), ("Exact", 157)):
            cases = [c for c in fcc.KYC_CASES if marker in c["recon"]["wc_dob"]]
            self.assertEqual(len(cases), n)
            self.assertTrue(all(self.alert_of(c)["alerts"][0]["outcome"] == "Pending" for c in cases), marker)
        self.assertTrue(all(c in held for c in fcc.KYC_CASES if c["tenant"] == "ttshop"))

    def test_documents_are_the_files(self):
        self.login("amara.iyer")
        c = self.K["KYC-2026-3011"]
        names = [(d["id"], d["category"], d.get("hub_file")) for d in c["documents"]]
        self.assertEqual(names, [("d1", "Passport", "passport"), ("d2", "Passport", "passport-back"),
                                 ("d3", "NationalID", "secondary"), ("d4", "ProofOfAddress", "poa")])
        passport = self.c.get(f"/documents/{c['id']}/d1.svg")
        self.assertEqual(passport.status_code, 200)
        body = passport.data.decode("utf-8")
        self.assertIn("C3048862", body)
        self.assertIn("P&lt;VNMNGUYEN&lt;&lt;NGOC&lt;PHUONG", body)                # the file's MRZ line 1
        card = self.c.get(f"/documents/{c['id']}/d3.svg").data.decode("utf-8")
        self.assertIn("NGUYỄN NGỌC PHƯƠNG", card)
        self.assertIn("CĂN CƯỚC CÔNG DÂN", card)
        self.assertIn("CT07", self.c.get(f"/documents/{c['id']}/d4.svg").data.decode("utf-8"))
        self.assertEqual(c["documents"][3]["kind"], "residence_confirmation")
        flat = self.K["KYC-2026-3118"]
        self.assertEqual((flat["documents"][0]["quality"], flat["vendor_check"]["result"]), ("scan", "Declined"))
        self.assertIn(b'data-capture="flat_scan"', self.c.get(f"/documents/{flat['id']}/d1.svg").data)
        self.assertIn("Vendor rejected", flat["manual_review_reason"])
        tampered = self.K["KYC-2026-3040"]
        self.assertEqual(tampered["documents"][0]["quality"], "ok")             # not labelled: the analyst must spot it
        self.assertIn("check digit", tampered["vendor_check"]["vendor_reason"])
        company = self.K["KYC-2026-3029"]
        licence = self.c.get(f"/documents/{company['id']}/d1.svg").data.decode("utf-8")
        for needle in ("Ramasamy Agro Exports Pvt Ltd", "CERTIFICATE OF INCORPORATION", "Private Limited Company",
                       "Export of agricultural produce", "REGISTRAR OF COMPANIES"):
            self.assertIn(needle, licence)
        self.assertNotIn("FREE ZONE", licence)
        for case in (c, flat, company, self.K["KYC-2026-3043"]):
            for d in case["documents"]:
                self.assertTrue(fcc._render_document(case, d).startswith("<svg"), (case["id"], d["id"]))

    def test_case_page_shows_the_recorded_outcome(self):
        self.login("amara.iyer")
        page = self.c.get("/kyc/detail/KYC-2026-3012").data.decode("utf-8")
        for needle in ('id="recorded-outcome"', "True Positive - Application Rejected", "P. Nair", "2026-06-01",
                       "Given name / surname fields swapped", "1962-06-29 (Exact Match)", "WC-675768"):
            self.assertIn(needle, page)
        demo_case = loader.load_seed("demo")[1][0]
        self.assertNotIn("recon", demo_case)

    def test_confirm_auto_verifies_a_case_the_rules_cleared(self):
        self.login("amara.iyer")
        c = self.K["KYC-2026-3011"]
        r = self.confirm(c)
        self.assertEqual(c["status"], "Verified", flash_text(r))
        self.assertEqual((c["closed_by"], c["previous_reviewer"]), ("System User", AMARA))
        self.assertIn("auto-verified", flash_text(r))

    def test_clear_screening_never_verifies_a_bad_id(self):
        """The rules cleared the alert, but the vendor declined the ID or it has
        expired: Data Confirm leaves the case with a person, not Verified."""
        self.login("amara.iyer")
        auto = [c for c in fcc.KYC_CASES if c["screening"]["workflow"] == "auto-verified" and not c["current_reviewer"]]
        declined = next(c for c in auto if c["vendor_check"]["result"] == "Declined" and c["documents"][0]["quality"] != "expired")
        expired = next(c for c in auto if c["documents"][0]["quality"] == "expired")
        for c, needle in ((declined, "ID declined by the vendor"), (expired, "ID expired on")):
            r = self.confirm(c)
            self.assertEqual(c["status"], "DataScreening", flash_text(r))
            self.assertEqual(c["screening"]["workflow"], "human-in-the-loop")
            self.assertIn(needle, c["audit"][-1]["detail"])
            self.assertFalse(c.get("closed_by"))

    def test_confirm_corrects_a_garbled_name(self):
        self.login("amara.iyer")
        c = self.K["KYC-2026-3146"]
        self.assertIn("Ã", c["client"]["legal_name"])                          # the CRM's mojibake
        r = self.confirm(c, name="VŨ CÔNG KHÁNH")
        self.assertEqual(c["client"]["legal_name"], "VŨ CÔNG KHÁNH", flash_text(r))
        self.assertTrue(c["change_log"] and c["change_log"][-1]["to"] == "VŨ CÔNG KHÁNH", c["change_log"])
        self.assertEqual(c["status"], "Verified")

    def test_confirm_sends_a_held_case_to_screening(self):
        self.login("amara.iyer")
        c = self.K["KYC-2026-3012"]
        r = self.confirm(c)
        self.assertEqual(c["status"], "DataScreening", flash_text(r))
        self.assertEqual(self.alert_of(c)["alerts"][0]["outcome"], "Pending")
        self.assertEqual(c["screening"]["workflow"], "human-in-the-loop")

    def test_confirm_refuses_an_old_proof_of_address(self):
        self.login("jon.miranda")
        c = self.K["KYC-2026-3037"]
        self.assertGreater(recon_book.VIEWS[c["id"]]["synth"]["poa_age_days"], 183)
        r = self.confirm(c)
        self.assertEqual(c["status"], "DataConfirming")
        self.assertIn("Issued within the last 6 months", flash_text(r))

    def test_company_confirms_as_a_seller(self):
        self.login("jon.miranda", tenant="ttshop")
        c = self.K["KYC-2026-3029"]
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn("Ramasamy Agro Exports Pvt Ltd", page)
        self.assertIn("Vignesh R", page)                                         # the UBO
        cl = c["client"]
        r = self.confirm(c, dob="", dob_ok="", id_number=cl["id_number"], id_number_ok="1")
        self.assertEqual(c["status"], "DataScreening", flash_text(r))            # a seller never verifies itself

    def test_transposed_date_stays_with_the_analyst(self):
        self.login("jon.miranda")
        c = self.K["KYC-2026-3046"]
        alert = self.alert_of(c)
        self.assertEqual((alert["status"], alert["alerts"][0]["outcome"]), ("Init", "Pending"))
        self.assertIn("1976-04-03", self.c.get(f"/name_screening/case/{alert['id']}").data.decode("utf-8"))

    # the lists, routing and search --------------------------------------------
    def test_case_list_and_queue_are_paged(self):
        self.login("amara.iyer")
        self.assertEqual(self.c.get("/kyc/list").data.decode("utf-8").count('data-case-id="'), 10)    # the real list's 10 / Page
        page = self.c.get("/kyc/list?per_page=100").data.decode("utf-8")
        self.assertEqual(page.count('data-case-id="'), 100)
        self.assertIn('Total: <b id="total-count">943</b>', page)
        self.assertEqual(self.c.get("/kyc/list?per_page=100&page=10").data.decode("utf-8").count('data-case-id="'), 43)
        page = self.c.get("/kyc/list?case_id=kyc-2026-3011").data.decode("utf-8")
        self.assertEqual(page.count('data-case-id="'), 1)
        self.assertIn('data-case-id="KYC-2026-3011"', page)
        page = self.c.get("/kyc/list?workflow=human-in-the-loop").data.decode("utf-8")
        self.assertIn('Total: <b id="total-count">351</b>', page)
        queue = self.c.get("/name_screening/queue?level=L1").data.decode("utf-8")
        self.assertEqual(queue.count('href="/name_screening/case/'), 100)
        self.assertIn("943 case(s)", queue)
        self.assertEqual(self.c.get("/name_screening/queue?level=L1&page=10").data.decode("utf-8")
                         .count('href="/name_screening/case/'), 43)

    def test_routing_console_and_run(self):
        self.login("nisha.rao")
        started = time.perf_counter()
        page = self.c.get("/routing").data.decode("utf-8")
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertIn("most urgent of", page)
        self.assertEqual(page.count('class="rt-check"'), 100)
        free = sum(a["capacity"] - fcc.routing.load(a["email"]) for a in fcc.ANALYSTS
                   if "L1" in a["roles"] and "ttlive" in a["tenants"])
        before = StateStore.count()
        r = self.post("/routing/run")
        self.assertIn("still waiting for capacity", flash_text(r))
        self.assertLessEqual(StateStore.count() - before, free)                 # only what it changed was saved
        for a in fcc.ANALYSTS:
            if "L1" in a["roles"] and "ttlive" in a["tenants"]:
                self.assertEqual(fcc.routing.load(a["email"]), a["capacity"], a["key"])

    def test_search_finds_the_files_ids(self):
        self.login("amara.iyer")
        c = self.K["KYC-2026-3011"]
        r = self.c.get("/name_screening/search?q=KYC-2026-3011")
        self.assertTrue(r.headers["Location"].endswith(f"/name_screening/case/{c['screening']['alert_case_id']}"))
        r = self.c.get("/name_screening/search?q=wc-554473")
        self.assertIn(f"/wc/{c['screening']['alert_case_id']}/match/", r.headers["Location"])

    def test_ai_page_sets_the_rules_against_the_file(self):
        self.login("amara.iyer")
        page = self.c.get("/ai-screening").data.decode("utf-8")
        self.assertIn('id="against-file"', page)
        self.assertIn("592 of 592 recorded as false positives (100%)", page)
        self.assertIn("every true match the file records is in this row", page)
        self.login("jon.miranda", tenant="ttshop")
        page = self.c.get("/ai-screening").data.decode("utf-8")
        self.assertIn("what the file's analysts recorded for the same 57 cases", page)


if __name__ == "__main__":
    unittest.main()
