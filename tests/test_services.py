"""The services connected by API (fincrime/adapters/azure_docintel.py,
fincrime/adapters/didit.py): the requests they make and how their answers
are read. No network: each adapter's _send is stood in for.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_services
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fincrime.adapters import azure_docintel, didit  # noqa: E402
from fincrime.documents import id_scan  # noqa: E402

KEYS = [k for k, _ in id_scan.FIELDS]
AZ = dict(azure_docintel.DEFAULTS, endpoint="https://res.cognitiveservices.azure.com/", api_key="AZ-KEY")
DD = dict(didit.DEFAULTS, api_key="DD-KEY")


def _azure_result(confidence=0.99):
    f = lambda v, kind="valueString", c=0.99: {kind: v, "content": v, "confidence": c}   # noqa: E731
    return {"pages": [{}], "documents": [{"docType": "idDocument.passport", "confidence": 0.99, "fields": {
        "FirstName": f("MARGARET MARIE"), "LastName": f("WALLACE"), "DateOfBirth": f("1965-09-08", "valueDate"),
        "Sex": f("F"), "Nationality": f("USA", "valueCountryRegion"), "CountryRegion": f("USA", "valueCountryRegion"),
        "DocumentNumber": f("623984704", c=confidence), "DateOfExpiration": f("2027-10-01", "valueDate"),
        "MachineReadableZone": {"type": "object", "content": "P<USAWALLACE<<MARGARET<MARIE<<<<<<<<<<<<<<<<<<\n6239847045USA6509088F2710015<<<<<<<<<<<<<<04",
                                "confidence": 0.98}}}]}


class AzureTests(unittest.TestCase):

    def test_the_read_is_scan_ids(self):
        sent = []

        def send(req):
            sent.append(req)
            if req.get_method() == "POST":
                return 202, {"Operation-Location": "https://res/op/1"}, b""
            if len(sent) == 2:
                return 200, {}, json.dumps({"status": "running"}).encode()
            return 200, {}, json.dumps({"status": "succeeded", "analyzeResult": _azure_result(confidence=0.5)}).encode()
        with mock.patch.object(azure_docintel, "_send", send):
            read, used = azure_docintel.read(AZ, b"jpeg", KEYS, sleep=lambda s: None)
        post = sent[0]
        self.assertEqual(post.full_url, "https://res.cognitiveservices.azure.com/documentintelligence/documentModels/"
                                        "prebuilt-idDocument:analyze?api-version=2024-11-30")
        self.assertEqual(post.get_header("Ocp-apim-subscription-key"), "AZ-KEY")
        self.assertEqual(json.loads(post.data), {"base64Source": "anBlZw=="})
        self.assertEqual((read["document_type"], read["issuing_country"]), ("Passport", "USA"))
        self.assertEqual((read["fields"]["full_name"], read["fields"]["date_of_birth"], read["fields"]["date_of_expiry"]),
                         ("MARGARET MARIE WALLACE", "1965-09-08", "2027-10-01"))
        self.assertEqual(len(read["mrz"]), 2)
        self.assertEqual(read["uncertain"], ["document_number"])               # under the 0.8 floor
        self.assertEqual((used["model"], used["cost"]), ("azure-docintel", 0.01))
        self.assertEqual(set(read["fields"]), set(KEYS))
        self.assertTrue(id_scan.checks(read))                                    # Scan ID's checks run on it

    def test_refusals_are_said_plainly(self):
        with mock.patch.object(azure_docintel, "_send", lambda req: (401, {}, b"{}")):
            with self.assertRaisesRegex(azure_docintel.AzureError, "refused the key"):
                azure_docintel.analyze(AZ, b"x", sleep=lambda s: None)
        self.assertFalse(azure_docintel.configured(azure_docintel.DEFAULTS))
        empty = azure_docintel.to_read({"documents": []}, KEYS)
        self.assertEqual((empty["document_type"], empty["readable"]), ("Not an identity document", False))


class DiditTests(unittest.TestCase):

    def answer(self, status="Approved", warnings=()):
        return {"request_id": "R-1", "id_verification": {
            "status": status, "document_type": "Passport", "full_name": "MARGARET MARIE WALLACE", "date_of_birth": "1965-09-08",
            "document_number": "623984704", "issuing_state_name": "United States",
            "front_image_quality_score": {"overall_score": 81.0, "is_document_fully_visible": True},
            "warnings": list(warnings)}}

    def test_a_verification(self):
        sent = []

        def send(req):
            sent.append(req)
            return 200, json.dumps(self.answer("Declined", [{"risk": "PRINTED_COPY_DETECTED", "log_type": "error",
                                                              "short_description": "Printed copy detected"}])).encode()
        with mock.patch.object(didit, "_send", send):
            v = didit.verify(DD, b"\xff\xd8jpeg", "CUST-1")
        req = sent[0]
        self.assertEqual(req.full_url, "https://verification.didit.me/v3/id-verification/")
        self.assertEqual(req.get_header("X-api-key"), "DD-KEY")
        self.assertIn(b'name="front_image"; filename="id.jpg"', req.data)
        self.assertIn(b"\xff\xd8jpeg", req.data)
        self.assertIn(b"CUST-1", req.data)
        self.assertEqual((v["status"], v["warnings"][0]["declines"], v["cost"], v["quality"]["overall_score"]),
                         ("Declined", True, 0.2, 81.0))

    def test_a_sandbox_answer_is_kept_as_one(self):
        v = didit.summary({"request_id": "R", "id_verification": {"status": "Approved", "full_name": "Sandbox User",
                                                                 "document_number": "SANDBOX-0001"}}, 0.2)
        self.assertEqual((v["sandbox"], v["cost"]), (True, 0.0))                 # free, and marked
        self.assertFalse(didit.summary(self.answer(), 0.2)["sandbox"])

    def test_refusals_are_said_plainly(self):
        for code, words in ((403, "refused the call"), (401, "does not accept the API key"), (400, "could not use the image")):
            with mock.patch.object(didit, "_send", lambda req, code=code: (code, b'{"error": "x"}')):
                with self.assertRaisesRegex(didit.DiditError, words):
                    didit.verify(DD, b"x")
        self.assertFalse(didit.configured(didit.DEFAULTS))
        no_credit = b'{"error":"You don\'t have enough credits to perform this request. Please top up at https://business.didit.me"}'
        with mock.patch.object(didit, "_send", lambda req: (403, no_credit)):
            with self.assertRaisesRegex(didit.DiditError, "accepted the key, but the account has no credits"):
                didit.verify(DD, b"x")


if __name__ == "__main__":
    unittest.main()
