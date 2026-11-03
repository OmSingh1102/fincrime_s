"""OCR (Tesseract) beside the AI Agent's read: the MRZ parser on the ICAO
9303 specimen (a fictional passport, "Anna Maria Eriksson of Utopia"), the
pooling of several readings, the agent's "Passport MRZ read by OCR" check
with OCR stood in for, and one real Tesseract run on the app's own drawn
passport (skipped on a machine without Tesseract).

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_ocr
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-ocr-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from PIL import Image  # noqa: E402
from fincrime.data import recon_book  # noqa: E402
from fincrime.documents import ocr, uploads  # noqa: E402
from fincrime.documents.raster import svg_to_png  # noqa: E402
from fincrime.engines import ai_agent, case_documents  # noqa: E402
from fincrime.filebook import documents as file_documents  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_ai_agent import Stub, perfect_read  # noqa: E402

SPECIMEN = ("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<", "L898902C36UTO7408122F1204159ZE184226B<<<<<10")


def drawn_mrz(case):
    v = recon_book.VIEWS[case["recon"]["id"]]
    return v["documents"]["passport"]["mrz"]["line1"], file_documents.mrz_line2(v)


def jpeg(png):
    with Image.open(io.BytesIO(png)) as im:
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


class MrzParserTestCase(unittest.TestCase):

    def test_the_icao_specimen(self):
        got = ocr.parse(*SPECIMEN)
        self.assertTrue(got["valid"], got["checks"])
        self.assertEqual(got["fields"], {"document_type": "P", "issuer": "UTO", "surname": "ERIKSSON", "given_names": "ANNA MARIA",
                                         "number": "L898902C3", "nationality": "UTO", "dob": "1974-08-12", "sex": "F",
                                         "expiry": "2012-04-15"})

    def test_a_broken_check_digit_is_not_valid(self):
        line2 = SPECIMEN[1][:19] + "3" + SPECIMEN[1][20:]                 # the date of birth's check digit
        got = ocr.parse(SPECIMEN[0], line2)
        self.assertFalse(got["valid"])
        self.assertEqual({k for k, ok in got["checks"].items() if not ok}, {"dob", "composite"})

    def test_look_alike_letters_in_number_fields_are_put_right(self):
        line2 = SPECIMEN[1][:13] + "74O8I2" + SPECIMEN[1][19:]          # OCR read O for 0 and I for 1 in the date
        got = ocr.parse(SPECIMEN[0], line2)
        self.assertTrue(got["valid"], got["checks"])
        self.assertEqual(got["fields"]["dob"], "1974-08-12")
        self.assertIsNone(ocr.parse("X" + SPECIMEN[0][1:], SPECIMEN[1]))   # not a passport's first line

    def test_readings_are_pooled_and_noise_is_left_out(self):
        bad = SPECIMEN[1][:5] + "8" + SPECIMEN[1][6:]                     # one reading misread the number
        raw = "\n".join(["PASSPORT  PASSEPORT  UTOPIA", SPECIMEN[0], bad, "SIGNATURE OF BEARER", SPECIMEN[1]])
        got = ocr.find(raw)
        self.assertTrue(got["valid"])
        self.assertEqual(got["lines"][1], SPECIMEN[1])
        self.assertIsNone(ocr.find("NO MACHINE READABLE ZONE HERE\n1234"))


class OcrReaderTestCase(unittest.TestCase):
    """OCR as Scan ID's reader ("Read with OCR"), Tesseract stood in for."""

    KEYS = ("full_name", "surname", "given_names", "date_of_birth", "sex", "nationality", "document_number", "date_of_expiry", "address")

    def test_the_fields_are_the_zones(self):
        zone = ocr.parse(*SPECIMEN)
        with mock.patch.object(ocr, "text", lambda data: "UTOPIA PASSPORT ERIKSSON"), mock.patch.object(ocr, "mrz", lambda data: zone):
            read, used = ocr.id_read(b"img", self.KEYS)
        self.assertEqual((read["fields"]["full_name"], read["fields"]["date_of_birth"], read["fields"]["document_number"], read["fields"]["address"]),
                         ("ANNA MARIA ERIKSSON", "1974-08-12", "L898902C3", ""))
        self.assertEqual((read["document_type"], read["issuing_country"], read["mrz"], read["uncertain"]),
                         ("Passport", "UTO", list(SPECIMEN), []))
        self.assertEqual((used["model"], used["cost"], read["ocr_text"]), ("tesseract-ocr", 0.0, "UTOPIA PASSPORT ERIKSSON"))
        from fincrime.documents import id_scan
        checks = {c["label"]: c["state"] for c in id_scan.checks(read, today=__import__("datetime").date(2011, 1, 1))}
        self.assertEqual(checks["Photo"], "warn")                                  # OCR does not judge the photo
        self.assertEqual(checks["MRZ check digits (OCR's reading)"], "ok")
        self.assertNotIn("Printed fields against the MRZ", checks)                 # the fields are the zone's

    def test_no_zone_no_field(self):
        broken = ocr.parse(SPECIMEN[0], SPECIMEN[1][:9] + "4" + SPECIMEN[1][10:])
        with mock.patch.object(ocr, "text", lambda data: "DRIVER LICENSE"), mock.patch.object(ocr, "mrz", lambda data: broken):
            read, _ = ocr.id_read(b"img", self.KEYS)
        self.assertIn("document_number", read["uncertain"])                        # its check digit does not add up
        with mock.patch.object(ocr, "text", lambda data: "DRIVER LICENSE"), mock.patch.object(ocr, "mrz", lambda data: None):
            read, _ = ocr.id_read(b"img", self.KEYS)
        self.assertEqual((set(read["fields"].values()), read["mrz"]), ({""}, []))
        self.assertIn("no passport machine-readable zone", read["note"])


class OcrCheckTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        cls.book_before = fcc.CASE_BOOK
        fcc.CASE_BOOK = "file"
        cls.dir_before = os.environ.get("FCC_UPLOAD_DIR")
        os.environ["FCC_UPLOAD_DIR"] = tempfile.mkdtemp(prefix="fcc-ocr-uploads-")

    @classmethod
    def tearDownClass(cls):
        uploads.remove_all()
        if cls.dir_before is None:
            os.environ.pop("FCC_UPLOAD_DIR", None)
        else:
            os.environ["FCC_UPLOAD_DIR"] = cls.dir_before
        fcc.CASE_BOOK = cls.book_before
        StateStore.clear()
        fcc._load_seed()

    def setUp(self):
        StateStore.clear()
        uploads.remove_all()
        fcc._load_seed()

    def uploaded_case(self):
        """A clean auto-verified case with its drawn passport uploaded as a photo."""
        c = next(c for c in fcc.KYC_CASES if ai_agent.eligible(c) and c["screening"]["workflow"] == "auto-verified"
                 and not c.get("current_reviewer") and all(x["ok"] for x in ai_agent.checks(c, perfect_read(c))[0])
                 and not ai_agent.address_update(c, perfect_read(c)) and ocr.parse(*drawn_mrz(c))["valid"])
        image, info = uploads.prepare(jpeg(svg_to_png(fcc._render_document(c, case_documents.slot_row(c, "passport")))))
        case_documents.attach(c, {"passport": (image, info, "passport.jpg")}, "amara.iyer@straive.com")
        return c

    def run_with(self, case, mrz, available=True):
        alert, wc = fcc._linked(case)
        with mock.patch.object(ai_agent.ocr, "available", lambda: available), \
                mock.patch.object(ai_agent.ocr, "mrz", lambda data: mrz), \
                mock.patch.object(ai_agent.ocr, "text", lambda data: "PASSPORT  REPUBLIC"):
            return ai_agent.run_case(case, alert, wc, fcc._render_document, call=Stub(perfect_read(case)), sleep=lambda s: None)

    def row(self, rec):
        return next(x for x in rec["checks"] if x["check"] == ai_agent.MRZ_CHECK)

    def test_an_mrz_that_agrees_confirms_the_read(self):
        c = self.uploaded_case()
        rec = self.run_with(c, ocr.parse(*drawn_mrz(c)))
        self.assertTrue(self.row(rec)["ok"] and not self.row(rec).get("info"), self.row(rec))
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        self.assertEqual(rec["ocr"]["text"]["passport"], "PASSPORT  REPUBLIC")
        page_c = fcc.app.test_client()
        page_c.post("/login", data={"analyst": "amara.iyer", "password": "1234"})
        page = page_c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        for needle in ('id="ai-agent-ocr"', 'id="ai-agent-mrz"', "Check digits add up", ai_agent.MRZ_CHECK):
            self.assertIn(needle, page)

    def test_an_mrz_that_differs_sends_the_case_to_a_person(self):
        c = self.uploaded_case()
        m = ocr.parse(*drawn_mrz(c))
        m["fields"]["dob"] = "1950-01-01"                                 # OCR's (valid) reading says otherwise
        rec = self.run_with(c, m)
        self.assertEqual(rec["decision"], "human")
        self.assertTrue(any("The MRZ read by OCR differs from the read (date of birth: MRZ 1950-01-01" in r for r in rec["reasons"]),
                        rec["reasons"])

    def test_look_alikes_or_an_unreadable_mrz_are_information_only(self):
        c = self.uploaded_case()
        m = ocr.parse(*drawn_mrz(c))
        number = m["fields"]["number"]
        m["fields"]["number"] = number.replace("0", "O").replace("1", "I").replace("5", "S").replace("8", "B")
        if m["fields"]["number"] == number:
            m["fields"]["number"] = "S" + number[1:] if number[0] == "5" else number[:-1] + {"0": "O", "1": "I"}.get(number[-1], number[-1])
        rec = self.run_with(c, m)
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        if m["fields"]["number"] != number:
            self.assertIn("look-alike characters", self.row(rec)["note"])
        c2 = self.uploaded_case()
        rec = self.run_with(c2, None)
        self.assertTrue(self.row(rec)["info"])
        self.assertEqual(rec["decision"], "verified", rec["reasons"])

    def test_an_unreadable_mrz_can_be_required(self):
        c = self.uploaded_case()
        with mock.patch.dict(fcc.SOP_SETTINGS["values"], {"ocr_mrz_required": True}):        # the SOP rules page
            rec = self.run_with(c, None)
        self.assertEqual(rec["decision"], "human")
        self.assertIn("OCR found no machine-readable zone on the passport", rec["reasons"])

    def test_without_tesseract_nothing_is_blocked(self):
        c = self.uploaded_case()
        rec = self.run_with(c, None, available=False)
        self.assertEqual(rec["ocr"], {"available": False})
        self.assertTrue(self.row(rec)["info"])
        self.assertEqual(rec["decision"], "verified", rec["reasons"])

    def test_a_drawn_document_is_not_ocrd(self):
        c = next(c for c in fcc.KYC_CASES if ai_agent.eligible(c))
        self.assertIsNone(ai_agent.read_ocr(c))

    @unittest.skipUnless(ocr.available(), "Tesseract is not installed on this machine")
    def test_tesseract_reads_a_drawn_passport_photo(self):
        c = fcc.KYC_BY_ID["KYC-2026-3011"]
        got = ocr.mrz(jpeg(svg_to_png(fcc._render_document(c, case_documents.slot_row(c, "passport")))))
        self.assertTrue(got and got["valid"], got)
        self.assertEqual(got["lines"][1], drawn_mrz(c)[1])
        self.assertEqual(got["fields"]["number"], c["client"]["id_number"])


if __name__ == "__main__":
    unittest.main()
