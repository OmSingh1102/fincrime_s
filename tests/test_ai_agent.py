"""The AI Agent on the file book: Claude's read of the ID and the proof of
address (stood in for here, so no call leaves the machine), the fixed checks,
and what the agent does with the case.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_ai_agent
"""
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-agent-"), "state.db")
os.environ.setdefault("FCC_STATE_DB", _DB)
os.environ["CONSOLE_CASES"] = "demo"      # the suite's default; this class switches to the file book itself
from fincrime.data.state_store import StateStore  # noqa: E402
if StateStore.path != os.environ["FCC_STATE_DB"]:
    StateStore.path = os.environ["FCC_STATE_DB"]
import app as fcc  # noqa: E402
from fincrime.data import recon_book  # noqa: E402
from fincrime.documents.raster import svg_to_png  # noqa: E402
from fincrime.documents.svg_cards import ISO3  # noqa: E402
from fincrime.engines import ai_agent, ai_screening, routing, sla  # noqa: E402
from fincrime.engines.errors import WorkflowError  # noqa: E402


def perfect_read(case, **id_overrides):
    """What the document images print, as a flawless reader would return it."""
    v = recon_book.view(case["recon"]["id"])
    pp, s = v["documents"]["passport"], v["synth"]
    idr = {"readable": True, "problems": [], "surname": pp["viz"]["surname"], "given_names": pp["viz"]["given"],
           "dob": v["customer"]["dob"], "nationality_code": ISO3.get(case["client"]["nationality"], ""),
           "document_number": pp["number"], "expiry": s["passport_expiry"], "address": "", "issued_on": s["passport_issued"],
           "note": "", "edges_visible": True, "mrz_visible": True}
    idr.update(id_overrides)
    holder = v["identity"].get("name", {}).get("canonical", v["customer"]["name"])     # as the proof of address prints it
    poa = {"readable": True, "problems": [], "surname": holder, "given_names": "", "dob": "", "nationality_code": "",
           "document_number": "", "expiry": "", "address": v["documents"]["poa"]["address"], "issued_on": s["poa_issued_on"],
           "note": ""}
    back = {"readable": True, "endorsements": "NONE", "issuing_authority": "PASSPORT OFFICE", "note": ""}
    sec = v["documents"]["secondary"]
    secondary = {"readable": True, "problems": [], "document_type": sec["type"], "name": sec["name"] or holder,
                 "dob": v["customer"]["dob"], "document_number": s["id_number"], "expiry": "",
                 "address": sec["address"]["text"] if sec["address"]["printed"] else "", "note": ""}
    return {"id": idr, "back": back, "secondary": secondary, "poa": poa}


VERIFY = {"decision": "verify", "confidence": 93, "reason": "Clear passport; every detail matches the record."}


class Stub:
    """Stands in for llm.call: records what it was sent, answers `answer` to
    the read and `verdict` to the verdict call."""

    def __init__(self, answer=None, error=None, verdict=None):
        self.answer, self.error, self.verdict, self.sent = answer, error, verdict or VERIFY, []

    def __call__(self, system, content, schema, max_tokens=3000):
        self.sent.append(content)
        if self.error:
            raise self.error
        if schema is ai_agent.VERDICT_SCHEMA:
            return self.verdict, {"input": 900, "output": 60, "cached": 0}, 3.0
        return self.answer, {"input": 3000, "output": 200, "cached": 0}, 8.5


