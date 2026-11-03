"""Scan an ID (fincrime/documents/id_scan.py and /scan): the checks on a
read, the MRZ transcription counted out to 44, and the pages (upload,
result, JSON, delete). No network: the model call is stood in for.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_id_scan
"""
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-scan-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime.documents import id_scan, ocr, uploads  # noqa: E402

LINE1 = "P<INDUPADHYAY<<ASHISH<<<<<<<<<<<<<<<<<<<<<<<"
LINE2 = "S3321051<3IND6206295M3306024<<<<<<<<<<<<<<<2"


def a_read(**fields):
    base = {"full_name": "ASHISH UPADHYAY", "surname": "UPADHYAY", "given_names": "ASHISH", "date_of_birth": "1962-06-29",
            "sex": "M", "nationality": "IND", "document_number": "S3321051", "personal_number": "", "date_of_issue": "2023-06-03",
            "date_of_expiry": "2033-06-02", "place_of_birth": "Patna", "address": "", "father_or_spouse_name": "",
            "issuing_authority": ""}
    base.update(fields)
    return {"document_type": "Passport", "issuing_country": "India", "readable": True, "problems": [], "fields": base,
            "mrz": [LINE1, LINE2], "uncertain": [], "note": ""}


def states(cs):
    return {c["label"]: c["state"] for c in cs}


class CheckTests(unittest.TestCase):
    today = date(2026, 10, 28)

    def test_a_good_passport_passes(self):
        cs = id_scan.checks(a_read(), self.today)
        s = states(cs)
        for label in ("Photo", "Main fields", "Expiry", "Age", "MRZ check digits (the model's transcription)",
                      "Printed fields against the MRZ"):
            self.assertEqual(s[label], "ok", label)
        self.assertEqual(id_scan.verdict({"read": a_read(), "checks": cs}), "Check" if "warn" in s.values() else "Pass")

    def test_expired_misread_and_minor(self):
        s = states(id_scan.checks(a_read(date_of_expiry="2020-01-01", document_number="S3321057"), self.today))
        self.assertEqual(s["Expiry"], "fail")
        self.assertEqual(s["Printed fields against the MRZ"], "fail")      # the printed number is not the zone's
        s = states(id_scan.checks(a_read(date_of_birth="2012-01-01"), self.today))
        self.assertEqual(s["Age"], "warn")

    def test_a_miscounted_filler_run_is_counted_out(self):
        one, two = id_scan._td3([LINE1 + "<", LINE2[:28] + "<" + LINE2[28:]])
        self.assertEqual((len(one), len(two)), (44, 44))
        self.assertTrue(ocr.parse(one, two)["valid"])

    def test_no_ocr_engine_runs(self):
        """The read is the model's alone: no Tesseract call, no OCR check."""
        with mock.patch.object(id_scan, "model_ready", lambda: False):
            out = id_scan.scan(b"jpeg")
        self.assertIsNone(out["read"])
        self.assertNotIn("tesseract", out)
        self.assertIn("LLM_FOUNDRY_KEY", out["error"])
        self.assertFalse([c for c in id_scan.checks(a_read(), self.today) if "OCR" in c["label"] or "Tesseract" in c["detail"]])

    def test_a_document_that_is_not_an_id(self):
        read = dict(a_read(), document_type="Not an identity document", fields={k: "" for k, _ in id_scan.FIELDS}, mrz=[])
        cs = id_scan.checks(read, self.today)
        self.assertEqual(states(cs)["Identity document"], "fail")
        self.assertEqual(id_scan.verdict({"read": read, "checks": cs}), "Fail")


class PageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        StateStore.clear()
        fcc._load_seed()

    def client(self):
        c = fcc.app.test_client()
        c.post("/login", data={"analyst": "amara.iyer", "password": "1234"})
        return c

    def png(self):
        from PIL import Image
        out = io.BytesIO()
        Image.new("RGB", (900, 600), "white").save(out, "PNG")
        return out.getvalue()

    def test_upload_result_json_and_delete(self):
        c = self.client()
        page = c.get("/scan").data.decode("utf-8")
        self.assertIn('id="scan-upload"', page)
        self.assertIn("Scan ID", page)                         # the menu entry
        result = {"read": a_read(), "model": {"model": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "tokens_in": 1100,
                                              "tokens_out": 300, "cost": 0.001, "seconds": 5.0},
                  "error": ""}
        result["checks"] = id_scan.checks(result["read"])
        no_call = lambda case_id, who, hit, sop, settings: {"status": "failed", "error": "no call in the suite", "sop": sop["version"]}
        with mock.patch.object(id_scan, "scan", lambda jpeg, model_id=None: dict(result)),                 mock.patch.object(fcc.id_screening, "decide", no_call):
            r = c.post("/scan", data={"image": (io.BytesIO(self.png()), "id.png"), "model": "gemini-2.5-flash"},
                       content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        sid = r.headers["Location"].rsplit("/", 1)[-1]
        page = c.get(f"/scan/{sid}").data.decode("utf-8")
        for needle in ("Information fetched", "UPADHYAY", "S3321051", 'id="scan-checks"', "Gemini 2.5 Flash"):
            self.assertIn(needle, page)
        img = c.get(f"/scan/{sid}/image")
        self.assertEqual((img.status_code, img.mimetype), (200, "image/jpeg"))
        self.assertIn("no-store", img.headers["Cache-Control"])
        data = c.get(f"/scan/{sid}/data.json").get_json()
        self.assertEqual(data["fields"]["document_number"], "S3321051")
        self.assertEqual(c.get("/scan").status_code, 200)
        rec = next(r for r in StateStore.records("scan") if r["id"] == sid)
        self.assertTrue(os.path.exists(os.path.join(uploads.base(), rec["image"])))
        c.post(f"/scan/{sid}/delete")
        self.assertFalse(os.path.exists(os.path.join(uploads.base(), rec["image"])))
        self.assertEqual(c.get(f"/scan/{sid}").status_code, 404)

    def test_a_file_that_is_not_an_image_is_refused(self):
        c = self.client()
        r = c.post("/scan", data={"image": (io.BytesIO(b"not an image"), "id.pdf")}, content_type="multipart/form-data",
                   follow_redirects=True)
        self.assertIn("not an image this tool can read", r.data.decode("utf-8"))
        self.assertFalse(StateStore.records("scan"))


if __name__ == "__main__":
    unittest.main()
