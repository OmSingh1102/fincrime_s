"""Real document images uploaded on a case (file book): each image checked
and cleaned, stored beside the state database, served only to the case's
tenant, read by the AI Agent in place of the drawing, kept through a
restart and removed by the demo reset. Every image here is made by Pillow
in memory: no real identity document is used.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_documents
"""
import base64
import csv
import io
import os
import re
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-documents-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from PIL import Image  # noqa: E402
from fincrime.documents import uploads  # noqa: E402
from fincrime.engines import ai_agent, case_documents  # noqa: E402
from fincrime.data import recon_book  # noqa: E402
from fincrime.filebook import documents as file_documents, loader as file_loader  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_ai_agent import Stub, perfect_read  # noqa: E402


def flash_text(response):
    return " | ".join(re.findall(r'role="status"[^>]*>\s*<span[^>]*>[^<]*</span>\s*<span>([^<]+)</span>',
                                 response.data.decode("utf-8")))


def photo(size=(400, 200), fmt="JPEG", orientation=None, colour="white"):
    """A plain test picture, with a camera make (and an orientation) in its EXIF."""
    im = Image.new("RGB", size, colour)
    out = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "TestCam"                     # Make
    if orientation:
        exif[0x0112] = orientation
    im.save(out, fmt, exif=exif.tobytes()) if fmt == "JPEG" else im.save(out, fmt)
    return out.getvalue()


class DocumentUploadTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        cls.book_before = fcc.CASE_BOOK
        fcc.CASE_BOOK = "file"
        cls.dir_before = os.environ.get("FCC_UPLOAD_DIR")
        os.environ["FCC_UPLOAD_DIR"] = tempfile.mkdtemp(prefix="fcc-uploads-")

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
        self.login("amara.iyer")
        self.started = []
        for target, value in ((fcc, "_agent_start"), (fcc.ai_review, "configured")):
            patcher = mock.patch.object(target, value, (lambda by: self.started.append(by)) if value == "_agent_start" else (lambda: True))
            patcher.start()
            self.addCleanup(patcher.stop)

    def login(self, key):
        self.c = fcc.app.test_client()
        self.assertEqual(self.c.post("/login", data={"analyst": key, "password": "1234"}).status_code, 302)

    def send(self, case, **files):
        data = {k: ((io.BytesIO(v[0]), v[1]) if k in uploads.SLOT_LABELS else v) for k, v in files.items()}
        return self.c.post(f"/kyc/{case['id']}/documents", data=data, content_type="multipart/form-data", follow_redirects=True)

    def clean_case(self):
        """An auto-verified individual the agent can take, that every check
        passes on a perfect read."""
        return next(c for c in fcc.KYC_CASES if ai_agent.eligible(c) and c["screening"]["workflow"] == "auto-verified"
                    and not c.get("current_reviewer")
                    and all(x["ok"] for x in ai_agent.checks(c, perfect_read(c))[0])
                    and not ai_agent.address_update(c, perfect_read(c)))

    # ── an image in its slot ────────────────────────────────────────────────
    def test_an_uploaded_photo_replaces_the_drawing(self):
        c = self.clean_case()
        r = self.send(c, passport=(photo(orientation=6), "C:\\Users\\x\\IMG_0042.JPG"))
        self.assertIn("Uploaded: Passport data page. The AI Agent is reading them now.", flash_text(r))
        self.assertEqual(self.started, ["documents"])
        row = case_documents.slot_row(c, "passport")
        self.assertEqual(row["id"], "d1")                                  # the drawn row's place and id
        self.assertNotIn("hub_file", row)
        up = row["upload"]
        self.assertEqual((up["media_type"], up["by"], up["original_name"]), ("image/jpeg", fcc.ANALYST_BY_KEY["amara.iyer"]["email"], "IMG_0042.JPG"))
        self.assertEqual((c["audit"][-1]["action"], c["vendor_check"]["result"]), (case_documents.UPLOADED, "Not run"))
        stored = os.path.join(uploads.base(), up["path"])
        self.assertTrue(stored.startswith(os.environ["FCC_UPLOAD_DIR"]) and os.path.exists(stored))
        with Image.open(stored) as im:
            self.assertEqual((im.format, im.size), ("JPEG", (200, 400)))  # turned upright from its EXIF orientation
            self.assertEqual(len(im.getexif()), 0)                         # the camera's EXIF block is gone
        served = self.c.get(f"/documents/{c['id']}/d1.svg")
        self.assertEqual((served.status_code, served.mimetype), (200, "image/jpeg"))
        self.assertEqual(served.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", served.headers["Cache-Control"])
        with open(stored, "rb") as fh:
            self.assertEqual(served.data, fh.read())
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        for needle in ('id="upload-docs-btn"', 'id="upload-docs-form"', 'data-uploaded="d1"', 'enctype="multipart/form-data"'):
            self.assertIn(needle, page)

    def test_the_agent_reads_the_uploaded_images(self):
        c = self.clean_case()
        read = perfect_read(c)
        self.send(c, passport=(photo(), "passport.jpg"), poa=(photo((300, 400), "PNG"), "bill.png"),
                  poa_kind=case_documents.slot_row(c, "poa")["kind"], poa_country=case_documents.slot_row(c, "poa")["country"])
        poa = case_documents.slot_row(c, "poa")
        self.assertEqual(poa["issued_on"], "")                             # no date typed in: the read gives it
        alert, wc = fcc._linked(c)
        stub = Stub(read)
        rec = ai_agent.run_case(c, alert, wc, fcc._render_document, call=stub, sleep=lambda s: None)
        images = [b for b in stub.sent[0] if b["type"] == "image"]
        self.assertEqual([b["source"]["media_type"] for b in images], ["image/jpeg", "image/png", "image/png", "image/jpeg"])
        self.assertEqual(base64.b64decode(images[0]["source"]["data"]), uploads.load(case_documents.slot_row(c, "passport")["upload"]["path"]))
        self.assertEqual((poa["issued_on"], poa["issued_on_source"]), (read["poa"]["issued_on"][:10], "read"))
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        self.assertEqual(c["status"], "Verified")

    def test_a_hit_case_stays_the_agents_and_a_resubmission_comes_back(self):
        hit = next(c for c in fcc.KYC_CASES if ai_agent.eligible(c) and c["screening"]["workflow"] == "human-in-the-loop")
        hit["ai_agent"] = {"decision": "human", "at": "2026-01-01 00:00:00", "read": {}}
        self.send(hit, secondary=(photo(), "id.jpg"))
        self.assertTrue(ai_agent.eligible(hit))                            # sending images in is not working the case
        self.assertNotIn("ai_agent", hit)
        self.assertEqual(hit["ai_agent_history"][-1]["decision"], "human")
        waiting = self.clean_case()
        waiting.update(status="ResubmitPOA", pending_reason="Pending Resubmission")
        self.send(waiting, poa=(photo(), "new-bill.jpg"), poa_kind="utility_bill", poa_issued_on="2026-10-01")
        self.assertEqual((waiting["status"], waiting["pending_reason"]), ("DataConfirming", ""))
        self.assertEqual([e["action"] for e in waiting["audit"][-2:]], [case_documents.UPLOADED, case_documents.RECEIVED])
        self.assertEqual((case_documents.slot_row(waiting, "poa")["issued_on"], case_documents.slot_row(waiting, "poa")["kind"]),
                         ("2026-10-01", "utility_bill"))

    # ── what is refused ─────────────────────────────────────────────────────
    def test_bad_files_and_cases_that_cannot_take_them_are_refused(self):
        c = self.clean_case()
        for blob, name, needle in ((b"just some text", "fake.jpg", "Passport data page: not an image this tool can read"),
                                   (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, "IMG_1.HEIC", "HEIC photo"),
                                   (photo(fmt="GIF"), "anim.gif", "not an image this tool can read"),
                                   (b"\xff" * (uploads.MAX_BYTES + 1), "huge.jpg", "larger than 10 MB")):
            self.assertIn(needle, flash_text(self.send(c, passport=(blob, name))), name)
        self.assertIn("Choose at least one image", flash_text(self.send(c)))
        self.assertIn("Choose the proof of address", flash_text(self.send(c, poa=(photo(), "b.jpg"), poa_kind="lottery")))
        self.assertFalse(case_documents.has_uploads(c))
        closed = next(x for x in fcc.KYC_CASES if x["tenant"] == "ttlive" and x["status"] == "DataConfirming" and x is not c
                      and not x.get("current_reviewer"))
        closed["status"] = "Verified"
        self.assertIn("The case is closed", flash_text(self.send(closed, passport=(photo(), "p.jpg"))))
        c["current_reviewer"] = fcc.ANALYST_BY_KEY["jon.miranda"]["email"]
        self.assertIn("Jon Miranda holds this case", flash_text(self.send(c, passport=(photo(), "p.jpg"))))
        c["current_reviewer"] = ""
        with mock.patch.dict(fcc.app.config, {"MAX_CONTENT_LENGTH": 2048}):
            self.assertIn("The upload is larger than", flash_text(self.send(c, passport=(bytes(5000), "p.jpg"))))

    def test_another_tenants_image_is_not_served(self):
        shop = next(x for x in fcc.KYC_CASES if x["tenant"] == "ttshop")
        row = shop["documents"][0]
        jpeg, info = uploads.prepare(photo())
        row["upload"] = dict(info, path=uploads.store(shop["id"], row["id"], jpeg), by="x", at="", original_name="")
        self.assertEqual(self.c.get(f"/documents/{shop['id']}/{row['id']}.svg").status_code, 404)    # Amara works Individuals only
        self.login("jon.miranda")
        self.assertEqual(self.c.get(f"/documents/{shop['id']}/{row['id']}.svg").mimetype, "image/jpeg")

    # ── kept, and removed ───────────────────────────────────────────────────
    def test_uploads_survive_a_restart_and_the_reset_removes_them(self):
        c = self.clean_case()
        self.send(c, passport=(photo(), "p.jpg"))
        path = os.path.join(uploads.base(), case_documents.slot_row(c, "passport")["upload"]["path"])
        stored = next(s for s in StateStore.records("kyc") if s["id"] == c["id"])
        stored["recon"]["drawn"] = "an older drawing"                     # would otherwise make the case start again
        StateStore.save("kyc", stored)
        fcc._load_seed()
        again = fcc.KYC_BY_ID[c["id"]]
        self.assertTrue(case_documents.slot_row(again, "passport").get("upload"))
        self.assertTrue(os.path.exists(path))
        self.c.post("/reset")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(case_documents.has_uploads(fcc.KYC_BY_ID[c["id"]]))


    # ── cases that come with their images ───────────────────────────────────
    def upload_csv(self, csv_bytes, images=None, name="new_cases.csv"):
        data = {"file": (io.BytesIO(csv_bytes), name)}
        if images is not None:
            data["images"] = (io.BytesIO(images), "images.zip")
        return self.c.post("/kyc/upload", data=data, content_type="multipart/form-data", follow_redirects=True)

    def sample_ids(self, data):
        return [r["kyc_case_id"] for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig")))]

    def test_a_case_without_document_columns_waits_for_its_images(self):
        data = self.c.get("/kyc/upload/sample.csv?images=1").data
        header = data.decode("utf-8-sig").splitlines()[0]
        self.assertNotIn("name_passport_data_page_viz", header)
        r = self.upload_csv(data)
        self.assertIn("4 case(s) wait for their images", flash_text(r))
        ids = self.sample_ids(data)
        c = fcc.KYC_BY_ID[ids[0]]
        self.assertTrue(c["awaiting_documents"])
        self.assertEqual((c["pending_reason"], c["vendor_check"]["result"]), ("Awaiting Documents", "Not run"))
        self.assertFalse(ai_agent.eligible(c))
        self.assertIn("Awaiting upload", self.c.get(f"/documents/{c['id']}/d1.svg").data.decode("utf-8"))
        self.send(c, passport=(photo(), "p.jpg"))
        self.assertTrue(c["awaiting_documents"])                           # the proof of address is still to come
        self.send(c, poa=(photo(), "bill.jpg"), poa_kind="bank_statement", poa_country=c["client"]["nationality"])
        self.assertNotIn("awaiting_documents", c)
        self.assertEqual(c["pending_reason"], "")
        self.assertEqual(c["audit"][-1]["action"], case_documents.RECEIVED)
        self.assertTrue(ai_agent.eligible(c))                              # the agent's to read now
        self.assertIsNone(ai_agent._secondary_doc(c))                      # an empty slot is no document
        fcc._load_seed()                                                   # a restart keeps it so
        again = fcc.KYC_BY_ID[c["id"]]
        self.assertNotIn("awaiting_documents", again)
        self.assertTrue(case_documents.slot_row(again, "poa")["upload"])

    def test_a_csv_with_a_zip_of_its_images(self):
        data = self.c.get("/kyc/upload/sample.csv?images=1").data
        full = {r["kyc_case_id"]: r for r in csv.DictReader(io.StringIO(self.c.get("/kyc/upload/sample.csv").data.decode("utf-8-sig")))}
        images = self.c.get("/kyc/upload/sample-images.zip")
        self.assertEqual(images.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(images.data)) as z:
            self.assertEqual(len(z.namelist()), 16)                        # four people x four documents; the company has none
        r = self.upload_csv(data, images.data)
        self.assertIn("16 image(s) from images.zip put on their cases", flash_text(r))
        self.assertEqual(self.started, ["upload"])
        ids = self.sample_ids(data)
        first = fcc.KYC_BY_ID[ids[0]]
        self.assertNotIn("awaiting_documents", first)
        self.assertTrue(all(d.get("upload") for d in first["documents"]))
        # what Claude reads off those images: what the full row prints (this upload's rows left it out)
        printed = file_documents.synthesise(file_loader.build_case(full[first["id"]], 0))
        with mock.patch.dict(recon_book.VIEWS, {first["recon"]["id"]: printed}):
            read = perfect_read(first)
        alert, wc = fcc._linked(first)
        with mock.patch.object(ai_agent.ocr, "available", lambda: False):
            rec = ai_agent.run_case(first, alert, wc, fcc._render_document, call=Stub(read), sleep=lambda s: None)
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        self.assertEqual(case_documents.slot_row(first, "poa")["issued_on_source"], "read")
        paths = [os.path.join(uploads.base(), d["upload"]["path"]) for d in first["documents"]]
        batch = StateStore.records("upload")[0]
        self.c.post(f"/kyc/upload/{batch['id']}/remove", follow_redirects=True)
        self.assertFalse(any(os.path.exists(p) for p in paths))            # removing the upload removes its images

    def test_a_zip_is_checked(self):
        data = self.c.get("/kyc/upload/sample.csv?images=1").data
        first = self.sample_ids(data)[0]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{first}_passport.jpg", photo())
            z.writestr(f"scans/{first.lower()}_PASSPORT.jpg", photo())        # same place again (any case, any folder)
            z.writestr("holiday.jpg", photo())
            z.writestr("KYC-2026-0001_poa.jpg", photo())                     # not a case of this upload
            z.writestr(f"{first}_poa.jpg", bytes(uploads.MAX_BYTES + 1))  # a bomb in miniature: refused before it is read
            z.writestr("__MACOSX/._x.jpg", b"junk")
        said = flash_text(self.upload_csv(data, buf.getvalue()))
        for needle in ("1 image(s) from images.zip put on their cases", "holiday.jpg: not named",
                       "a second image for the same place", "KYC-2026-0001: no case of that id", "larger than 10 MB"):
            self.assertIn(needle, said)
        self.assertNotIn("_x.jpg", said)
        self.assertIn("not a ZIP archive", flash_text(self.upload_csv(data, b"not a zip", name="again.csv")))
        with mock.patch.object(case_documents, "ZIP_MAX_FILES", 2):
            self.assertIn("one upload takes at most 2", flash_text(self.upload_csv(data, buf.getvalue(), name="again.csv")))


if __name__ == "__main__":
    unittest.main()