class AiAgentTestCase(unittest.TestCase):

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
        self.K = fcc.KYC_BY_ID

    def run_agent(self, case, answer=None, error=None, verdict=None, **id_overrides):
        stub = Stub(answer or perfect_read(case, **id_overrides), error, verdict)
        alert, wc = fcc._linked(case)
        return ai_agent.run_case(case, alert, wc, fcc._render_document, call=stub, sleep=lambda s: None), stub

    def pick(self, test):
        return next(c for c in fcc.KYC_CASES if ai_agent.eligible(c) and test(c))

    # ── the clean path ──────────────────────────────────────────────────────
    def test_a_clean_case_is_verified_with_no_person(self):
        c = self.clean(lambda c: c["screening"]["workflow"] == "auto-verified"
                       and not ai_agent.address_update(c, perfect_read(c)))      # its record's address needs no correction
        rec, stub = self.run_agent(c)
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        self.assertEqual((c["status"], c["closed_by"], c["previous_reviewer"]), ("Verified", "System User", "AI Agent"))
        self.assertEqual(c["confirmations"]["dob"]["confirmed_by"], "AI Agent")
        self.assertIn("Data confirmed by AI Agent", c["audit"][-1]["detail"])
        # every document: the passport's two pages, the secondary ID, the proof of address
        self.assertEqual([b["type"] for b in stub.sent[0]], ["text", "image"] * 4)
        self.assertEqual(stub.sent[0][1]["source"]["media_type"], "image/png")
        said = " ".join(b.get("text", "") for b in stub.sent[0])
        for needle in ("passport data page", "passport's back page", "secondary ID", "proof of address"):
            self.assertIn(needle, said)
        self.assertEqual(rec["read_version"], ai_agent.READ_VERSION)
        made = [x["check"] for x in rec["checks"]]
        for check in ("Passport back page", "Secondary ID readable", "Name on secondary ID", "Date of birth on secondary ID"):
            self.assertIn(check, made)

    def test_a_case_the_rules_held_goes_to_screening(self):
        c = self.K["KYC-2026-3012"]
        c["current_reviewer"] = ""                                  # a starter case; free it for the agent
        rec, _ = self.run_agent(c)
        self.assertEqual(rec["decision"], "screening", rec["reasons"])
        self.assertEqual((c["status"], c["screening"]["workflow"]), ("DataScreening", "human-in-the-loop"))
        self.assertEqual(fcc.ALERT_BY_ID[c["screening"]["alert_case_id"]]["alerts"][0]["outcome"], "Pending")

    # ── what sends a case to a person ───────────────────────────────────────
    def assert_human(self, case, needle, answer=None, **id_overrides):
        rec, _ = self.run_agent(case, answer, **id_overrides)
        self.assertEqual(rec["decision"], "human")
        self.assertEqual((case["status"], case["screening"]["workflow"]), ("DataConfirming", "human-in-the-loop"))
        self.assertTrue(any(needle in r for r in rec["reasons"]), rec["reasons"])
        self.assertEqual((case["audit"][-1]["actor"], case["audit"][-1]["action"]), ("AI Agent", "Sent to a person"))
        return rec

    def clean(self, also=lambda c: True):
        """A case every check passes on a perfect read."""
        return self.pick(lambda c: also(c) and all(x["ok"] for x in ai_agent.checks(c, perfect_read(c))[0]))

    def test_unreadable_id(self):
        self.assert_human(self.clean(), "ID could not be read: blurred", readable=False, problems=["blurred"])
        self.assert_human(self.clean(), "ID could not be read: cut off", problems=["cut_off"])

    def test_date_of_birth_missing_or_different(self):
        self.assert_human(self.clean(), "Date of birth missing on the ID", dob="")
        self.assert_human(self.clean(), "Date of birth on the ID 1999-01-02", dob="1999-01-02")

    def test_expired_id(self):
        self.assert_human(self.clean(), "ID expired on 2025-03-01", expiry="2025-03-01")

    def test_name_that_is_somebody_else(self):
        self.assert_human(self.clean(), "Name on the ID differs from the CRM", surname="OKONKWO", given_names="CHIDI")

    def test_vendor_declined_and_old_proof_of_address(self):
        declined = self.pick(lambda c: c["vendor_check"]["result"] == "Declined")
        self.assert_human(declined, "ID declined by the vendor")
        old = self.pick(lambda c: ai_agent.workflow.poa_failures(c))
        old["current_reviewer"] = "amara.iyer@straive.com"           # held by an analyst: theirs to ask, not the agent's
        self.assert_human(old, "Issued within the last 6 months")

    # ── what the agent asks the customer for by itself ──────────────────────
    def only_old_poa(self, c):
        """An auto-verified case nobody holds whose only failure on a perfect
        read is its proof of address's age."""
        found = ai_agent.checks(c, perfect_read(c))[0]
        return (c["screening"]["workflow"] == "auto-verified" and not c["current_reviewer"]
                and [x["check"] for x in found if not x["ok"]] == ["Proof of address checks"]
                and [f["label"] for f in ai_agent.workflow.poa_failures(c)] == [ai_agent.POA_TOO_OLD])

    def test_an_old_proof_of_address_alone_is_asked_for_again(self):
        c = self.pick(self.only_old_poa)
        rec, _ = self.run_agent(c)
        self.assertEqual((rec["decision"], rec["requested"]), ("resubmit", ai_agent.RESUBMIT_POA_OLD), rec["reasons"])
        self.assertIn(ai_agent.RESUBMIT_POA_OLD, ai_agent.vocab.RESUBMIT_POA_REASONS)       # the analysts' own reason
        self.assertTrue(any(ai_agent.POA_TOO_OLD in r for r in rec["reasons"]), rec["reasons"])
        self.assertEqual((c["status"], c["pending_reason"], c["screening"]["workflow"], c["current_reviewer"]),
                         ("ResubmitPOA", "Pending Resubmission", "auto-verified", ""))
        self.assertEqual((c["resubmit"]["decisions"], c["resubmit"]["poa_reason"], c["resubmit"]["by"]),
                         (["Resubmit POA"], ai_agent.RESUBMIT_POA_OLD, "AI Agent"))
        self.assertEqual((c["audit"][-1]["actor"], c["audit"][-1]["action"]), ("AI Agent", "Resubmission requested"))
        self.assertNotIn("Sent to a person", [e["action"] for e in c["audit"]])
        self.assertFalse(ai_agent.eligible(c))                           # it waits for the customer's new document
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-requested"', page)
        self.assertIn("Asked the customer", page)
        self.assertIn(f'data-case-id="{c["id"]}"', self.c.get("/kyc/list?agent=resubmit").data.decode("utf-8"))
        self.assertIn('id="ai-agent-resubmit-kpi"', self.c.get("/ai-screening").data.decode("utf-8"))

    def test_what_the_agent_never_asks_the_customer_for(self):
        # the same old proof of address beside any other failure: a person
        c = self.pick(self.only_old_poa)
        self.assert_human(c, "Date of birth missing on the ID", dob="")
        # an unreadable or covered ID: a person sees what it hides
        c = self.pick(self.only_old_poa)
        self.assert_human(c, "ID could not be read", problems=["covered"])
        # an address that differs: the documents may be right and the record old
        c = self.clean(lambda c: c["screening"]["workflow"] == "auto-verified" and not c["current_reviewer"])
        read = perfect_read(c)
        read["poa"]["address"] = "PO Box 4500, Atlanta, GA 30309"
        rec = self.assert_human(c, "Address on the proof of address does not match", answer=read)
        self.assertEqual(rec["resubmit"], "")
        # an old proof of address on a case with a watchlist alert left: a person
        c = self.K["KYC-2026-3276"]
        rec = self.assert_human(c, "Issued within the last 6 months", answer=self.old_read(c))
        self.assertTrue(rec["watchlist"])

    # ── the record's own slips ──────────────────────────────────────────────
    def free(self, case_id):
        c = self.K[case_id]
        c["current_reviewer"] = ""
        return c

    def assert_corrected(self, c, field, before, after, why):
        rec, _ = self.run_agent(c)
        self.assertEqual((rec["decision"], c["status"]), ("verified", "Verified"), rec["reasons"])
        self.assertEqual(c["client"][field], after)
        fixed = rec["name_corrected" if field == "legal_name" else "address_updated"]
        self.assertEqual((fixed["from"], fixed["to"]), (before, after))
        self.assertIn(why, " ".join(e["detail"] for e in c["audit"] if e["actor"] == "AI Agent"))
        self.assertIn(("AI Agent", "name" if field == "legal_name" else "address", before, after),
                      [(e["by"], e["field"], e["from"], e["to"]) for e in c["change_log"]])
        self.assertNotIn("verdict", rec)                   # Claude is asked again, on the corrected record
        return rec

    def test_a_record_that_named_the_father_is_corrected(self):
        c = self.free("KYC-2026-3052")                     # passport MORE, MANASI PRAKASH; the record PRAKASH MORE
        rec = self.assert_corrected(c, "legal_name", "PRAKASH MORE", "MANASI PRAKASH MORE", "father's name PRAKASH")
        self.assertIn("dropped the holder's own name MANASI", rec["name_corrected"]["why"])
        self.assertIn(("AI Agent", "Name corrected"), [(e["actor"], e["action"]) for e in c["audit"]])
        self.assertEqual(c["screening"]["rows"][-1]["user_name"], "MANASI PRAKASH MORE")    # screened on her own name
        self.assertTrue(ai_agent.needs_verdict(c))
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-name"', page)
        self.assertIn("MANASI PRAKASH MORE", page)

    def test_a_married_surname_on_the_form_and_a_po_box_are_corrected(self):
        self.assert_corrected(self.free("KYC-2026-3033"), "legal_name", "NHUNG VU", "PHAN BICH NHUNG",
                              "the surname VU, which none of the documents prints")
        self.assert_corrected(self.free("KYC-2026-3189"), "address", "PO Box 2021, Chicago, IL 60614",
                              "8391 West Devon Avenue, Apartment 5A, Chicago, IL 60614", "only a PO Box")

    def assert_rfi(self, c, needle, answer=None):
        rec, _ = self.run_agent(c, answer)
        self.assertEqual((rec["decision"], c["status"], c["pending_reason"]), ("rfi", "DataConfirming", "RFI Sent"),
                         rec["reasons"])
        self.assertIn(needle, rec["requested"])
        self.assertEqual((c["rfi"][-1]["by"], c["rfi"][-1]["message"]), ("AI Agent", rec["requested"]))
        self.assertEqual(c["screening"]["workflow"], "auto-verified")
        # still open: shown as waiting on the customer, never as auto-verified (closed)
        self.assertEqual(ai_screening.shown_workflow(c), "waiting-on-customer")
        # it waits on the user: out of the analysts' queue, with the intake clock shown as waiting
        self.assertEqual(routing.level_for(c), "")
        self.assertEqual(sla.chip(c)["text"], "Waiting on user")
        return rec

    def test_the_document_that_links_two_names_is_asked_for(self):
        rec = self.assert_rfi(self.free("KYC-2026-3069"), "your marriage certificate")      # licence WILSON, passport ROBERTS
        self.assertIn("(PATRICIA MARIE ROBERTS) to the name on your account (PATRICIA WILSON)", rec["requested"])
        # MIKE NGUYEN for NGUYEN MINH MINH, with a licence for second ID: the certificate is not on the case
        c = self.free("KYC-2026-3259")
        ai_agent._secondary_doc(c)["name"] = "Driver's License (TX)"
        read = perfect_read(c)
        read["secondary"]["document_type"] = "DRIVER LICENSE"
        self.assert_rfi(c, "your naturalisation certificate", answer=read)
        c = self.free("KYC-2026-3074")                                                       # a second home in Scottsdale
        self.assert_rfi(c, "Please confirm your main home address.")
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-requested"', page)
        self.assertIn("Asked the customer (RFI)", page)
        self.assertIn("Waiting on customer", page)
        self.assertIn(f'data-case-id="{c["id"]}"', self.c.get("/kyc/list?agent=rfi").data.decode("utf-8"))
        # auto-verified means closed: the open case is listed as waiting on the customer, not as auto-verified
        mine = f"/kyc/list?case_id={c['id']}&workflow="
        self.assertIn('data-workflow="waiting-on-customer"', self.c.get(mine + "waiting-on-customer").data.decode("utf-8"))
        self.assertNotIn(f'data-case-id="{c["id"]}"', self.c.get(mine + "auto-verified").data.decode("utf-8"))
        self.assertEqual(ai_screening.shown_workflow(dict(c, status="Verified", pending_reason="")), "auto-verified")
        # an analyst who takes it holds it in the queue again
        self.c.post(f"/kyc/{c['id']}/reserve")
        self.assertEqual(routing.level_for(c), "L1")

    def test_a_second_surname_beside_the_passports_is_asked_for(self):
        # SEEMA VERMA PANDEY: the passport VERMA, the bill SEEMA VERMA, the Aadhaar SEEMA PANDEY. The record
        # keeping both surnames links nothing: the file's analysts obtained the marriage certificate.
        rec = self.assert_rfi(self.free("KYC-2026-3239"), "your marriage certificate")
        self.assertIn("(SEEMA VERMA) to the name on your Aadhaar (Seema Pandey)", rec["requested"])    # as the card prints it
        self.assertEqual(rec["reasons"], [ai_agent.SECOND_SURNAME])
        # beside a watchlist alert the agent asks nothing itself: a person does
        rec = self.assert_human(self.free("KYC-2026-3057"), ai_agent.SECOND_SURNAME)     # SUDESHNA SARKAR BOSE
        self.assertTrue(rec["watchlist"])

    def test_a_case_confirmed_beside_a_second_surname_is_decided_again(self):
        c = self.free("KYC-2026-3502")                                      # BHAVYA SHENOY GOWDA; Aadhaar BHAVYA GOWDA
        with mock.patch.object(ai_agent, "_second_surname", lambda *a: []):              # the checks before the fix
            rec, _ = self.run_agent(c)
        self.assertEqual((rec["decision"], c["client"]["legal_name"]), ("verified", "BHAVYA SHENOY"))   # confirmed as the passport's
        c["ai_agent"]["rules"] = 12
        StateStore.save("kyc", c)
        read_at = rec["at"]
        fcc._load_seed()
        c = fcc.KYC_BY_ID[c["id"]]
        rec = c["ai_agent"]
        self.assertEqual(rec["at"], read_at)                                # the stored read: no new look at the images
        self.assertEqual((rec["decision"], c["status"], c["pending_reason"]), ("rfi", "DataConfirming", "RFI Sent"))
        self.assertIn("(BHAVYA SHENOY) to the name on your Aadhaar (Bhavya Gowda)", rec["requested"])
        self.assertEqual(c["client"]["legal_name"], "BHAVYA SHENOY GOWDA")  # the record as the file gave it
        self.assertNotIn("verdict", rec)                                    # Claude is asked again, on today's policy
        self.assertTrue(ai_agent.needs_verdict(c))
        self.assertIn("marriage certificate that links the two", ai_agent.POLICY)

    def test_an_anglicised_name_beside_the_naturalisation_certificate_is_corrected(self):
        # KEVIN NGUYEN; the passport, the certificate (the secondary ID) and the bill: KHOA QUOC NGUYEN.
        # The file's analysts kept the legal name and Kevin as the preferred name, and asked for nothing.
        c = self.free("KYC-2026-3025")
        rec = self.assert_corrected(c, "legal_name", "KEVIN NGUYEN", "KHOA QUOC NGUYEN",
                                    "the naturalisation certificate on the case names the holder")
        self.assertEqual((c["client"]["preferred_name"], rec["name_corrected"]["preferred"]), ("KEVIN NGUYEN", "KEVIN NGUYEN"))
        self.assertIn(("AI Agent", "preferred name", "", "KEVIN NGUYEN"),
                      [(e["by"], e["field"], e["from"], e["to"]) for e in c["change_log"]])
        self.assertEqual((c["pending_reason"], c.get("rfi", [])), ("", []))                # nothing asked of the customer
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="preferred-name"', page)
        self.assertIn("Preferred name kept:</b> KEVIN NGUYEN", page)

    def test_what_the_record_cannot_settle_stays_with_a_person(self):
        # a spelling the documents themselves disagree on (MOHANTI, MAHANTY, MOHANTY)
        self.assert_human(self.free("KYC-2026-3255"), "Name on the ID differs from the CRM")
        # a married surname too close to a name the documents print to call (HOANG beside HAN)
        self.assert_human(self.free("KYC-2026-3141"), "Name on the ID differs from the CRM")
        # the same slip beside any other failure
        self.assert_human(self.free("KYC-2026-3033"), "Date of birth missing on the ID", dob="")
        self.assertEqual(self.K["KYC-2026-3033"]["client"]["legal_name"], "NHUNG VU")

    def test_gateway_error_goes_to_a_person(self):
        c = self.clean()
        rec, _ = self.run_agent(c, error=WorkflowError("The model gateway answered 500: busy"))
        self.assertEqual(rec["decision"], "human")
        self.assertIn("could not read the documents", rec["reasons"][0])
        self.assertEqual(c["status"], "DataConfirming")

    def test_rate_limit_waits_and_tries_again(self):
        for failure in ("The model gateway answered 429: too many connections", "Could not reach the model gateway."):
            c = self.clean(lambda c: "ai_agent" not in c)
            calls = []

            def flaky(system, content, schema, max_tokens=3000):
                if schema is ai_agent.VERDICT_SCHEMA:
                    return VERIFY, {}, 1.0
                calls.append(1)
                if len(calls) == 1:
                    raise WorkflowError(failure)
                return perfect_read(c), {}, 5.0
            alert, wc = fcc._linked(c)
            rec = ai_agent.run_case(c, alert, wc, fcc._render_document, call=flaky, sleep=lambda s: None)
            self.assertEqual(len(calls), 2, failure)
            self.assertNotEqual(rec["decision"], "human", rec["reasons"])

    # ── bad photos of the ID ────────────────────────────────────────────────
    def capture(self, kind):
        return self.pick(lambda c: recon_book.view(c["recon"]["id"])["synth"]["capture"] == kind)

    def test_a_phone_on_the_passport_passes_the_vendor_but_not_the_agent(self):
        c = self.capture("obstructed")
        self.assertEqual(c["vendor_check"]["result"], "Approved")                   # the vendor missed it, as on real uploads
        self.assertNotIn("Vendor rejected", c["manual_review_reason"])
        self.assertEqual(c["documents"][0]["quality"], "covered")
        drawings = recon_book.file_documents
        self.assertEqual(c["recon"]["drawn"], drawings.POA_REDRAWN if drawings.poa_wraps(recon_book.view(c["recon"]["id"]))
                         else drawings.REDRAWN)
        rec = self.assert_human(c, "ID could not be read: something covers part of it", problems=["covered"], dob="",
                                expiry="", mrz_visible=False)
        self.assertEqual(rec["resubmit"], ai_agent.RESUBMIT_UNCLEAR)
        self.assertIn(ai_agent.RESUBMIT_UNCLEAR, ai_agent.vocab.RESUBMIT_ID_REASONS)
        # the picture hides the dates: a reader that names no problem still cannot verify it
        rec = self.assert_human(self.capture("angled"), "Date of birth missing on the ID", dob="", expiry="")
        self.assertIn("Expiry date not readable", rec["reasons"])

    def test_a_slight_tilt_alone_stops_nothing(self):
        rec, _ = self.run_agent(self.clean(), problems=["angled"])
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        rec = self.assert_human(self.clean(), "something covers part of it, photographed at an angle", problems=["angled", "covered"])
        self.assertEqual(rec["resubmit"], ai_agent.RESUBMIT_UNCLEAR)

    def test_edges_and_mrz_out_of_shot(self):
        rec = self.assert_human(self.clean(), "its edges are not all in the picture", edges_visible=False)
        self.assertEqual(rec["resubmit"], ai_agent.RESUBMIT_CROPPED)
        rec = self.assert_human(self.clean(), "the MRZ is not fully visible", mrz_visible=False)
        self.assertEqual(rec["resubmit"], ai_agent.RESUBMIT_UNCLEAR)

    def test_the_suggested_resubmit_reason(self):
        for overrides, want in ((dict(problems=["damaged"]), ai_agent.RESUBMIT_DAMAGED),
                                (dict(readable=False, problems=["blurred", "too_dark"]), ai_agent.RESUBMIT_ILLEGIBLE),
                                (dict(problems=["angled", "covered"]), ai_agent.RESUBMIT_UNCLEAR),
                                (dict(problems=["covered", "angled"], edges_visible=False), ai_agent.RESUBMIT_UNCLEAR),
                                (dict(problems=["cut_off", "angled"], edges_visible=False), ai_agent.RESUBMIT_CROPPED),
                                (dict(expiry="2025-09-19"), ai_agent.RESUBMIT_EXPIRED),
                                (dict(surname="OKONKWO", given_names="CHIDI"), "")):   # not the photo: nothing to retake
            rec, _ = self.run_agent(self.clean(), **overrides)
            self.assertEqual((rec["decision"], rec["resubmit"]), ("human", want), overrides)
            self.assertTrue(not want or want in ai_agent.vocab.RESUBMIT_ID_REASONS)

    # ── Claude's own verdict, beside the rules ──────────────────────────────
    def test_the_verdict_sits_beside_the_rules_and_decides_nothing(self):
        c = self.clean()
        rec, stub = self.run_agent(c)
        self.assertEqual(rec["decision"], "verified")
        self.assertEqual((rec["verdict"]["decision"], rec["verdict"]["confidence"], rec["verdict"]["agrees"]), ("verify", 93, True))
        # the verdict call is text only and carries the read and the record, never the rules' result
        sent = stub.sent[1]
        self.assertEqual([b["type"] for b in sent], ["text"])
        self.assertIn(c["client"]["legal_name"], sent[0]["text"])
        self.assertIn('"readable": true', sent[0]["text"])
        self.assertNotIn("verified", sent[0]["text"].replace("Approved", ""))
        # the image read never sees the record: a hidden date of birth cannot be filled in from it
        self.assertNotIn(c["client"]["dob"], " ".join(b.get("text", "") for b in stub.sent[0]))
        # a verdict that differs is recorded as such and changes nothing
        other = self.clean()
        rec, _ = self.run_agent(other, verdict={"decision": "send_to_person", "confidence": 61, "reason": "Unsure."})
        self.assertEqual((rec["decision"], other["status"]), ("verified", "Verified"))
        self.assertFalse(rec["verdict"]["agrees"])
        # a person-bound case where Claude would verify also disagrees
        rec, _ = self.run_agent(self.clean(), dob="")
        self.assertEqual(rec["decision"], "human")
        self.assertFalse(rec["verdict"]["agrees"])

    def test_a_failed_or_odd_verdict_changes_nothing(self):
        c = self.clean()
        rec, _ = self.run_agent(c, verdict={"decision": "maybe", "confidence": 50, "reason": ""})
        self.assertEqual(rec["decision"], "verified")
        self.assertIn("not verify or send_to_person", rec["verdict"]["error"])
        self.assertNotIn("agrees", rec["verdict"])
        rec, _ = self.run_agent(self.clean(), error=WorkflowError("The model gateway answered 500: busy"))
        self.assertNotIn("verdict", rec)                                   # no read, nothing to judge

    def test_a_failed_verdict_is_asked_again_once_per_run_three_times_in_all(self):
        c = self.clean()
        rec, _ = self.run_agent(c)
        rec["verdict"] = {"error": "Could not reach the model gateway.", "policy": ai_agent.POLICY_VERSION}   # failed before tries counted
        status = c["status"]
        busy = Stub(error=WorkflowError("The model gateway answered 500: busy"))
        for run in (1, 2):
            self.assertTrue(ai_agent.needs_verdict(c))
            with mock.patch.object(ai_agent.llm, "call", busy), mock.patch.object(ai_agent, "eligible", lambda x: False):
                fcc.AI_AGENT.update(running=True, threads=[], busy=set())
                fcc._agent_worker()
            self.assertEqual(c["ai_agent"]["verdict"]["tries"], run + 1)          # one try per run
        self.assertFalse(ai_agent.needs_verdict(c))                               # three tries in all: no more
        self.assertEqual((len(busy.sent), c["status"]), (2, status))
        # a later success replaces the failure
        c["ai_agent"]["verdict"]["tries"] = 1
        with mock.patch.object(ai_agent.llm, "call", Stub()), mock.patch.object(ai_agent, "eligible", lambda x: False):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertEqual((c["ai_agent"]["verdict"]["decision"], c["ai_agent"]["verdict"]["agrees"]), ("verify", True))
        self.assertFalse(ai_agent.needs_verdict(c))

    def test_earlier_reads_get_a_verdict_from_the_stored_read(self):
        c = self.clean()
        self.run_agent(c)
        del c["ai_agent"]["verdict"]                                       # as read before verdicts existed
        status = c["status"]
        self.assertTrue(ai_agent.needs_verdict(c))
        stub = Stub(verdict={"decision": "verify", "confidence": 88, "reason": "Fine."})
        with mock.patch.object(ai_agent.llm, "call", stub), mock.patch.object(ai_agent, "eligible", lambda x: False):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertEqual((c["ai_agent"]["verdict"]["confidence"], c["ai_agent"]["verdict"]["agrees"]), (88, True))
        self.assertEqual(c["status"], status)
        self.assertEqual([[b["type"] for b in sent] for sent in stub.sent], [["text"]])   # no images sent again
        self.assertFalse(ai_agent.needs_verdict(c))
        # the AI Screening page counts it, the case list filters on it
        self.login("leah.fernandez")
        page = self.c.get("/ai-screening").data.decode("utf-8")
        self.assertIn('id="ai-verdict-row"', page)
        self.assertIn("agrees with the rules on 1 of 1 (100%)", page)
        self.login("amara.iyer")
        self.assertIn('id="ai-verdict"', self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8"))
        listing = self.c.get("/kyc/list?verdict=agree").data.decode("utf-8")
        self.assertIn(f'data-case-id="{c["id"]}"', listing)
        self.assertNotIn(f'data-case-id="{c["id"]}"', self.c.get("/kyc/list?verdict=disagree").data.decode("utf-8"))

    def test_a_verdict_under_an_older_policy_is_redone(self):
        # the policy carries Vietnam's 2025 mergers, from the address rules' own list
        c = self.clean()
        rec, stub = self.run_agent(c)
        self.assertIn("Bac Giang → Bac Ninh", stub.sent[1][0]["text"])
        self.assertIn("Binh Duong", ai_agent.POLICY)
        self.assertEqual(rec["verdict"]["policy"], ai_agent.POLICY_VERSION)
        self.assertFalse(ai_agent.needs_verdict(c))
        del rec["verdict"]["policy"]                                         # as given under the first policy
        self.assertTrue(ai_agent.needs_verdict(c))
        self.login("leah.fernandez")
        page = self.c.get("/ai-screening").data.decode("utf-8")
        self.assertIn("1 earlier read(s) waiting for a verdict", page)       # waiting, not counted as judged
        self.assertNotIn("agrees with the rules on", page)
        stub = Stub(verdict={"decision": "verify", "confidence": 91, "reason": "Fine."})
        with mock.patch.object(ai_agent.llm, "call", stub), mock.patch.object(ai_agent, "eligible", lambda x: False):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertEqual((rec["verdict"]["confidence"], rec["verdict"]["policy"]), (91, ai_agent.POLICY_VERSION))
        self.assertEqual([[b["type"] for b in sent] for sent in stub.sent], [["text"]])   # no images sent again
        self.assertIn("agrees with the rules on 1 of 1 (100%)", self.c.get("/ai-screening").data.decode("utf-8"))

    # ── an Aadhaar card has no age limit; older decisions are checked again ──
    def poa_kind(self, kind):
        return lambda c: (ai_agent.workflow.poa_docs(c) or [{}])[0].get("kind") == kind

    def old_read(self, case):
        read = perfect_read(case)
        read["poa"]["issued_on"] = "2024-10-14"                             # an Aadhaar card's date of issue
        return read

    def test_an_aadhaar_card_has_no_age_limit(self):
        card = self.clean(self.poa_kind("aadhaar"))
        rec, _ = self.run_agent(card, answer=self.old_read(card))
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        self.assertIn("except an Aadhaar card", ai_agent.POLICY)
        # the Data Confirm checks agree, whatever the upload's date
        other = self.clean(self.poa_kind("aadhaar"))
        ai_agent.workflow.poa_docs(other)[0]["issued_on"] = "2025-01-02"
        self.assertEqual(ai_agent.workflow.poa_failures(other), [])
        # a bill read as two years old is not verified: the customer is asked for a newer one
        bill = self.clean(lambda c: self.poa_kind("utility_bill")(c) and c["screening"]["workflow"] == "auto-verified"
                          and not c["current_reviewer"])
        rec, _ = self.run_agent(bill, answer=self.old_read(bill))
        self.assertEqual((rec["decision"], bill["status"]), ("resubmit", "ResubmitPOA"))
        self.assertTrue(any("Issued within the last 6 months" in r for r in rec["reasons"]), rec["reasons"])

    def test_an_unreadable_proof_of_address_date_goes_to_a_person(self):
        # the record carries a date, but the document's own date must be legible
        bill = self.clean(self.poa_kind("utility_bill"))
        read = perfect_read(bill)
        read["poa"]["issued_on"] = ""
        rec, _ = self.run_agent(bill, answer=read)
        self.assertEqual(rec["decision"], "human")
        self.assertTrue(any("Issue date readable: the date on the document could not be read" in r for r in rec["reasons"]),
                        rec["reasons"])
        # an Aadhaar card has no age limit, so its date does not matter
        card = self.clean(self.poa_kind("aadhaar"))
        read = perfect_read(card)
        read["poa"]["issued_on"] = ""
        self.assertEqual(self.run_agent(card, answer=read)[0]["decision"], "verified")

    def test_a_case_sent_to_a_person_under_older_checks_is_checked_again(self):
        card = self.clean(self.poa_kind("aadhaar"))
        touched = self.clean(lambda c: self.poa_kind("aadhaar")(c) and c["id"] != card["id"])
        bill = self.clean(lambda c: self.poa_kind("utility_bill")(c) and c["screening"]["workflow"] == "auto-verified"
                          and not c["current_reviewer"])
        with mock.patch.dict(fcc.SOP_SETTINGS["values"], {"poa_no_age_limit": []}), \
                mock.patch.object(ai_agent, "customer_request", lambda *a: ""):  # the checks before the fix
            for c in (card, touched, bill):
                self.assertEqual(self.run_agent(c, answer=self.old_read(c))[0]["decision"], "human")
                del c["ai_agent"]["rules"]                                    # decided under the first rules
        self.login("amara.iyer")
        self.c.post(f"/kyc/{touched['id']}/reserve")                         # a person has it now: left alone
        self.assertEqual([ai_agent.needs_recheck(c) for c in (card, touched, bill)], [True, False, True])
        stub = Stub()
        with mock.patch.object(ai_agent.llm, "call", stub), mock.patch.object(ai_agent, "eligible", lambda x: False):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertEqual(stub.sent, [])                                       # no model call, no images
        self.assertEqual((card["status"], card["ai_agent"]["decision"]), ("Verified", "verified"))
        self.assertIn(("AI Agent", "Checked again"), [(e["actor"], e["action"]) for e in card["audit"]])
        self.assertEqual((touched["status"], touched["ai_agent"]["decision"]), ("DataConfirming", "human"))
        # still not verified, checked once: its old proof of address, the only thing wrong, is asked for again
        self.assertEqual((bill["status"], bill["ai_agent"]["decision"], bill["ai_agent"]["rules"]),
                         ("ResubmitPOA", "resubmit", ai_agent.RULES_VERSION))
        self.assertEqual((bill["audit"][-1]["actor"], bill["audit"][-1]["action"]), ("AI Agent", "Resubmission requested"))
        self.assertFalse(ai_agent.needs_recheck(bill))

    def test_the_proof_of_address_is_in_the_customers_name(self):
        card = self.clean(self.poa_kind("aadhaar"))
        order = [c["check"] for c in ai_agent.checks(card, perfect_read(card))[0]]
        self.assertEqual(order[order.index("Proof of address readable") + 1], "Name on proof of address")
        poa_prompt = next(b["text"] for b in self.run_agent(card)[1].sent[0] if "proof of address" in b.get("text", ""))
        self.assertIn("An S/O, D/O, W/O or C/O line in an address names a relative", poa_prompt)
        self.assertEqual(card["ai_agent"]["decision"], "verified", card["ai_agent"]["reasons"])
        # the relative an Aadhaar's address names is not the holder
        other = self.clean(lambda c: self.poa_kind("aadhaar")(c) and c["id"] != card["id"])
        read = perfect_read(other)
        read["poa"]["surname"] = "RAKESH TIWARI"
        self.assert_human(other, "Name on the proof of address differs from the CRM", answer=read)
        bill = self.clean(self.poa_kind("utility_bill"))
        read = perfect_read(bill)
        read["poa"]["surname"] = ""
        self.assert_human(bill, "No holder name could be read on the proof of address", answer=read)
        v = ai_agent.poa_name_verdict
        self.assertTrue(v("SEEMA VERMA", {"surname": "Seema Verma Pandey", "given_names": ""}))        # a married surname added
        self.assertTrue(v("JEFF WILLIAMS", {"surname": "WILLIAMS", "given_names": "JEFFREY DEAN"}))   # nickname
        self.assertTrue(v("SAKSHI", {"surname": "SAKSHI", "given_names": ""}))                        # a single name
        self.assertFalse(v("PRIYA SHARMA", {"surname": "SHARMA", "given_names": ""}))                 # a surname alone is not her
        self.assertIsNone(v("PRIYA SHARMA", {"surname": "", "given_names": ""}))

    def test_a_read_without_the_proof_of_address_name_starts_again(self):
        unnamed, named = self.clean(self.poa_kind("utility_bill")), self.clean(self.poa_kind("aadhaar"))
        touched = self.clean(lambda c: self.poa_kind("utility_bill")(c) and c["id"] != unnamed["id"])
        for c in (unnamed, named, touched):
            self.assertEqual(self.run_agent(c)[0]["decision"], "verified", c["ai_agent"]["reasons"])
            c["ai_agent"]["rules"] = 3                                       # decided under the checks before the name
            if c is not named:
                c["ai_agent"]["read"]["poa"]["surname"] = ""                 # a read that was not asked for it
            StateStore.save("kyc", c)
        self.assertEqual([ai_agent.needs_reread(c) for c in (unnamed, named, touched)], [True, False, True])
        ai_agent.workflow._audit(touched, "amara.iyer", "Reopened")          # a person has worked on it since
        StateStore.save("kyc", touched)
        self.assertFalse(ai_agent.needs_reread(touched))
        fcc._load_seed()
        self.assertNotIn("ai_agent", fcc.KYC_BY_ID[unnamed["id"]])           # back to its seeded state, read again
        self.assertEqual(fcc.KYC_BY_ID[named["id"]]["ai_agent"]["decision"], "verified")
        self.assertEqual(fcc.KYC_BY_ID[touched["id"]]["ai_agent"]["decision"], "verified")

    # ── the name rules ──────────────────────────────────────────────────────
    def test_name_rules(self):
        v = ai_agent.name_verdict
        self.assertEqual(v("NGUYEN NGOC PHUONG", "NGUYỄN", "NGỌC PHƯƠNG"), (True, "NGUYEN NGOC PHUONG"))
        self.assertEqual(v("JEFF WILLIAMS", "WILLIAMS", "JEFFREY DEAN"), (True, "JEFFREY DEAN WILLIAMS"))   # nickname
        self.assertEqual(v("PILLAI NEETHU", "PILLAI", "NEETHU"), (True, "PILLAI NEETHU"))                    # order
        self.assertEqual(v("BALAJI K", "KRISHNAN", "BALAJI"), (True, "BALAJI KRISHNAN"))                     # patronymic initial
        self.assertEqual(v("PRABHU FNU", "", "PRABHU"), (True, "PRABHU"))                                    # single name
        self.assertEqual(v("VÅ© CÃ´ng KhÃ¡nh", "VŨ", "CÔNG KHÁNH")[0], True)                                # mojibake
        self.assertFalse(v("PATRICIA WILSON", "ROBERTS", "PATRICIA MARIE")[0])                              # another surname
        # an anglicised first name is no spelling of the passport's: the naturalisation certificate settles it (name_slip)
        self.assertFalse(v("KEVIN NGUYEN", "NGUYEN", "KHOA QUOC")[0])
        self.assertTrue(v("DAVID DUY NGUYEN", "NGUYEN", "DUY MINH", country="VN")[0])                       # the record keeps the passport's too
        self.assertFalse(v("BRANDON NGUYEN", "NGUYEN", "KHOA QUOC")[0])                                     # another given name
        # one word, one part of the name: HONG cannot be both the passport's HOÀNG and its HỒNG
        self.assertFalse(v("HONG NGUYEN", "HOÀNG", "PHƯƠNG HỒNG", country="VN")[0])
        self.assertTrue(v("RAHUL CHOUDHARY", "CHAUDHARY", "RAHUL", country="IN")[0])                       # a spelling of the surname still is

    def test_a_case_confirmed_on_a_word_used_twice_is_decided_again(self):
        c = self.free("KYC-2026-3896")                                      # HONG NGUYEN for the passport's HOÀNG, PHƯƠNG HỒNG
        with mock.patch.object(ai_agent, "name_verdict", lambda crm, s, g, filed="", country="": (True, crm)):
            self.assertIn(self.run_agent(c)[0]["decision"], ("verified", "screening"))   # the checks before the fix
        c["ai_agent"]["rules"] = 9
        StateStore.save("kyc", c)
        read_at = c["ai_agent"]["at"]
        fcc._load_seed()
        c = fcc.KYC_BY_ID[c["id"]]
        rec = c["ai_agent"]                                                 # back on its seed, decided again on the stored read
        self.assertEqual((rec["at"], rec["decision"], c["status"]), (read_at, "human", "DataConfirming"))
        self.assertIn("Name on the ID differs from the CRM", rec["reasons"])   # now its name goes to a person
        self.assertTrue(rec["watchlist"])
        self.assertEqual(c["client"]["legal_name"], "HONG NGUYEN")          # no slip corrected beside an alert

    def test_a_case_confirmed_on_an_anglicised_name_is_decided_again(self):
        c = self.free("KYC-2026-3025")                                      # KEVIN NGUYEN for the passport's NGUYEN, KHOA QUOC
        with mock.patch.object(ai_agent, "_given_match", ai_agent.names.given_match):     # the name rules before the fix
            rec, _ = self.run_agent(c)
        self.assertEqual((rec["decision"], c["client"]["legal_name"]), ("verified", "KHOA QUOC NGUYEN"))
        c["ai_agent"]["rules"] = 10
        StateStore.save("kyc", c)
        read_at = rec["at"]
        fcc._load_seed()
        c = fcc.KYC_BY_ID[c["id"]]
        rec = c["ai_agent"]
        self.assertEqual(rec["at"], read_at)                                # the stored read: no new look at the images
        # back on its seed (KEVIN NGUYEN), the naturalisation certificate on the case settles the name as a slip
        self.assertEqual((rec["decision"], c["status"]), ("verified", "Verified"))
        self.assertEqual(rec["name_corrected"]["from"], "KEVIN NGUYEN")
        self.assertEqual((c["client"]["legal_name"], c["client"]["preferred_name"]), ("KHOA QUOC NGUYEN", "KEVIN NGUYEN"))
        self.assertIn("Checked again", [e["action"] for e in c["audit"]])
        fcc._load_seed()                                                    # decided once: stored as such from then on
        self.assertEqual(fcc.KYC_BY_ID[c["id"]]["ai_agent"]["at"], read_at)
        self.assertEqual(fcc.KYC_BY_ID[c["id"]]["client"]["preferred_name"], "KEVIN NGUYEN")

    def test_a_case_asked_for_a_certificate_it_holds_is_decided_again(self):
        asked, married = self.free("KYC-2026-3172"), self.free("KYC-2026-3069")    # PHILIP for PHUC MINH; WILSON for ROBERTS
        worked = self.free("KYC-2026-3334")                                          # HELEN for HUONG THI, then a person's note
        with mock.patch.object(ai_agent, "_naturalisation_certificate", lambda case, got: False):   # the rules before the fix
            for c in (asked, married, worked):
                self.assertEqual(self.run_agent(c)[0]["decision"], "rfi", c["ai_agent"]["reasons"])
                c["ai_agent"]["rules"] = 11
        self.assertIn("naturalisation certificate", asked["ai_agent"]["requested"])
        # a read older than the case's intake rows (an RFI decided again on an earlier restart, on a fresh seed)
        asked["ai_agent"]["at"] = "2000-01-01 00:00:00"
        self.assertFalse(ai_agent._untouched(asked))
        ai_agent.workflow._audit(worked, "amara.iyer@straive.com", "Note added")
        for c in (asked, married, worked):
            StateStore.save("kyc", c)
        read_at, married_at, worked_at = asked["ai_agent"]["at"], married["ai_agent"]["at"], worked["ai_agent"]["at"]
        fcc._load_seed()
        w = fcc.KYC_BY_ID[worked["id"]]                                     # a person worked it: theirs, as stored
        self.assertEqual((w["ai_agent"]["at"], w["ai_agent"]["decision"], w["client"]["legal_name"]),
                         (worked_at, "rfi", "HELEN NGUYEN"))
        c = fcc.KYC_BY_ID[asked["id"]]
        rec = c["ai_agent"]
        self.assertEqual(rec["at"], read_at)                                # the stored read: no new look at the images
        self.assertEqual((rec["decision"], c["status"], c["pending_reason"], c.get("rfi", [])), ("verified", "Verified", "", []))
        self.assertEqual((c["client"]["legal_name"], c["client"]["preferred_name"]), ("PHUC MINH NGUYEN", "PHILIP NGUYEN"))
        self.assertIn("Checked again", [e["action"] for e in c["audit"]])
        # a marriage certificate the case does not hold is still asked for
        m = fcc.KYC_BY_ID[married["id"]]
        self.assertEqual((m["ai_agent"]["at"], m["ai_agent"]["decision"], m["pending_reason"]), (married_at, "rfi", "RFI Sent"))

    def test_a_postal_code_another_document_disputes_stays(self):
        c = self.K["KYC-2026-3473"]                                         # Calcutta 700019 on the record and the passport
        got = perfect_read(c)
        got["poa"]["address"] = got["poa"]["address"].replace("700019", "700089")    # one digit misread on the bill
        got["id"]["address"] = "FLAT NO 303 SUNSHINE ENCLAVE GARIAHAT ROAD BALLYGUNGE CALCUTTA PIN:700019, WEST BENGAL, INDIA"
        written = ai_agent.address_update(c, got)
        self.assertIn("Kolkata", written)                                   # the city's current name is still written
        self.assertTrue(written.endswith("700019"), written)                # the record's PIN, which the passport prints
        got["id"]["address"] = ""                                           # nothing disputes the bill: its code governs
        self.assertTrue(ai_agent.address_update(c, got).endswith("700089"))

    def test_a_record_that_keeps_the_fathers_name_is_not_the_holder(self):
        # an Indian passport's second given name is the father's: a record
        # that keeps only it names the father (the file's "Father's name as
        # middle name"); one that drops it keeps the holder
        v = ai_agent.name_verdict
        self.assertFalse(v("PRAKASH MORE", "MORE", "MANASI PRAKASH", country="IN")[0])
        self.assertTrue(v("HITESH BHATT", "BHATT", "HITESH DINESHBHAI", country="IN")[0])
        self.assertTrue(v("M PRAKASH MORE", "MORE", "MANASI PRAKASH", country="IN")[0])                    # her initial kept
        # not held to it: a Vietnamese personal name (its last), a longer name, a name often dropped
        self.assertTrue(v("NGUYEN MINHKHANG", "NGUYỄN", "VĂN MINH KHANG", country="VN")[0])
        self.assertTrue(v("PHUONG NGUYEN", "NGUYỄN", "NGỌC PHƯƠNG", country="VN")[0])
        self.assertTrue(v("DURGA BANDARU", "BANDARU", "VENKATA LAKSHMI DURGA", country="IN")[0])
        self.assertTrue(v("SALMAN PATHAN", "PATHAN", "MOHAMMED SALMAN", country="IN")[0])
        # held by an analyst, the book's case is theirs, on the ID and on the proof of address alike
        # (nobody holding it, the agent corrects the record: test_a_record_that_named_the_father_is_corrected)
        c = self.K["KYC-2026-3052"]
        c["current_reviewer"] = "amara.iyer@straive.com"
        rec = self.assert_human(c, "Name on the ID differs from the CRM")
        self.assertIn("Name on the proof of address differs from the CRM", rec["reasons"])
        self.assertEqual(c["client"]["legal_name"], "PRAKASH MORE")

    def test_a_case_confirmed_on_the_fathers_name_is_decided_again(self):
        father, kept = self.K["KYC-2026-3052"], self.clean()
        with mock.patch.object(ai_agent, "_own_name_kept", lambda *a, **k: True):   # the checks before the fix
            for c in (father, kept):
                self.assertEqual(self.run_agent(c)[0]["decision"], "verified", c["ai_agent"]["reasons"])
                c["ai_agent"]["rules"] = 4
                StateStore.save("kyc", c)
        self.assertEqual(father["client"]["legal_name"], "MANASI PRAKASH MORE")            # the confirmation rewrote it
        kept_at = kept["ai_agent"]["at"]
        fcc._load_seed()
        again = fcc.KYC_BY_ID[father["id"]]
        # back on its seed (PRAKASH MORE), decided again on the stored read: today's
        # checks correct the father's name as a slip, and confirm
        self.assertIn("Checked again", [e["action"] for e in again["audit"]])
        self.assertEqual(again["ai_agent"]["name_corrected"]["from"], "PRAKASH MORE")
        self.assertEqual((again["ai_agent"]["decision"], again["client"]["legal_name"]), ("verified", "MANASI PRAKASH MORE"))
        self.assertEqual(fcc.KYC_BY_ID[kept["id"]]["ai_agent"]["at"], kept_at)             # the rest is kept as stored

    # ── the verified address written back ──────────────────────────────────
    def test_the_verified_address_corrects_the_record(self):
        # a PIN keyed wrong: corrected in the record's own words
        c = self.K["KYC-2026-3014"]
        self.assertTrue(c["client"]["address"].endswith("Uttar Pradesh 226001"))
        rec, _ = self.run_agent(c)
        self.assertEqual(rec["decision"], "verified", rec["reasons"])
        self.assertTrue(c["client"]["address"].endswith("Uttar Pradesh 226010"), c["client"]["address"])
        self.assertEqual(rec["address_updated"]["from"][-6:], "226001")
        self.assertEqual((c["change_log"][-1]["by"], c["change_log"][-1]["field"]), ("AI Agent", "address"))
        self.assertIn(("AI Agent", "Address updated"), [(e["actor"], e["action"]) for e in c["audit"]])
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-address"', page)
        # a city under its old name: the document's address, without the relative's line
        c = self.K["KYC-2026-3032"]                                           # Madras; the Aadhaar reads S/O: Krishnan, ... Chennai
        self.assertIn("Madras", c["client"]["address"])
        self.assertEqual(self.run_agent(c)[0]["decision"], "verified")
        self.assertIn("Chennai", c["client"]["address"])
        self.assertNotIn("S/O", c["client"]["address"])
        # an address written another way only is left as it is
        same = self.clean(lambda x: x["id"] not in ("KYC-2026-3014", "KYC-2026-3032")
                          and not ai_agent.address_update(x, perfect_read(x)))
        before = same["client"]["address"]
        self.assertEqual(self.run_agent(same)[0]["decision"], "verified")
        self.assertEqual((same["client"]["address"], same["ai_agent"].get("address_updated")), (before, None))

    def test_an_address_confirmed_before_is_corrected_with_no_model_call(self):
        c = self.K["KYC-2026-3014"]
        with mock.patch.object(ai_agent, "address_update", lambda case, got: ""):    # confirmed before the write-back
            self.run_agent(c)
        c["ai_agent"]["rules"] = 4
        self.assertTrue(ai_agent.needs_address_update(c))
        stub = Stub()
        with mock.patch.object(ai_agent.llm, "call", stub), mock.patch.object(ai_agent, "eligible", lambda x: False):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertEqual(stub.sent, [])                                       # no model call, no images
        self.assertTrue(c["client"]["address"].endswith("226010"))
        self.assertEqual(c["ai_agent"]["rules"], ai_agent.RULES_VERSION)
        self.assertFalse(ai_agent.needs_address_update(c))

    def test_a_garbled_record_name_repairs_from_the_files_text(self):
        # the console keeps the record's name in capitals, and capitals change
        # the bytes a garbled name stands for: it repairs from the file's text
        v, filed = ai_agent.name_verdict, "VÃµ Gia SÆ¡n"
        self.assertEqual(filed.upper(), "VÃΜ GIA SÆ¡N")
        self.assertFalse(v(filed.upper(), "VÕ", "GIA SƠN")[0])
        self.assertEqual(v(filed.upper(), "VÕ", "GIA SƠN", filed=filed), (True, "VO GIA SON"))
        self.assertFalse(v("NGUYEN VAN AN", "VÕ", "GIA SƠN", filed=filed)[0])        # an edited record: the file no longer applies
        # a byte the file itself lost stands for exactly one letter
        filed = "Phan Ngá»c Äáº¡t"
        self.assertEqual(v(filed.upper(), "PHAN", "NGỌC ĐẠT", filed=filed), (True, "PHAN NGOC DAT"))
        self.assertFalse(v(filed.upper(), "PHAN", "NGỌC TÂM", filed=filed)[0])
        self.assertFalse(v(filed.upper(), "PHAN", "NGỌC ĐẠT ANH", filed=filed)[0])
        # the agent finds the file's text for a case of the book
        c = self.K["KYC-2026-3353"]
        self.assertEqual(ai_agent._filed_name(c), "VÃµ Gia SÆ¡n")
        name = next(x for x in ai_agent.checks(c, perfect_read(c))[0] if x["check"] == "Name")
        self.assertTrue(name["ok"], name)

    def test_which_cases_the_agent_takes(self):
        self.assertTrue(ai_agent.eligible(self.K["KYC-2026-3011"]))      # auto-verified, even though Amara holds it
        self.assertFalse(ai_agent.eligible(self.K["KYC-2026-3012"]))     # an alert left, and a person holds it
        self.assertFalse(ai_agent.eligible(self.K["KYC-2026-3029"]))     # a company
        meg = self.K["KYC-2026-3276"]                                    # an alert left that nobody holds or has worked
        self.assertEqual((meg["screening"]["workflow"], meg["current_reviewer"]), ("human-in-the-loop", ""))
        self.assertTrue(ai_agent.eligible(meg))
        ai_agent.workflow._audit(meg, "nisha.rao@straive.com", "Status changed")      # once an analyst has acted on it
        self.assertFalse(ai_agent.eligible(meg))
        self.assertTrue(svg_to_png('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>').startswith(b"\x89PNG"))

    # ── every document: the passport's back page and the secondary ID ─────
    def test_the_secondary_id_must_name_the_same_person(self):
        c = self.clean()
        read = perfect_read(c)
        read["secondary"]["name"] = "OKONKWO CHIDI"
        rec, _ = self.run_agent(c, answer=read)
        self.assertEqual(rec["decision"], "human")
        self.assertIn("Name on the secondary ID matches neither the passport nor the CRM", rec["reasons"])
        other = self.clean()
        read = perfect_read(other)
        read["secondary"]["dob"] = "1999-01-02"
        rec, _ = self.run_agent(other, answer=read)
        self.assertIn(f"Date of birth on the secondary ID 1999-01-02, CRM {other['client']['dob']}", rec["reasons"])
        blurred = self.clean()
        read = perfect_read(blurred)
        read["secondary"].update(readable=False, problems=["blurred"])
        rec, _ = self.run_agent(blurred, answer=read)
        self.assertIn("Secondary ID could not be read: blurred", rec["reasons"])

    def test_a_second_document_may_write_the_name_another_way(self):
        named = ai_agent.holder_named                       # a one-line name against the passport's surname and given names
        self.assertTrue(named("GREGORY E DINAPOLI", "DI NAPOLI", "GREGORY EDWARD"))     # particles run together
        self.assertTrue(named("SUSAN K DE LUCA", "DELUCA", "SUSAN KAY"))
        self.assertTrue(named("CHRISTOPHER R DELACRUZ", "DE LA CRUZ", "CHRISTOPHER RAY"))
        self.assertTrue(named("Prabhu K", "", "PRABHU"))                                # a single name and an initial
        self.assertTrue(named("FARHEEN QUREISHI", "QURESHI", "FARHEEN", country="IN"))  # the name rules' spellings
        self.assertFalse(named("PATRICIA MARIE WILSON", "ROBERTS", "PATRICIA MARIE"))   # another surname
        self.assertFalse(named("PATRICIA MARIE JONES", "ROBERTS", "PATRICIA MARIE"))
        self.assertFalse(ai_agent.same_holder("OKONKWO CHIDI", "PRABHU"))
        # a licence in the married name the record keeps passes, and says so
        c = self.K["KYC-2026-3069"]
        found = {x["check"]: x for x in ai_agent.checks(c, perfect_read(c))[0]}
        self.assertFalse(found["Name"]["ok"])                                         # the passport keeps ROBERTS
        self.assertTrue(found["Name on secondary ID"]["ok"])
        self.assertIn("the record's name; the passport reads ROBERTS PATRICIA MARIE", found["Name on secondary ID"]["document"])
        # but not beside the passport's own surname: a record that keeps both surnames links nothing
        c = self.K["KYC-2026-3239"]                                                  # SEEMA VERMA PANDEY; Aadhaar SEEMA PANDEY
        found = {x["check"]: x for x in ai_agent.checks(c, perfect_read(c))[0]}
        self.assertTrue(found["Name"]["ok"])                                          # the passport's VERMA, SEEMA
        self.assertEqual((found["Name on secondary ID"]["ok"], found["Name on secondary ID"]["note"]),
                         (False, ai_agent.SECOND_SURNAME))

    def test_the_secondary_ids_address_informs_and_never_fails(self):
        c = self.clean(lambda c: perfect_read(c)["secondary"]["address"])
        read = perfect_read(c)
        read["secondary"]["address"] = "1 Nowhere Lane, Springfield"
        rec, _ = self.run_agent(c, answer=read)
        self.assertNotEqual(rec["decision"], "human", rec["reasons"])
        row = next(x for x in rec["checks"] if x["check"] == "Address on secondary ID")
        self.assertEqual((row["ok"], row["info"]), (True, True))
        self.assertIn("the proof of address is the address evidence", row["note"])

    def test_an_endorsement_on_the_passports_back_page_goes_to_a_person(self):
        c = self.clean()
        read = perfect_read(c)
        read["back"]["endorsements"] = "THE HOLDER IS ALSO KNOWN AS JOHN SMITH"
        rec, _ = self.run_agent(c, answer=read)
        self.assertIn("An endorsement on the passport's back page: THE HOLDER IS ALSO KNOWN AS JOHN SMITH", rec["reasons"])
        self.assertEqual(c["status"], "DataConfirming")

    def test_an_older_read_is_checked_as_it_was(self):
        c = self.clean()
        read = perfect_read(c)
        del read["back"], read["secondary"]                                  # a read of two documents
        made = [x["check"] for x in ai_agent.checks(c, read)[0]]
        self.assertNotIn("Secondary ID readable", made)
        self.assertNotIn("Passport back page", made)

    # ── an alert left: the agent confirms, the analyst clears the alert ────
    def test_an_open_alert_case_is_confirmed_with_the_watchlist_finding(self):
        c = self.K["KYC-2026-3276"]                          # Meg Wallace; OFAC lists WALLACE, Margaret Marie, 1965-08-09
        rec, stub = self.run_agent(c, verdict=dict(VERIFY, watchlist="same_person", watchlist_reason="Same name; DOB transposed."))
        w = rec["watchlist"]
        self.assertEqual(w["finding"], "likely_match")
        m = w["matches"][0]
        self.assertEqual((m["name_relation"], m["dob_relation"], m["nationality_relation"]), ("same", "swapped", True))
        self.assertIn("is the listed date with day and month swapped", m["detail"])
        self.assertIn("Watchlist finding", [e["action"] for e in c["audit"]])
        # the verdict call was told the open record; the alert itself is untouched
        self.assertIn("WALLACE, Margaret Marie", stub.sent[1][0]["text"])
        self.assertEqual(rec["verdict"]["watchlist"], "same_person")
        alert, wc = fcc._linked(c)
        self.assertEqual(wc["matches"][0]["resolution"]["status"], "Unresolved")
        self.assertNotEqual(c["status"], "Verified")
        if rec["decision"] == "screening":                    # the documents passed: the analyst has only the alert left
            self.assertEqual((c["status"], c["screening"]["workflow"]), ("DataScreening", "human-in-the-loop"))
        # the analyst sees it on the case, the list and the screening case
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-watchlist"', page)
        self.assertIn("Likely true match", page)
        self.assertIn('id="ai-verdict-watchlist"', page)
        listing = self.c.get("/kyc/list?watch=likely_match").data.decode("utf-8")
        self.assertIn(f'data-case-id="{c["id"]}"', listing)
        self.assertIn(">Likely match</span>", listing)
        screening_case = self.c.get(f"/name_screening/case/{alert['id']}").data.decode("utf-8")
        self.assertIn('id="ns-ai-watch"', screening_case)
        self.assertIn("Likely true match", screening_case)

    def test_the_documents_beside_a_watchlist_record(self):
        c = self.K["KYC-2026-3276"]
        read = perfect_read(c)
        wc = {"matches": [{"name": "WALLACE, Margaret Marie", "dobs": ["1965-09-08"], "citizenship": "United States",
                           "resolution": {"status": "Unresolved"}},
                          {"name": "WALLACE, Margaret Marie", "dobs": ["1965"], "resolution": {"status": "Unresolved"}},
                          {"name": "WALLACE, Margaret Marie", "dobs": ["1964–1966"], "resolution": {"status": "Unresolved"}},
                          {"name": "WALLACE, Margaret Marie", "dobs": ["1971-02-03"], "resolution": {"status": "Unresolved"}},
                          {"name": "OKONKWO, Chidi", "dobs": [], "resolution": {"status": "Unresolved"}},
                          {"name": "WALLACE, Margaret Marie", "dobs": ["1965-09-08"], "resolution": {"status": "False positive"}}]}
        found = ai_agent.watchlist_findings(c, wc, read)
        self.assertEqual([(m["dob_relation"], m["finding"]) for m in found["matches"]],
                         [("same", "likely_match"), ("same_year", "possible_match"), ("in_range", "possible_match"),
                          ("differs", "likely_different"), ("not_listed", "likely_different")])   # the resolved one is left out
        self.assertEqual(found["finding"], "likely_match")
        self.assertIsNone(ai_agent.watchlist_findings(c, {"matches": []}, read))

    def test_a_case_decided_on_two_documents_starts_again(self):
        decided = []
        for _ in range(2):
            c = self.clean(lambda c: not c["current_reviewer"])              # a verified case is no longer eligible: the next one
            self.run_agent(c)
            c["ai_agent"].pop("read_version")                                # as read before every document was
            StateStore.save("kyc", c)
            decided.append(c)
        old, touched = decided
        self.assertTrue(ai_agent.read_before_all_documents(old))
        ai_agent.workflow._audit(touched, "amara.iyer@straive.com", "Reopened")   # a person has worked on it since
        StateStore.save("kyc", touched)
        self.assertFalse(ai_agent.read_before_all_documents(touched))
        fcc._load_seed()
        self.assertNotIn("ai_agent", fcc.KYC_BY_ID[old["id"]])               # back to its seeded state, read again
        self.assertEqual(fcc.KYC_BY_ID[old["id"]]["status"], "DataConfirming")
        self.assertIn("ai_agent", fcc.KYC_BY_ID[touched["id"]])              # a person's case keeps its read


    # ── in the console ──────────────────────────────────────────────────────
    def login(self, key):
        self.c = fcc.app.test_client()
        r = self.c.post("/login", data={"analyst": key, "password": "1234"})
        self.assertEqual(r.status_code, 302)

    def test_the_agent_verifies_an_auto_verified_case_by_itself(self):
        self.login("amara.iyer")
        c = self.K["KYC-2026-3011"]                                     # auto-verified, reserved by Amara at intake
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertNotIn('id="ai-agent"', page)                          # no card, no button before the agent runs
        self.assertNotIn("Run AI agent", page)
        original = ai_agent.eligible
        with mock.patch.object(ai_agent, "eligible", lambda x: x is c and original(x)),                 mock.patch.object(ai_agent.llm, "call", Stub(perfect_read(c))):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertEqual((c["status"], c["closed_by"], c["previous_reviewer"]), ("Verified", "System User", "AI Agent"))
        self.assertTrue(all(w.get("to") for w in c.get("work", [])), c.get("work"))      # Amara's turn closed
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-decision"', page)
        self.assertIn("Handled by AI", page)
        self.assertNotIn("Run AI agent", page)
        self.assertIn(c["id"], self.c.get("/kyc/list?agent=verified").data.decode("utf-8"))

    def test_the_resubmit_dialog_arrives_filled_in(self):
        c = self.capture("obstructed")
        c["current_reviewer"] = ""
        self.run_agent(c, problems=["covered"], dob="", expiry="")
        self.login("amara.iyer")
        page = self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8")
        self.assertIn('id="ai-agent-resubmit"', page)
        self.assertIn('id="rs-suggested"', page)
        self.assertIn('value="Resubmit ID" onchange="rsToggle()" checked', page)
        self.assertIn(f"<option selected>{ai_agent.RESUBMIT_UNCLEAR}</option>", page)
        self.c.post(f"/kyc/{c['id']}/reserve")
        self.c.post(f"/kyc/{c['id']}/resubmit", data={"decision": "Resubmit ID", "id_reason": ai_agent.RESUBMIT_UNCLEAR})
        self.assertEqual((c["status"], c["resubmit"]["id_reason"]), ("ResubmitID", ai_agent.RESUBMIT_UNCLEAR))
        self.assertNotIn('id="ai-agent-resubmit"', self.c.get(f"/kyc/detail/{c['id']}").data.decode("utf-8"))

    def test_a_watchlist_record_lists_every_place_the_file_gives(self):
        c = self.K["KYC-2026-3887"]                                          # Singapore; Dhaka, Bangladesh
        alert, wc = fcc._linked(c)
        m = wc["matches"][0]
        self.assertEqual(m["country"], "Singapore; Bangladesh")
        self.assertEqual([(x["city"], x["country"]) for x in m["key_data"]["locations"]], [("", "Singapore"), ("Dhaka", "Bangladesh")])
        self.assertEqual(m["key_data"]["location"]["country"], "Singapore")                # the first stays the main one
        self.assertEqual(fcc._linked(self.K["KYC-2026-3014"])[1]["matches"][0]["country"], "India")   # one place: as before
        # a match stored when it kept only the first place is brought in line
        stale = copy.deepcopy(wc)
        stale["matches"][0]["country"] = "Singapore"
        stale["matches"][0]["key_data"].pop("locations")
        StateStore.save("wc", stale)
        fcc._load_seed()
        m = fcc.WC_BY_ID[wc["id"]]["matches"][0]
        self.assertEqual((m["country"], len(m["key_data"]["locations"])), ("Singapore; Bangladesh", 2))
        self.login("amara.iyer")
        page = self.c.get(f"/wc/{wc['id']}/match/{m['id']}").data.decode("utf-8")
        self.assertIn("Dhaka", page)
        self.assertIn("Singapore; Bangladesh", page)

    def test_an_old_proof_of_address_is_reported_once(self):
        bill = self.clean(self.poa_kind("utility_bill"))
        read = perfect_read(bill)
        read["poa"]["issued_on"] = ai_agent.workflow.poa_docs(bill)[0]["issued_on"] = "2025-12-01"   # the record and the document agree
        rec, _ = self.run_agent(bill, answer=read)
        note = next(r for r in rec["reasons"] if "Issued within the last 6 months" in r)
        self.assertEqual(note.count("Issued within the last 6 months"), 1, note)
        # a document whose own date is old where the record's is not: both are said
        other = self.clean(lambda c: self.poa_kind("utility_bill")(c) and c["id"] != bill["id"])
        rec, _ = self.run_agent(other, answer=self.old_read(other))
        self.assertIn("the document reads 2024-10-14", " ".join(rec["reasons"]))

    def test_a_stored_case_takes_its_sex_from_the_file(self):
        c = self.K["KYC-2026-3140"]                                          # Jennifer Smith
        alert, wc = fcc._linked(c)
        self.assertEqual((c["client"]["gender"], alert["screening_info"]["gender"], wc["gender"]), ("F", "Female", "Female"))
        stale = [copy.deepcopy(r) for r in (c, alert, wc)]                     # as stored under the coin toss
        stale[0]["client"]["gender"], stale[1]["screening_info"]["gender"], stale[2]["gender"] = "M", "Male", "Male"
        for kind, record in zip(("kyc", "alert", "wc"), stale):
            StateStore.save(kind, record)
        fcc._load_seed()
        c = fcc.KYC_BY_ID["KYC-2026-3140"]
        alert, wc = fcc._linked(c)
        self.assertEqual((c["client"]["gender"], alert["screening_info"]["gender"], wc["gender"]), ("F", "Female", "Female"))
        self.login("amara.iyer")
        self.assertIn("Female", self.c.get(f"/wc/{wc['id']}").data.decode("utf-8"))

    def test_a_case_stored_from_an_older_drawing_starts_again(self):
        c = self.capture("damaged")
        self.run_agent(c, problems=["damaged"])
        stale = dict(c, recon=dict(c["recon"], drawn=""))                   # as stored before the redraw
        StateStore.save("kyc", stale)
        kept = self.clean()
        self.run_agent(kept)
        StateStore.save("kyc", kept)
        fcc._load_seed()
        self.assertNotIn("ai_agent", fcc.KYC_BY_ID[c["id"]])                 # back to its seeded state, read again
        self.assertEqual(fcc.KYC_BY_ID[c["id"]]["status"], "DataConfirming")
        self.assertEqual(fcc.KYC_BY_ID[kept["id"]]["ai_agent"]["decision"], "verified")    # the rest is kept

    def test_the_worker_works_the_queue_and_stops(self):
        chosen = [c["id"] for c in fcc.KYC_CASES if ai_agent.eligible(c)][:3]
        original = ai_agent.eligible
        only = lambda c: c["id"] in chosen and original(c)                  # noqa: E731
        answers = [perfect_read(self.K[i]) for i in chosen]                  # the worker takes them in book order

        def call(system, content, schema, max_tokens=3000):
            if schema is ai_agent.VERDICT_SCHEMA:
                return VERIFY, {"input": 10, "output": 5}, 1.0
            return answers.pop(0), {"input": 10, "output": 5}, 1.0
        with mock.patch.object(ai_agent, "eligible", only), mock.patch.object(ai_agent.llm, "call", call):
            fcc.AI_AGENT.update(running=True, threads=[], busy=set())
            fcc._agent_worker()
        self.assertFalse(fcc.AI_AGENT["running"])
        self.assertEqual(fcc.AI_AGENT["stopped"], "Queue finished")
        self.assertTrue(all(self.K[i].get("ai_agent") for i in chosen))

    def test_start_and_pause_are_for_the_team_lead(self):
        self.login("amara.iyer")
        r = self.c.post("/ai-agent/start", follow_redirects=True)
        self.assertIn("That action needs QC access", r.data.decode("utf-8"))
        self.assertFalse(fcc.AI_AGENT["running"])
        self.login("leah.fernandez")
        with mock.patch.object(fcc, "_agent_worker", lambda: None), mock.patch.object(fcc.ai_review, "configured", lambda: True):
            self.c.post("/ai-agent/start")
            self.assertTrue(fcc.AI_AGENT["running"])
            page = self.c.get("/ai-screening").data.decode("utf-8")
            self.assertIn('id="ai-agent-card"', page)
            self.assertIn("Running", page)
            self.c.post("/ai-agent/pause")
        self.assertFalse(fcc.AI_AGENT["running"])
        self.assertIn("Paused by Leah Fernandez", self.c.get("/ai-screening").data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
