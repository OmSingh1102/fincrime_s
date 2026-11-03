"""Upload cases on the file book: the sample, an upload, what intake does to
the new cases, duplicates and a wrong file refused, the AI Agent started for
the auto-verified ones, and the uploads surviving a reload.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_upload
"""
import csv
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-upload-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime.engines import ai_agent  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_ai_agent import Stub, perfect_read  # noqa: E402


def flash_text(response):
    return " | ".join(re.findall(r'role="status"[^>]*>\s*<span[^>]*>[^<]*</span>\s*<span>([^<]+)</span>',
                                 response.data.decode("utf-8")))


class UploadTestCase(unittest.TestCase):

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
        self.c = fcc.app.test_client()
        self.assertEqual(self.c.post("/login", data={"analyst": "amara.iyer", "password": "1234"}).status_code, 302)
        self.started = []
        patcher = mock.patch.object(fcc, "_agent_start", lambda by: self.started.append(by))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sample(self):
        r = self.c.get("/kyc/upload/sample.csv")
        self.assertEqual(r.status_code, 200)
        self.last_sample = r.data
        return r.data

    def upload(self, data, name="new_cases.csv"):
        with mock.patch.object(fcc.ai_review, "configured", lambda: True):
            return self.c.post("/kyc/upload", data={"file": (io.BytesIO(data), name)},
                               content_type="multipart/form-data", follow_redirects=True)

    def test_the_page_and_the_sample(self):
        self.assertIn('id="upload-cases"', self.c.get("/kyc/list").data.decode("utf-8"))
        self.assertIn('id="upload-form"', self.c.get("/kyc/upload").data.decode("utf-8"))
        rows = list(csv.DictReader(io.StringIO(self.sample().decode("utf-8-sig"))))
        ids = [r["kyc_case_id"] for r in rows]
        self.assertEqual(len(set(ids)), 5)
        self.assertTrue(all(i not in fcc.KYC_BY_ID and int(i.rsplit("-", 1)[1]) > 4010 for i in ids))
        self.assertTrue(all(r["disposition_status"] == "" for r in rows))

    def test_an_upload_becomes_cases_and_starts_the_agent(self):
        r = self.upload(self.sample())
        self.assertIn("5 case(s) added from new_cases.csv", flash_text(r))
        ids = [r["kyc_case_id"] for r in csv.DictReader(io.StringIO(self.last_sample.decode("utf-8-sig")))]
        new = [fcc.KYC_BY_ID[i] for i in ids]
        self.assertEqual(len(fcc.KYC_CASES), 1005)
        self.assertTrue(all("Uploaded" in c["tags"] and c["status"] == "DataConfirming" for c in new))
        self.assertEqual([c["screening"]["workflow"] for c in new],
                         ["auto-verified", "auto-verified", "auto-verified", "human-in-the-loop", "human-in-the-loop"])
        self.assertEqual(new[4]["tenant"], "ttshop")
        self.assertEqual(self.started, ["upload"])
        for c in new[:4]:                                                       # the Individuals list, filtered to the upload
            self.assertIn(c["id"], r.data.decode("utf-8"))
        page = self.c.get(f"/kyc/detail/{new[0]['id']}").data.decode("utf-8")
        self.assertNotIn('id="recorded-outcome"', page)                         # a new case has no recorded outcome
        self.assertEqual(self.c.get(f"/documents/{new[0]['id']}/d1.svg").status_code, 200)
        # the agent verifies a clean one, sends the surname mismatch to an analyst
        for c, want in ((new[0], "verified"), (new[2], "human")):
            alert, wc = fcc._linked(c)
            rec = ai_agent.run_case(c, alert, wc, fcc._render_document, call=Stub(perfect_read(c)), sleep=lambda s: None)
            self.assertEqual(rec["decision"], want, rec["reasons"])
        self.assertIn("Name on the ID differs from the CRM", new[2]["ai_agent"]["reasons"])
        # kept through a reload (a restart): the batch rebuilds the cases
        fcc._load_seed()
        self.assertEqual(len(fcc.KYC_CASES), 1005)
        self.assertIn(ids[-1], fcc.KYC_BY_ID)
        self.assertIn('id="upload-history"', self.c.get("/kyc/upload").data.decode("utf-8"))

    def test_duplicates_and_a_wrong_file_are_refused(self):
        data = self.sample()
        self.upload(data)
        r = self.upload(data)
        self.assertIn("No case was added", flash_text(r))
        self.assertIn("already exists", flash_text(r))
        self.assertEqual(len(fcc.KYC_CASES), 1005)
        r = self.upload(b"case_id,name\r\nX-1,Somebody\r\n", "wrong.csv")
        self.assertIn("The file lacks", flash_text(r))
        self.assertEqual(len(fcc.KYC_CASES), 1005)

    def test_an_upload_can_be_removed(self):
        self.upload(self.sample())
        ids = [r["kyc_case_id"] for r in csv.DictReader(io.StringIO(self.last_sample.decode("utf-8-sig")))]
        new = fcc.KYC_BY_ID[ids[0]]
        alert, wc = fcc._linked(new)
        for kind, record in (("kyc", new), ("alert", alert), ("wc", wc)):      # something was done on it
            fcc._save(kind, record)
        batch = StateStore.records("upload")[0]
        self.assertIn('id="remove-upload-1"', self.c.get("/kyc/upload").data.decode("utf-8"))
        # not while the AI Agent is reading one of its cases
        fcc.AI_AGENT["busy"].add(ids[0])
        try:
            r = self.c.post(f"/kyc/upload/{batch['id']}/remove", follow_redirects=True)
            self.assertIn("is reading", flash_text(r))
            self.assertIn(ids[0], fcc.KYC_BY_ID)
        finally:
            fcc.AI_AGENT["busy"].discard(ids[0])
        r = self.c.post(f"/kyc/upload/{batch['id']}/remove", follow_redirects=True)
        self.assertIn("Removed the upload new_cases.csv: 5 case(s)", flash_text(r))
        self.assertEqual(len(fcc.KYC_CASES), 1000)
        self.assertFalse(set(ids) & set(fcc.KYC_BY_ID))
        self.assertNotIn(alert["id"], fcc.ALERT_BY_ID)
        self.assertNotIn(wc["id"], fcc.WC_BY_ID)
        for kind, gone in (("kyc", ids[0]), ("alert", alert["id"]), ("wc", wc["id"])):
            self.assertNotIn(gone, {x["id"] for x in StateStore.records(kind)})
        self.assertEqual(self.c.get(f"/kyc/detail/{ids[0]}").status_code, 404)
        # the batch stays as history, marked removed, and cannot be removed twice
        page = self.c.get("/kyc/upload").data.decode("utf-8")
        self.assertIn("Removed by", page)
        self.assertNotIn('id="remove-upload-1"', page)
        self.assertIn("not there any more", flash_text(self.c.post(f"/kyc/upload/{batch['id']}/remove", follow_redirects=True)))
        # a restart does not bring the cases back, and the same file uploads again
        fcc._load_seed()
        self.assertEqual(len(fcc.KYC_CASES), 1000)
        self.assertIn("5 case(s) added", flash_text(self.upload(self.last_sample)))
        self.assertEqual(len(fcc.KYC_CASES), 1005)

    def test_only_on_the_file_book(self):
        fcc.CASE_BOOK = "demo"
        try:
            r = self.c.get("/kyc/upload", follow_redirects=True)
            self.assertIn("reconciliation-log book", flash_text(r))
        finally:
            fcc.CASE_BOOK = "file"


if __name__ == "__main__":
    unittest.main()
