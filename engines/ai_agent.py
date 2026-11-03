"""The AI Agent: it works an L1 Data Confirming case the way an analyst does,
and hands the case to a person whenever it cannot be sure.

1. Claude reads every document on the case from its image, in one call
   through Straive's LLM Foundry (fincrime/filebook/llm.call): the passport's
   data page and back page, the secondary ID and the proof of address - what
   is printed, whether it can be read, and what spoils the capture.
2. Fixed checks - code, not the model - compare that read with the case:
   readable, name, date of birth, nationality, ID number, expiry, the
   vendor's result, the passport's endorsements, the secondary ID (its name
   and date of birth), the proof of address and its address. On images
   uploaded in the console (not drawn ones), Tesseract's OCR of the
   passport's MRZ, read on this machine, is held against Claude's read too
   (read_ocr, _mrz_check).
3. Every check passes: the agent confirms the data through the analysts'
   own path (workflow.confirm_data) and the AI screening rules re-run. A
   case they leave nothing on verifies itself; one with a watchlist alert
   left goes to Data Screening, where an analyst clears the alert. Any check
   fails, or Claude cannot be reached: the case stays in Data Confirming,
   labelled human-in-the-loop, with the reasons and what the agent read.
   What the agent settles itself, when nothing else is wrong and nothing is
   left for an analyst: the record's own slips, where the documents agree
   with one another (the father's name kept for the holder's, a married
   surname on the form, an anglicised first name beside the naturalisation
   certificate, a PO Box for a home), it corrects and confirms
   (record_fixes); a proof of address older than six months, the document
   that links two names, or which home is the main one, it asks the
   customer for through the analysts' own Resubmit POA or RFI
   (customer_request). A request approves nothing; what comes back is
   checked again.
4. On a case with a watchlist alert left, the agent also sets what the
   documents say beside each open World-Check record (watchlist_findings):
   a likely true match, a possible one, or likely a different person. It
   informs the analyst; the rules and the analyst decide the alert.
5. Claude's own verdict, a second opinion beside the rules: a text-only call
   gets the read, the customer's record, the bank's document policy and any
   open watchlist record, and answers verify / send to a person with a
   confidence, and whether the documents show the customer is the listed
   person. It is shown and counted (does it agree with the rules?) but never
   acts on the case. It is a separate call so the record cannot leak into
   the read: a hidden date of birth must stay unread, not be filled in from
   the CRM.

It takes individuals in Data Confirming: those the rules cleared (the
auto-verified workflow), and those with an alert left that no person holds
or has worked. A company (KYB) stays with people. The result sits on the
case as case["ai_agent"].
"""
import base64
import json
import re
import time

from fincrime.data import vocab
from fincrime.documents.raster import svg_to_png
from fincrime.documents.svg_cards import ISO3
from fincrime.documents import ocr, uploads
from fincrime.engines import ai_screening, case_documents, sop, workflow
from fincrime.engines.errors import WorkflowError
from fincrime.filebook import addresses, llm, names, similarity
from fincrime.utils import dates

ACTOR = vocab.AI_AGENT
MODEL = llm.MODEL
ALPHA2 = {v: k for k, v in ISO3.items()}
SPOILERS = {"blurred": "blurred", "cut_off": "cut off", "glare": "glare", "covered": "something covers part of it",
            "angled": "photographed at an angle or from too far", "damaged": "the document is cracked or worn",
            "too_dark": "too dark"}
# A tilt is common on good photos (the model sometimes names one), so on its
# own it stops nothing; it is listed when something else on the ID is wrong.
SOFT = {"angled"}
FILLERS = {"FNU", "LNU", "NFN", "NLN", "-", "."}
RETRIES = 3           # the gateway allows few concurrent calls; a 429 or a dropped connection waits and tries again
RETRY_WAIT = 10.0

SYSTEM = ("You read identity documents for a KYC check at a bank. You are given images of a customer's documents. "
          "Report only what is printed and legible in the images. Never guess, infer or complete a value: if a field "
          "is not printed, cut off, blurred, hidden by glare or covered by something, return an empty string for it. "
          "Mark a document unreadable when any of name, date of birth, document number or expiry on an ID cannot be "
          "read with certainty. Name every capture problem you see: blurred; cut_off (part of the document is outside "
          "the picture); glare; covered (an object such as a phone, a finger or paper lies over part of it); angled "
          "(photographed at a steep angle or from so far away that the text is small; a slight tilt is normal, not a "
          "problem); damaged (cracked, torn, worn or faded); too_dark. "
          "Dates as YYYY-MM-DD.")

_DOC = {"type": "object", "additionalProperties": False, "properties": {
    "readable": {"type": "boolean"},
    "problems": {"type": "array", "items": {"type": "string", "enum": ["blurred", "cut_off", "glare", "covered", "angled",
                                                                      "damaged", "too_dark", "other"]}},
    "surname": {"type": "string", "description": "Surname as printed (on a proof of address: the holder's full name)"},
    "given_names": {"type": "string", "description": "Given names as printed, else empty"},
    "dob": {"type": "string"},
    "nationality_code": {"type": "string", "description": "Three-letter code printed on the ID (e.g. VNM), else empty"},
    "document_number": {"type": "string"},
    "expiry": {"type": "string"},
    "address": {"type": "string", "description": "The address printed on the document, else empty"},
    "issued_on": {"type": "string", "description": "Issue or statement date, else empty"},
    "note": {"type": "string", "description": "One short sentence on anything wrong with the document, else empty"}},
    "required": ["readable", "problems", "surname", "given_names", "dob", "nationality_code", "document_number", "expiry", "address",
                 "issued_on", "note"]}
# The ID's read also says whether the whole document and its MRZ are in shot.
_ID = dict(_DOC, properties=dict(_DOC["properties"],
    edges_visible={"type": "boolean", "description": "All four corners and edges of the document are inside the picture"},
    mrz_visible={"type": "boolean", "description": "Both machine-readable (MRZ) lines at the bottom of a passport page are fully "
                                                   "visible and legible, with nothing over them"}),
           required=_DOC["required"] + ["edges_visible", "mrz_visible"])
# The passport's back page carries no personal data, only what an officer
# wrote into it; an endorsement is for a person to read.
_BACK = {"type": "object", "additionalProperties": False, "properties": {
    "readable": {"type": "boolean"},
    "endorsements": {"type": "string", "description": "The endorsements / observations as printed (e.g. NONE), else empty"},
    "issuing_authority": {"type": "string", "description": "The issuing authority as printed, else empty"},
    "note": {"type": "string", "description": "One short sentence on anything wrong with the page, else empty"}},
    "required": ["readable", "endorsements", "issuing_authority", "note"]}
# The secondary ID (a national ID card, PAN, Aadhaar, a driving licence, a
# naturalisation certificate): its name in one, as the card writes it.
_SECONDARY = {"type": "object", "additionalProperties": False, "properties": {
    "readable": {"type": "boolean"},
    "problems": _DOC["properties"]["problems"],
    "document_type": {"type": "string", "description": "What the document is, as printed (e.g. DRIVER LICENSE, PAN, CCCD)"},
    "name": {"type": "string", "description": "The holder's full name exactly as printed, in the printed order"},
    "dob": {"type": "string"},
    "document_number": {"type": "string"},
    "expiry": {"type": "string"},
    "address": {"type": "string", "description": "The address printed on the document, else empty"},
    "note": {"type": "string", "description": "One short sentence on anything wrong with the document, else empty"}},
    "required": ["readable", "problems", "document_type", "name", "dob", "document_number", "expiry", "address", "note"]}
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"id": _ID, "back": _BACK, "secondary": _SECONDARY, "poa": _DOC},
          "required": ["id", "back", "secondary", "poa"]}

# Claude's own verdict: advisory, beside the rules' decision.
VERDICT_SYSTEM = ("You are a senior KYC analyst at a bank giving a second opinion. You get a customer's record, the "
                  "bank's document policy, the ID-verification vendor's result, and what an earlier reading step read "
                  "from the customer's documents: the passport (data page and back page), the secondary ID and the proof "
                  "of address. Decide whether you would verify this customer's identity and address from these documents "
                  "(verify) or send the case to a person (send_to_person). Judge only what is given; a field that was not "
                  "read was not legible. Give your confidence (0-100) that your decision is right and one short sentence "
                  "why. When you are also given watchlist records the name screening left open, say separately whether "
                  "the documents show the customer is the listed person (same_person), is not (different_person) or "
                  "cannot tell (cannot_tell), with one short sentence why; with no watchlist record, answer no_alert. "
                  "The watchlist answer does not change your verify / send_to_person decision, which is about the "
                  "documents only.")
VERDICT_WATCHLIST = {"same_person": "Same person", "different_person": "A different person", "cannot_tell": "Cannot tell"}
VERDICT_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "decision": {"type": "string", "enum": ["verify", "send_to_person"]},
    "confidence": {"type": "integer", "description": "0-100: how sure you are that your decision is right"},
    "reason": {"type": "string", "description": "One short sentence"},
    "watchlist": {"type": "string", "enum": [*VERDICT_WATCHLIST, "no_alert"]},
    "watchlist_reason": {"type": "string", "description": "One short sentence, else empty"}},
    "required": ["decision", "confidence", "reason", "watchlist", "watchlist_reason"]}
def _vn_mergers():
    """Vietnam's 2025 mergers (the address rules' list) in words."""
    joined, seen = {}, set()
    for old, new in addresses.VN_MERGED.items():
        if old.replace(" - ", " ") not in seen:
            seen.add(old.replace(" - ", " "))
            joined.setdefault(new.title(), []).append(old.title())
    return "; ".join(f"{', '.join(olds)} → {new}" for new, olds in sorted(joined.items()))


def _age_exemption():
    exempt = sop.value("poa_no_age_limit")
    if exempt == ["aadhaar"]:
        return " (except an Aadhaar card: it is a permanent card, so its date of issue does not matter)"
    if exempt:
        return f" (except {', '.join(sop.poa_type_labels(exempt))}: permanent cards, so their date of issue does not matter)"
    return ""


def policy():
    """The bank's document policy Claude's verdict is given, in the SOP's
    current terms (sop: the proof of address's age limit and the cards it
    does not apply to); at the shipped values it reads as POLICY."""
    return ("The ID must be fully legible (all four edges and the MRZ in shot, nothing covering it, not blurred or "
          "damaged), not expired, and its name, date of birth, nationality and number must match the record (a "
          "spelling variant, a nickname, another word order or an added middle name is acceptable). The proof of "
          f"address must be legible, issued within the last {sop.poa_age_text()}{_age_exemption()}, "
          "and show the customer's registered address. The secondary ID "
          "must be legible and name the same person, born on the same day, as the passport or the record (it may write "
          "the name another way: an initial, surname particles run together); one in a surname the passport does not "
          "print (a married or maiden name) needs the marriage certificate that links the two, which is not among these "
          "documents, even when the record keeps both surnames; its "
          "address is not the address evidence. An endorsement on the passport's back page is for a person to read. "
          "On 1 July 2025 Vietnam merged provinces and abolished district-level units, so an address may name a "
          "former province or district where the other names the province it joined; that is the same place. "
          f"Former → current: {_vn_mergers()}.")


POLICY = policy()                       # the shipped text (sop's defaults at import)
# Bumped when POLICY changes: verdicts given under an older one are redone.
# 4: every document, and the open watchlist records. Not bumped on
# 2026-10-25, when a secondary ID in a surname the passport does not print
# stopped passing because the record keeps both surnames: that sentence
# bears only on the cases the checks decide again for it (rules 13), and
# redecide asks those again; every other verdict would read the same.
POLICY_VERSION = 4
# A verdict call that failed (a busy gateway) is asked again on later agent
# runs, one try per run, this many tries in all.
VERDICT_TRIES = 3
# Bumped when the checks change: a case they sent to a person, and no one has
# touched since, is checked again on its stored read (recheck). 4: the name
# on the proof of address. 5: the holder's own given name (_own_name_kept),
# and the verified address written to the record (address_update). 6: an old
# proof of address reported once when its read and recorded dates agree.
# 7: the passport's back page and the secondary ID. 8: an old proof of
# address, the only thing wrong, asked for again (customer_request). 9: the
# record's own slips corrected (name_slip, address_slip) and RFIs for the
# document that links two names or for the main home. 10: a word that
# matched the surname by its spelling is not a given name too (name_verdict).
# 11: an anglicised first name is not the passport's (_given_match), and a
# postal code another document disputes is not written to the record
# (address_update). 12: an anglicised first name, with the naturalisation
# certificate on the case, is the record's slip: it takes the passport's legal
# name and keeps the one the customer goes by as the preferred name (name_slip).
# 13: a secondary ID in a surname the passport does not print fails, even when
# the record keeps both surnames (_second_surname); with nothing else wrong the
# customer is asked for the marriage certificate (name_link_request).
RULES_VERSION = 13
POA_NAME_RULES = 4    # the first checks to want the proof of address's name: reads before them may lack it (needs_reread)
# The last change to the name rules (5: the holder's own given name; 10: one
# word, one part of the name; 11: no anglicised first name; 12: an anglicised
# first name settled by the naturalisation certificate on the case; 13: a
# secondary ID in a surname the passport does not print): a case confirmed
# before it is checked again on its names (confirmed_on_older_names), and one
# asked about its names before is decided again (asked_on_older_names).
NAME_RULES = 13
# The last change to the address the agent writes back (11: a disputed postal
# code stays; a unit number is no ZIP): address_written_otherwise.
ADDRESS_RULES = 11
# What perceive() returns: a stored read decided again keeps it (redecide
# leaves out Claude's verdict, which is asked again).
PERCEIVED = ("at", "model", "read", "usage", "seconds", "error", "verdict", "read_version", "ocr")
# What a read covers. 1: the passport's data page and the proof of address.
# 2: every document on the case. A case read under 1 that no person has
# touched starts again and is read again (read_before_all_documents).
READ_VERSION = 2
NO_ENDORSEMENT = {"", "NONE", "NIL", "NO", "N/A", "NA", "-", "NOT RECORDED"}
VERDICT_LABELS = {"verify": "Verify", "send_to_person": "Send to a person"}

# What an analyst asks the customer for when the ID itself is the problem
# (vocab.RESUBMIT_ID_REASONS); the agent suggests it, the analyst decides.
RESUBMIT_CROPPED = "Cropped"
RESUBMIT_DAMAGED = "This ID is not in a suitable state"
RESUBMIT_ILLEGIBLE = "The document you have provided is illegible"
RESUBMIT_UNCLEAR = ("Please resubmit a clear, good quality valid proof of identity and ensure all information on the "
                    "document is visible")
RESUBMIT_EXPIRED = "The document you have submitted is expired"
# What the agent asks for by itself (vocab.RESUBMIT_POA_REASONS), and the
# proof-of-address check that alone lets it, as the shipped SOP words them;
# the agent uses the current words (sop.poa_old_reason, sop.poa_age_label).
RESUBMIT_POA_OLD = "The proof of address is older than 6 months"
POA_TOO_OLD = "Issued within the last 6 months"


# ── which case, which images ─────────────────────────────────────────────────

def eligible(case):
    """An individual (not a business) in Data Confirming the agent can work:
    one the screening rules cleared (auto-verified), which verifies itself
    when every check passes, or one with an alert left that no person holds
    or has worked, which the agent confirms for an analyst to clear the
    alert. Businesses are the analysts' from the start."""
    if case["tenant"] != "ttlive" or case["status"] != "DataConfirming" or case.get("agency") or case.get("awaiting_documents"):
        return False                    # (a case waiting for its uploaded images is read once they are in)
    if case["screening"].get("workflow") == "auto-verified":
        return True
    return bool(case["screening"].get("hit")) and not case.get("current_reviewer") and not _worked_by_a_person(case)


def _worked_by_a_person(case):
    """Has an analyst acted on the case? Their audit entries carry their
    e-mail; the system, the vendor, the screening and the agent do not.
    Sending the customer's images in (case_documents) is not working it."""
    return any("@" in (e.get("actor") or "") and e.get("action") not in case_documents.PASSIVE_ACTIONS
               for e in case.get("audit", []))


def _id_doc(case):
    return next((d for d in case["documents"] if d.get("category") == "Passport"), None) or case["documents"][0]


def _there(doc):
    """A document with an image: drawn, or uploaded; not a slot still
    waiting for its upload, which counts as no document."""
    return not doc.get("awaiting") or bool(doc.get("upload"))


def _back_doc(case):
    return next((d for d in case["documents"] if d.get("category") == "Passport" and d.get("side") == "back" and _there(d)), None)


def _secondary_doc(case):
    """The customer's second identity document: anything that is neither the
    passport nor the proof of address."""
    return next((d for d in case["documents"] if d.get("category") not in ("Passport", "ProofOfAddress") and _there(d)), None)


def _image(data, media_type="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}}


def _picture(case, doc, render):
    """The image block for one document: the photo or scan uploaded for it
    (documents/uploads), else the drawing of it."""
    up = doc.get("upload")
    if up:
        return _image(uploads.load(up["path"]), up["media_type"])
    return _image(svg_to_png(render(case, doc)))


def read(case, render, call=None, sleep=time.sleep):
    """Claude's read of every document on the case: (read, usage, seconds)."""
    return _ask(call or llm.call, SYSTEM, read_content(case, render), SCHEMA, 4000, sleep)


def read_content(case, render):
    """What the read sends with SYSTEM and SCHEMA: each document's image,
    after a line saying which document it is (also what the model benchmark
    sends every model it compares, fincrime/benchmark.py)."""
    content, n = [], 0

    def add(doc, key, intro, missing):
        nonlocal n
        if doc is None:
            content.append({"type": "text", "text": missing + f" Return `{key}` unreadable with empty fields."})
            return
        n += 1
        content.extend([{"type": "text", "text": f"Image {n} is {intro}"}, _picture(case, doc, render)])

    add(_id_doc(case), "id", "the customer's ID (passport data page). Read it into `id`.", "There is no ID.")
    add(_back_doc(case), "back", "the passport's back page. Read it into `back`.", "There is no passport back page.")
    sec = _secondary_doc(case)
    add(sec, "secondary", f"the customer's secondary ID ({sec['name'] if sec else ''}). Read it into `secondary`, with the "
                          "holder's full name exactly as printed, in the printed order, in `name`. A US driver's licence "
                          "prints its dates month first (MM/DD/YYYY).", "There is no secondary ID.")
    poa = workflow.poa_docs(case)
    add(poa[0] if poa else None, "poa", "the customer's proof of address. Read it into `poa`, with the holder's (account "
                                        "holder's, cardholder's) full name as printed in `surname`. An S/O, D/O, W/O or C/O "
                                        "line in an address names a relative, not the holder.", "There is no proof of address.")
    return content


def _ask(call, system, content, schema, max_tokens, sleep):
    """One model call; a busy gateway (429) or a dropped connection is worth
    another try."""
    for attempt in range(RETRIES):
        try:
            return call(system, content, schema, max_tokens=max_tokens)
        except WorkflowError as exc:
            if not any(m in str(exc) for m in (" 429", "Could not reach")) or attempt == RETRIES - 1:
                raise
            sleep(RETRY_WAIT * (attempt + 1))


# ── Claude's own verdict (advisory) ──────────────────────────────────────────

def open_matches(wc):
    """The watchlist records the screening left for a person (unresolved)."""
    return [m for m in (wc or {}).get("matches", []) if (m.get("resolution") or {}).get("status", "Unresolved") == "Unresolved"]


def _listed(m):
    """A watchlist record as the verdict call is told it."""
    kd = m.get("key_data") or {}
    places = [", ".join(x for x in (p.get("city"), p.get("region"), p.get("country")) if x) for p in kd.get("locations") or []]
    return {"name": m.get("name", ""), "aliases": m.get("aliases") or [], "dates_of_birth": m.get("dobs") or [],
            "gender": m.get("gender", ""), "citizenship": m.get("citizenship", ""), "locations": [p for p in places if p],
            "category": " / ".join(x for x in (kd.get("category"), kd.get("sub_category")) if x), "types": m.get("types") or []}


def verdict_input(case, got, now=None, wc=None):
    """What the verdict call is told: today, the policy, the record, the
    vendor's result, the read and any open watchlist record. Never the
    rules' checks or decision."""
    now = now or dates.now()
    client, vc = case["client"], case.get("vendor_check") or {}
    poa = workflow.poa_docs(case)
    record = {"legal_name": client["legal_name"], "date_of_birth": client["dob"], "nationality": client["nationality"],
              "id_type": client.get("id_type", ""), "id_number": client.get("id_number", ""),
              "registered_address": client.get("address", ""), "proof_of_address_required": bool(case.get("poa_required")),
              "proof_of_address_document": poa[0]["name"] if poa else ""}
    vendor = vc.get("result", "") + (f" ({vc['vendor_reason']})" if vc.get("vendor_reason") else "")
    listed = [_listed(m) for m in open_matches(wc)]
    return (f"Today is {now:%Y-%m-%d}.\nPolicy: {policy()}\nCustomer record: {json.dumps(record, ensure_ascii=False)}\n"
            f"ID-verification vendor: {vendor or 'no result'}\n"
            f"Read from the documents: {json.dumps(got, ensure_ascii=False)}\n"
            + (f"Watchlist records the name screening left open: {json.dumps(listed, ensure_ascii=False)}" if listed
               else "Watchlist records the name screening left open: none."))


def opinion(case, got, call=None, sleep=time.sleep, wc=None):
    """Claude's verdict on a read: {decision, confidence, reason, ...} or
    {error}. Touches nothing."""
    started = time.monotonic()
    out = {"at": dates.now_str(), "model": MODEL, "policy": POLICY_VERSION}
    try:
        ans, usage, seconds = _ask(call or llm.call, VERDICT_SYSTEM, [{"type": "text", "text": verdict_input(case, got, wc=wc)}],
                                   VERDICT_SCHEMA, 2000, sleep)
        decision = ans.get("decision") if isinstance(ans, dict) else None
        if decision not in VERDICT_LABELS:
            raise WorkflowError("The model's verdict was not verify or send_to_person.")
        out.update(decision=decision, confidence=max(0, min(100, int(ans.get("confidence") or 0))),
                   reason=str(ans.get("reason") or "")[:300], usage=usage, seconds=seconds)
        if ans.get("watchlist") in VERDICT_WATCHLIST and open_matches(wc):
            out.update(watchlist=ans["watchlist"], watchlist_reason=str(ans.get("watchlist_reason") or "")[:300])
    except (WorkflowError, TypeError, ValueError) as exc:
        out["error"] = str(exc)
    out.setdefault("seconds", round(time.monotonic() - started, 1))
    return out


def rules_say(record):
    """The rules' decision in the verdict's words: the documents passed every
    check (verified, or confirmed with an alert left) or went to a person."""
    return "verify" if record.get("decision") in ("verified", "screening") else "send_to_person"


def attach_verdict(record, verdict):
    """Put the verdict on the agent's record, with whether it agrees; a failed
    call counts its tries, after the failed tries before it."""
    before = record.get("verdict")
    if verdict.get("error"):
        again = before is not verdict and bool(before) and bool(before.get("error")) \
            and before.get("policy", 1) == verdict.get("policy", 1)
        verdict["tries"] = before.get("tries", 1) + 1 if again else 1
    else:
        verdict["agrees"] = verdict["decision"] == rules_say(record)
    record["verdict"] = verdict
    return verdict


def needs_verdict(case):
    """A case the agent read before verdicts existed, whose verdict was given
    under an older policy, or whose verdict call failed fewer than
    VERDICT_TRIES times: it gets one from its stored read, with no new look
    at the images."""
    rec = case.get("ai_agent") or {}
    if not rec.get("read") or rec.get("error"):
        return False
    verdict = rec.get("verdict")
    if verdict is None or verdict.get("policy", 1) != POLICY_VERSION:
        return True
    return bool(verdict.get("error")) and verdict.get("tries", 1) < VERDICT_TRIES


# ── the checks ───────────────────────────────────────────────────────────────

def _name_key(text):
    return sorted(similarity.tokens(text))


# Names a holder often goes without (a Telugu prefix, Mohammed): a CRM that
# drops one keeps the holder's own name all the same.
DROPPED_FIRST = names.GIVEN_PREFIXES | {"MOHAMMED", "MOHAMMAD", "MUHAMMAD", "MOHD", "MD"}


def _given_match(a, b):
    """names.given_match, less the English first names naturalised citizens
    take (names.ANGLICISED: KEVIN for KHOA). Those help a screening find a
    hit, but are no spelling of the passport's name: only a naturalisation
    certificate links the two (name_link_request)."""
    if b in names.ANGLICISED.get(a, ()) or a in names.ANGLICISED.get(b, ()):
        return 0.0
    return names.given_match(a, b)


def _own_name_kept(given, rest, country=""):
    """An ID with two given names, the second of them the father's (an
    Indian passport's MANASI PRAKASH): the CRM must keep the first, or its
    initial. A CRM that keeps only the second names the father (PRAKASH
    MORE): another person. A Vietnamese passport's personal name is its
    last, and a longer name is often called by its last (VENKATA LAKSHMI
    DURGA as DURGA), so neither is held to this."""
    if country == "VN" or len(given) != 2 or given[0] in DROPPED_FIRST:
        return True
    own = given[0]
    return any(_given_match(own, b) >= 0.9 or (names.is_initial(b) and b == own[0]) for b in rest)


def name_verdict(crm, surname, given, filed="", country=""):
    """Is the CRM name the person the ID names? The file book's name rules
    decide: the ID's surname is among the CRM's names (any typing, a
    compound part, a spelling variant) and the holder's own given name
    agrees (exact, nickname, short form or spelling variant; an initial
    alone is not enough; _own_name_kept). `filed` is the name as the file
    wrote it before the console put it in capitals: broken characters
    (UTF-8 read as Windows-1252) repair from that text, since capitals
    change the bytes they stand for, and a byte the file itself lost stands
    for one letter. `country` is the ID's. Returns (same, value to
    confirm)."""
    source = filed if filed and filed.upper() == crm else crm
    crm_fixed, repaired = names.repair_mojibake(source)
    c, s, g = similarity.tokens(crm_fixed), similarity.tokens(surname), similarity.tokens(given)
    if not c or not (s or g):
        return False, crm
    if repaired is False:
        found = _lost_letters_match(names.mojibake_lost(source), s + g)
        if found:
            return True, " ".join(found)
    if _name_key(crm_fixed) == sorted(s + g):
        return True, (similarity.fold(" ".join(c)) if repaired else crm)
    if not s:
        # a single-name passport: the CRM's other tokens are fillers (FNU),
        # repeats or initials the record system added
        core = {t for t in c if t not in FILLERS and not names.is_initial(t)}
        return (bool(core) and core <= set(g)), " ".join(g)
    sur_ok = names.surname_score(c, s) >= 0.8 or (
        # a patronymic initial: BALAJI K for KRISHNAN BALAJI
        any(names.is_initial(t) and t[0] == s[0][0] for t in c))
    rest = [t for t in c if t not in names.surname_forms(s)]
    if len(rest) == len(c):
        # the surname matched by its spelling only: the record's word that
        # matched it is the surname, not a given name as well (HONG for both
        # the passport's HOÀNG and its HỒNG)
        spelt = max(c, key=lambda t: names.surname_score([t], s))
        if names.surname_score([spelt], s) >= 0.8:
            rest.remove(spelt)
    giv_ok = not g or (any(_given_match(a, b) >= 0.9 for a in g for b in rest) and _own_name_kept(g, rest, country))
    if not (sur_ok and giv_ok):
        return False, crm
    # the ID's spelling, in the order the CRM keeps (surname first or last)
    surname_first = c[0] in names.surname_forms(s)
    return True, " ".join(s + g if surname_first else g + s)


def _lost_letters_match(lossy, printed):
    """The ID's name tokens, in the record's order, when each token of a
    record name that lost characters matches a different one of them: a lost
    character stands for exactly one letter, every other letter is the same.
    None when they do not."""
    if not lossy:
        return None
    want = [re.compile("".join("[A-Z]" if ch == "\ufffd" else re.escape(similarity.fold(ch)) for ch in tok) + "$")
            for tok in lossy.split()]
    left, found = list(printed), []
    if len(want) != len(left):
        return None
    for w in want:
        hit = next((t for t in left if w.match(t)), None)
        if hit is None:
            return None
        left.remove(hit)
        found.append(hit)
    return found


def poa_name_verdict(crm, poa, filed="", country=""):
    """Is the proof of address in the CRM name? A proof of address prints the
    holder's name in one line, with no surname field, so each of its names is
    tried as the surname, the rest as the given names, under the ID's rules
    (name_verdict). True, False, or None when no name was read."""
    printed = similarity.tokens(" ".join((poa.get("surname", ""), poa.get("given_names", ""))))
    if not printed:
        return None
    if poa.get("surname") and poa.get("given_names") and name_verdict(crm, poa["surname"], poa["given_names"], filed, country)[0]:
        return True
    if len(printed) == 1:
        return name_verdict(crm, "", printed[0], filed, country)[0]
    return any(name_verdict(crm, t, " ".join(printed[:i] + printed[i + 1:]), filed, country)[0] for i, t in enumerate(printed))


def _run_on(name):
    """A name with its surname particles run on to the word after them:
    DE LA CRUZ → DELACRUZ, MC DONALD → MCDONALD."""
    out, carry = [], ""
    for t in similarity.tokens(name):
        if t in names.PREFIXES:
            carry += similarity.squash(t)
            continue
        out.append(carry + t)
        carry = ""
    return " ".join(out + ([carry] if carry else []))


def holder_named(printed, surname, given, country=""):
    """Does a name printed in one line (a secondary ID's, any order) name the
    holder another document gives as a surname and given names (the
    passport, a watchlist record's SURNAME, Given)? The ID's name rules
    (name_verdict), held to that surname, and the ways a second document
    writes a name: surname particles run together (DINAPOLI for DI NAPOLI,
    DE LUCA for DELUCA) and a single name with an initial after it (PRABHU K
    for the passport's PRABHU)."""
    if not similarity.tokens(printed) or not similarity.tokens(f"{surname} {given}"):
        return False
    return any(name_verdict(p, s, given, country=country)[0]
               for p, s in ((printed, surname), _spaced_alike(printed, surname)))


def _spaced_alike(printed, surname):
    """(printed, surname) with the surname written in one word on both, when
    the two differ only in its spacing: LE BLANC for LEBLANC, DINAPOLI for DI
    NAPOLI. The pair unchanged when the printed name has no such run."""
    whole = similarity.squash("".join(similarity.tokens(surname)))
    toks = similarity.tokens(printed)
    for i in range(len(toks)):
        for j in range(i + 1, min(len(toks), i + 3) + 1):
            if whole and similarity.squash("".join(toks[i:j])) == whole:
                return " ".join(toks[:i] + [whole] + toks[j:]), whole
    return printed, surname


def same_holder(printed, reference, filed="", country=""):
    """Does a name one document prints in one line name the holder another
    names in one line (or the record)? Neither says which word is the
    surname, so each is tried (poa_name_verdict), with the same ways of
    writing a name as holder_named."""
    if not similarity.tokens(printed) or not similarity.tokens(reference):
        return False
    if any(poa_name_verdict(b, {"surname": a}, filed, country)
           for a, b in ((printed, reference), (_run_on(printed), _run_on(reference)))):
        return True
    words = [t for t in similarity.tokens(printed) if not names.is_initial(t)]
    single = [t for t in similarity.tokens(reference) if not names.is_initial(t) and t not in FILLERS]
    return len(set(single)) == 1 and set(words) == set(single)


def _listed_name(listed):
    """(surname, given) of a watchlist name: SURNAME, Given; an alias with no
    comma is (None, the alias)."""
    surname, comma, given = (listed or "").partition(",")
    return (surname.strip(), given.strip()) if comma else (None, listed or "")


# ── the watchlist, beside the documents ─────────────────────────────────────

WATCH_LABELS = vocab.AI_WATCH_FILTER
_WATCH_RANK = ["likely_match", "possible_match", "unread", "likely_different"]


def _dob_relation(dob, listed):
    """How a document's date of birth stands to the dates a record lists:
    same, swapped (day and month exchanged), same_year (a bare year that is
    the customer's), in_range, differs, not_listed or unread."""
    born = dates.parse(dob or "")
    if born is None:
        return "unread"
    if not listed:
        return "not_listed"
    found = set()
    for raw in listed:
        span = re.fullmatch(r"\s*(\d{4})\s*[–-]\s*(\d{4})\s*", raw or "")
        if span:
            found.add("in_range" if int(span[1]) <= born.year <= int(span[2]) else "differs")
            continue
        parts = ai_screening._listed_dob(raw)
        if parts is None:
            continue
        year, full = parts
        if full is None:
            found.add("same_year" if year == born.year else "differs")
        elif full == born.date():
            found.add("same")
        else:
            found.add("swapped" if ai_screening._swapped(full, born.date()) else "differs")
    for rel in ("same", "swapped", "same_year", "in_range", "differs"):
        if rel in found:
            return rel
    return "not_listed"


DOB_WORDS = {"same": "is the listed date", "swapped": "is the listed date with day and month swapped",
             "same_year": "is in the one year the record lists", "in_range": "is in the range of years the record lists",
             "differs": "differs from every date the record lists", "not_listed": "cannot be compared: the record lists none",
             "unread": "could not be read from the documents"}


def watchlist_findings(case, wc, got):
    """What the documents say beside each watchlist record the screening left
    open: the name the passport prints against the listed name and aliases,
    its date of birth against the listed dates, its nationality against the
    citizenship. {finding, matches}, or None with nothing open. It informs the
    analyst who clears the alert; it decides nothing."""
    open_ = open_matches(wc)
    if not open_:
        return None
    idr, sec = got.get("id") or {}, got.get("secondary") or {}
    name = " ".join(x for x in (idr.get("surname", ""), idr.get("given_names", "")) if x) or sec.get("name", "")
    dob = idr.get("dob") or sec.get("dob", "")
    code = (idr.get("nationality_code") or "").upper()
    country = vocab.COUNTRY_NAMES.get(ALPHA2.get(code, code[:2] if len(code) == 2 else ""), "")
    out = []
    for m in open_:
        listed = [m.get("name", "")] + list(m.get("aliases") or [])
        if not name:
            name_rel = "unread"
        elif any(holder_named(name, *_listed_name(n)) if _listed_name(n)[0] is not None else same_holder(name, n)
                 for n in listed if n):
            name_rel = "same"
        elif set(t for t in similarity.tokens(name) if len(t) > 2) & set(t for n in listed for t in similarity.tokens(n) if len(t) > 2):
            name_rel = "partly"
        else:
            name_rel = "different"
        dob_rel = _dob_relation(dob, m.get("dobs") or [])
        cit = m.get("citizenship", "")
        nat_rel = None if not (country and cit) else similarity.fold(country) in similarity.fold(cit)
        if name_rel == "unread" or (dob_rel == "unread" and name_rel != "different"):
            finding = "unread"
        elif dob_rel == "differs" or name_rel == "different":
            finding = "likely_different"
        elif name_rel == "same" and dob_rel in ("same", "swapped"):
            finding = "likely_match"
        else:
            finding = "possible_match"
        name_words = {"same": "is the listed name", "partly": "shares only part of the listed name",
                      "different": "is not the listed name", "unread": "could not be read"}[name_rel]
        detail = f"The documents' name {name or '—'} {name_words} ({m.get('name', '')}); date of birth {dob or '—'} {DOB_WORDS[dob_rel]}"
        if nat_rel is not None:
            detail += f"; nationality {country} {'is' if nat_rel else 'is not'} the listed citizenship ({cit})"
        out.append({"wc_id": m.get("wc_id", ""), "name": m.get("name", ""), "dobs": m.get("dobs") or [], "citizenship": cit,
                    "category": _listed(m)["category"], "document_name": name, "document_dob": dob,
                    "name_relation": name_rel, "dob_relation": dob_rel, "nationality_relation": nat_rel,
                    "finding": finding, "detail": detail + "."})
    return {"finding": min((x["finding"] for x in out), key=_WATCH_RANK.index), "matches": out}


def _filed_name(case):
    """The record's name as the reconciliation log wrote it, for a case
    built from the file; '' for any other case."""
    from fincrime.data import recon_book                 # lazy: recon_book imports the seed builders
    v = recon_book.VIEWS.get((case.get("recon") or {}).get("id", ""))
    return v["customer"]["name"] if v else ""


def _poa_address(read, country):
    """The demo's proof-of-address images print the passport country after
    every address, so an overseas one reads '..., CA 92683, USA, Vietnam'.
    Drop that tail when the address before it names another country."""
    name = vocab.COUNTRY_NAMES.get(country, "")
    head, _, tail = read.rpartition(",")
    if name and head and similarity.fold(tail) == similarity.fold(name)             and addresses.normalise(head, country)["country"] not in ("", country):
        return head.strip()
    return read


# What makes a registered address out of date when the proof of address
# matches it: a city or district under an older or other name (Bangalore,
# Secunderabad) or Vietnam's pre-2025 units, where the document names the
# current ones (addresses.normalise flags).
STALE_ADDRESS = {"legacy_city", "district_alias", "neighbourhood_alias", "pre2025_unit"}


def address_update(case, got):
    """The proof of address's address, when the record's should be corrected
    to it as an analyst would: the documents' postal code where the record's
    was keyed wrong (226001 for 226010), or the current place names where
    the record keeps older ones. '' when the two differ only in how they are
    written (abbreviations, ZIP+4, the order of a flat and wing). The
    record's code stays when another document prints the same address
    under another code than the proof of address (_code_disputed)."""
    country, now = case["client"]["nationality"], case["client"].get("address", "")
    read = _poa_address(((got or {}).get("poa") or {}).get("address", ""), country)
    reg, poa = addresses.normalise(now, country), addresses.normalise(read, country)
    if addresses.compare(reg, poa) != "same":
        return ""
    wrong_code = bool(reg["postal5"] and poa["postal5"]) and reg["postal5"] != poa["postal5"]
    disputed = wrong_code and _code_disputed(got, country, poa)
    stale = (set(reg["flags"]) & STALE_ADDRESS) - set(poa["flags"])
    if (not wrong_code or disputed) and not stale:
        return ""
    if not stale:
        # only the code was keyed wrong: correct it in the record's own words (the state the bill leaves out stays)
        text = re.sub(rf"\b{reg['postal5']}(-\d{{4}})?\b", poa["postal5"], now, count=1)
    else:
        text = _as_the_record_keeps(read, country)
        if disputed:
            text = re.sub(rf"\b{poa['postal5']}(-\d{{4}})?\b", reg["postal5"], text, count=1)
    return "" if similarity.fold(text) == similarity.fold(now) else text


def _code_disputed(got, country, poa):
    """Does another document (the passport, the secondary ID) print the proof
    of address's address under another postal code? Then the documents do
    not agree on the code, and one read of the proof of address (700089
    where the bill prints 700019) is no ground to rewrite the record's."""
    for doc in ((got or {}).get("id") or {}, (got or {}).get("secondary") or {}):
        other = addresses.normalise(doc.get("address", ""), country)
        if other["postal5"] and other["postal5"] != poa["postal5"] and addresses.compare(other, poa) == "same":
            return True
    return False


def _as_the_record_keeps(read, country):
    """A document's address as the record keeps one: no relative's line (S/O:
    Krishnan, ...), no country after its own."""
    text = addresses.RELATION.sub("", read).strip()
    head, _, tail = text.rpartition(",")
    if head and addresses.COUNTRY_WORDS.get(similarity.fold(tail).strip()) == country:
        text = head.strip()
    return text


def write_address(case, record, address, why=""):
    """Correct the record's registered address to the proof of address's."""
    before = case["client"].get("address", "")
    case["client"]["address"] = address
    workflow._change(case, ACTOR, "address", before, address)
    workflow._audit(case, ACTOR, "Address updated", f"Registered address corrected to the proof of address's: {address} (was {before})."
                    + (f" {why[0].upper()}{why[1:]}." if why else ""))
    record["address_updated"] = {"from": before, "to": address}


def resubmit_reason(got, record_checks):
    """The Resubmit ID reason that fits what the agent found on the ID, or ""
    when the ID itself is fine (a name or address mismatch is not the
    customer's photo to retake)."""
    idr = (got or {}).get("id") or {}
    problems = set(idr.get("problems") or [])
    failed = {c["check"] for c in record_checks if not c["ok"]}
    if "ID readable" in failed:
        if "cut_off" in problems:
            return RESUBMIT_CROPPED
        if "damaged" in problems:
            return RESUBMIT_DAMAGED
        if problems & {"blurred", "too_dark"}:
            return RESUBMIT_ILLEGIBLE
        if problems & {"covered", "angled", "glare"}:
            return RESUBMIT_UNCLEAR
        if idr.get("edges_visible") is False:        # out of shot, and nothing else named
            return RESUBMIT_CROPPED
        return RESUBMIT_UNCLEAR
    expiry = next((c for c in record_checks if c["check"] == "Expiry"), None)
    if expiry and not expiry["ok"] and expiry["note"].startswith("ID expired"):
        return RESUBMIT_EXPIRED
    return ""


def _check(out, check, crm, document, ok, note="", info=False):
    """One row of the checks. An `info` row is shown for the analyst and
    never fails: it is not evidence the case turns on."""
    row = {"check": check, "crm": crm or "", "document": document or "", "ok": bool(ok), "note": note}
    if info:
        row["info"] = True
    out.append(row)


SECOND_SURNAME = "Name on the secondary ID is in a surname the passport does not print (a married or maiden name)"


def _second_surname(record, idr, printed):
    """The words of the name a secondary ID prints that are a surname the
    passport does not print, when the record keeps them beside the passport's
    own surname (SEEMA VERMA PANDEY: the passport's VERMA, the Aadhaar's SEEMA
    PANDEY): a married or maiden name. The record keeping both surnames links
    nothing; the marriage certificate does (name_link_request). [] when the
    record lacks the passport's surname: the name check fails on it already."""
    s, g = similarity.tokens(idr.get("surname", "")), similarity.tokens(idr.get("given_names", ""))
    rec = similarity.tokens(record)
    if not s or not set(s) <= set(rec):
        return []
    return [t for t in similarity.tokens(printed) if t in rec and t not in s and _foreign(t, set(s + g))]


def _other_documents(case, got, out, printed):
    """The checks on the passport's back page and the secondary ID. Only a
    read of every document has them (READ_VERSION 2): an older read is
    checked as it was."""
    client = case["client"]
    if "back" in got and _back_doc(case) is not None:
        back = got.get("back") or {}
        endorsed = similarity.fold(back.get("endorsements", "")) not in NO_ENDORSEMENT
        shown = " · ".join(x for x in (f"Endorsements: {back.get('endorsements') or '—'}", back.get("issuing_authority", "")) if x)
        _check(out, "Passport back page", "", shown if back.get("readable") else "not readable", not endorsed,
               f"An endorsement on the passport's back page: {back.get('endorsements')}" if endorsed else "")
    sec_doc = _secondary_doc(case)
    if "secondary" not in got or sec_doc is None:
        return
    sec = got.get("secondary") or {}
    named = [p for p in sec.get("problems", []) if p in SPOILERS and p not in SOFT]
    ok = bool(sec.get("readable")) and not named
    _check(out, "Secondary ID readable", "", sec_doc["name"] + ("" if ok else ": not readable"), ok,
           "" if ok else f"Secondary ID could not be read: {', '.join(SPOILERS[p] for p in named) or sec.get('note') or 'unclear'}")
    if not ok:
        return
    name, filed, country = sec.get("name", ""), _filed_name(case), client["nationality"]
    idr = got.get("id") or {}
    on_passport = bool(printed) and holder_named(name, idr.get("surname", ""), idr.get("given_names", ""), country)
    on_record = same_holder(name, client["legal_name"], filed=filed, country=country)
    other_surname = on_record and not on_passport and bool(printed) and bool(_second_surname(client["legal_name"], idr, name))
    shown = name or "not read"
    if name and on_record and not on_passport and printed:
        shown += f" (the record's name; the passport reads {printed})"
    ok = on_passport or (on_record and not other_surname)
    _check(out, "Name on secondary ID", client["legal_name"], shown, ok,
           "" if ok else ("No name could be read on the secondary ID" if not name else SECOND_SURNAME if other_surname
                          else "Name on the secondary ID matches neither the passport nor the CRM"))
    dob = sec.get("dob", "")
    ok = not dob or dob == client["dob"]
    _check(out, "Date of birth on secondary ID", client["dob"], dob or "not printed", ok,
           "" if ok else f"Date of birth on the secondary ID {dob}, CRM {client['dob']}")
    if sec.get("address"):
        same = addresses.compare(addresses.normalise(client.get("address", ""), country), addresses.normalise(sec["address"], country))
        _check(out, "Address on secondary ID", client.get("address", ""), sec["address"], True,
               "Same as the CRM" if same == "same" else "Differs from the CRM; the proof of address is the address evidence",
               info=True)


def checks(case, got, now=None, scanned=None):
    """Every check the agent makes on Claude's read, in order; each says why
    it failed. Also returns the values to confirm when they all pass.
    `scanned` is read_ocr()'s result, when images were uploaded."""
    now = now or dates.now()
    client = case["client"]
    idr, poa = got.get("id") or {}, got.get("poa") or {}
    out = []
    named = [p for p in idr.get("problems", []) if p in SPOILERS]
    spoilt = [SPOILERS[p] for p in named if p not in SOFT]
    if idr.get("edges_visible") is False and "cut off" not in spoilt:
        spoilt.append("its edges are not all in the picture")
    if idr.get("mrz_visible") is False and _id_doc(case).get("category") == "Passport":
        spoilt.append("the MRZ is not fully visible")
    if spoilt:
        spoilt += [SPOILERS[p] for p in named if p in SOFT]
    ok = bool(idr.get("readable")) and not spoilt
    _check(out, "ID readable", "", "readable" if ok else "not readable", ok,
           "" if ok else f"ID could not be read: {', '.join(spoilt) or idr.get('note') or 'unclear'}")

    printed = " ".join(x for x in (idr.get("surname", ""), idr.get("given_names", "")) if x)
    same, name_value = name_verdict(client["legal_name"], idr.get("surname", ""), idr.get("given_names", ""),
                                    filed=_filed_name(case), country=client["nationality"])
    _check(out, "Name", client["legal_name"], printed, same,
           "" if same else ("Name not readable on the ID" if not printed else "Name on the ID differs from the CRM"))

    dob = idr.get("dob", "")
    ok = bool(dob) and dob == client["dob"]
    _check(out, "Date of birth", client["dob"], dob or "not on the ID", ok,
           "" if ok else ("Date of birth missing on the ID" if not dob else f"Date of birth on the ID {dob}, CRM {client['dob']}"))

    code = (idr.get("nationality_code") or "").upper()
    nat = ALPHA2.get(code, code[:2] if len(code) == 2 else "")
    ok = nat == client["nationality"]
    _check(out, "Nationality", client["nationality"], code, ok, "" if ok else "Nationality on the ID differs from the CRM")

    number = similarity.squash(similarity.fold(idr.get("document_number", "")))
    ok = bool(number) and number == similarity.squash(similarity.fold(client.get("id_number", "")))
    _check(out, "ID number", client.get("id_number", ""), idr.get("document_number", ""), ok,
           "" if ok else "ID number on the document differs from the CRM")

    expiry = dates.parse(idr.get("expiry", ""))
    ok = expiry is not None and expiry >= now
    _check(out, "Expiry", "", idr.get("expiry", "") or "not readable", ok,
           "" if ok else (f"ID expired on {idr['expiry']}" if expiry else "Expiry date not readable"))

    vc = case.get("vendor_check") or {}
    ok = vc.get("result") != "Declined"
    _check(out, "Vendor check", "", vc.get("result", ""), ok,
           "" if ok else "ID declined by the vendor" + (f": {vc['vendor_reason']}" if vc.get("vendor_reason") else ""))
    _mrz_check(out, idr, scanned)
    _other_documents(case, got, out, printed)

    if case.get("poa_required"):
        _check(out, "Proof of address readable", "", "readable" if poa.get("readable") else "not readable",
               poa.get("readable"), "" if poa.get("readable") else "Proof of address could not be read")
        if poa.get("readable"):
            holder = " ".join(x for x in (poa.get("surname", ""), poa.get("given_names", "")) if x)
            same = poa_name_verdict(client["legal_name"], poa, filed=_filed_name(case), country=client["nationality"])
            _check(out, "Name on proof of address", client["legal_name"], holder or "not read", same,
                   "" if same else ("No holder name could be read on the proof of address" if same is None
                                    else "Name on the proof of address differs from the CRM"))
        country = client.get("nationality", "")
        verdict = addresses.compare(addresses.normalise(client.get("address", ""), country),
                                    addresses.normalise(_poa_address(poa.get("address", ""), country), country))
        _check(out, "Address", client.get("address", ""), poa.get("address", ""), verdict == "same",
               "" if verdict == "same" else f"Address on the proof of address does not match the CRM ({verdict.replace('_', ' ')})")
        failures = workflow.poa_failures(case, now)
        issued = dates.parse(poa.get("issued_on", ""))
        kind = (workflow.poa_docs(case) or [{}])[0].get("kind", "")
        exempt = sop.value("poa_no_age_limit")
        if issued is not None and (now - issued).days > sop.value("poa_max_age_days") and kind not in exempt \
                and not any(f.get("key") == "poa_age" and poa["issued_on"] in f["detail"] for f in failures):
            # the document's own date, when the upload's record does not already report that date too old
            failures.append({"key": "poa_age", "label": sop.poa_age_label(), "detail": f"the document reads {poa['issued_on']}"})
        if issued is None and poa.get("readable") and kind not in exempt:
            # the record's date proves nothing about the document: its own date must be legible
            failures.append({"label": "Issue date readable", "detail": "the date on the document could not be read"})
        _check(out, "Proof of address checks", "", "; ".join(f["detail"] for f in failures) or "passed", not failures,
               "; ".join(f"{f['label']}: {f['detail']}" for f in failures))
        if failures:
            out[-1]["failures"] = [f["label"] for f in failures]
            out[-1]["failure_keys"] = [f.get("key", "") for f in failures]
    values = {"name": name_value, "dob": client["dob"], "nationality": client["nationality"]}
    return out, values


# ── OCR beside the read ──────────────────────────────────────────────────────
# Tesseract (documents/ocr) reads the images uploaded for a case, on this
# machine. The passport's MRZ, once its check digits add up, is held against
# Claude's read: number, date of birth, expiry, nationality. A difference
# only in characters OCR mistakes for one another is shown, not failed.
MRZ_CHECK = "Passport MRZ read by OCR"
_LOOKALIKE = (set("0OQD"), set("1ILT7"), set("2Z"), set("5S"), set("6G"), set("8B3"), set("4A"))


def read_ocr(case):
    """Tesseract on the images uploaded for the case: the passport's MRZ and
    each image's text. None when no image was uploaded (a drawn document is
    not OCR'd); {"available": False} on a machine without Tesseract."""
    rows = [(slot, case_documents.slot_row(case, slot)) for slot, _ in uploads.SLOTS]
    rows = [(slot, row) for slot, row in rows if row and row.get("upload")]
    if not rows:
        return None
    if not ocr.available():
        return {"available": False}
    started = time.monotonic()
    out = {"available": True, "engine": "Tesseract", "mrz": None, "text": {}, "errors": {}}
    for slot, row in rows:
        try:
            data = uploads.load(row["upload"]["path"])
            if slot == "passport":
                out["mrz"] = ocr.mrz(data)
            out["text"][slot] = ocr.text(data)[:4000]
        except (ocr.OcrError, OSError, WorkflowError) as exc:
            out["errors"][slot] = str(exc)
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def _lookalike(a, b):
    return len(a) == len(b) and all(x == y or any(x in g and y in g for g in _LOOKALIKE) for x, y in zip(a, b))


def _mrz_check(out, idr, scanned):
    """The OCR'd MRZ against Claude's read of the data page (no row without
    uploaded images)."""
    if not scanned:
        return
    if not scanned.get("available"):
        _check(out, MRZ_CHECK, "", "not read", True, "OCR is not installed on this machine", info=True)
        return
    m = scanned.get("mrz")
    if not (m and m.get("valid")):
        why = ("OCR found no machine-readable zone on the passport" if not m
               else "OCR's reading of the MRZ does not add up (its check digits)")
        required = sop.value("ocr_mrz_required")             # the SOP rules page; off: information only
        _check(out, MRZ_CHECK, "", "not readable", not required, why, info=not required)
        return
    f = m["fields"]
    number = similarity.squash(similarity.fold(idr.get("document_number", ""))).upper()
    pairs = (("document number", f["number"], number), ("date of birth", f["dob"], idr.get("dob", "")),
             ("expiry", f["expiry"], idr.get("expiry", "")),
             ("nationality", f["nationality"], (idr.get("nationality_code") or "").upper()))
    differ = [(label, mrz_value, read_value) for label, mrz_value, read_value in pairs if mrz_value != read_value]
    hard = [d for d in differ if not _lookalike(d[1], d[2])]
    said = "; ".join(f"{label}: MRZ {mrz_value}, read {read_value or '—'}" for label, mrz_value, read_value in (hard or differ))
    shown = f"MRZ {f['number']} · born {f['dob']} · expires {f['expiry']} · {f['nationality']}"
    if hard:
        _check(out, MRZ_CHECK, "", shown, False, f"The MRZ read by OCR differs from the read ({said})")
    elif differ:
        _check(out, MRZ_CHECK, "", shown, True, f"OCR and the read differ only in look-alike characters ({said})", info=True)
    else:
        _check(out, MRZ_CHECK, "", shown, True)


# ── one case ─────────────────────────────────────────────────────────────────

def perceive(case, render, call=None, sleep=time.sleep, wc=None):
    """The slow half, safe to run outside any lock: Claude's read of the
    documents, or the error that stopped it. Touches nothing."""
    started = time.monotonic()
    out = {"at": dates.now_str(), "model": MODEL, "read": {}, "usage": {}, "seconds": 0.0, "error": "",
           "read_version": READ_VERSION}
    try:
        got, usage, seconds = read(case, render, call=call, sleep=sleep)
        out.update(read=got, usage=usage, seconds=seconds)
    except WorkflowError as exc:
        out["error"] = str(exc)
    out["seconds"] = out["seconds"] or round(time.monotonic() - started, 1)
    if not out["error"]:
        out["verdict"] = opinion(case, out["read"], call=call, sleep=sleep, wc=wc)
    scanned = read_ocr(case)
    if scanned is not None:
        out["ocr"] = scanned
    return out


DOCUMENTS_READ = "the passport (both pages), the secondary ID and the proof of address"


def decide(case, alert_case, wc, perceived):
    """The quick half: check the read and act on it. Mutates the case (and,
    when it confirms, the screening and watchlist cases the rules touch);
    the caller saves them."""
    record = dict(perceived, checks=[], reasons=[], rules=RULES_VERSION, sop=sop.version())
    values = {}
    if record["error"]:
        record["reasons"] = [f"AI Agent could not read the documents: {record['error']}"]
    else:
        case_documents.date_from_read(case, record["read"])
        record["checks"], values = checks(case, record["read"], scanned=record.get("ocr"))
        watch = watchlist_findings(case, wc, record["read"])
        if watch:
            record["watchlist"] = watch
            workflow._audit(case, ACTOR, "Watchlist finding", f"{WATCH_LABELS[watch['finding']]}: "
                            + " ".join(m["detail"] for m in watch["matches"]) + " The analyst clears the alert.")
        fixes = record_fixes(case, alert_case, record)
        if fixes:
            correct_record(case, record, fixes)
            record["checks"], values = checks(case, record["read"], scanned=record.get("ocr"))   # against the corrected record
        record["reasons"] = [c["note"] for c in record["checks"] if not c["ok"]]
    if not record["reasons"]:
        workflow._audit(case, ACTOR, "Documents read", f"{MODEL} read {DOCUMENTS_READ}; every check passed.")
        try:
            workflow.confirm_data(case, ACTOR, values, f"Confirmed by the AI Agent against {DOCUMENTS_READ}.",
                                  alert_case=alert_case, wc=wc)
        except WorkflowError as exc:
            record["reasons"] = [str(exc)]
        if not record["reasons"]:
            address = address_update(case, record["read"])
            if address:
                write_address(case, record, address)
    if record["reasons"]:
        ask = customer_request(case, alert_case, record)
        if ask:
            ask_customer(case, record, ask)
        else:
            record["decision"] = "human"
            record["resubmit"] = resubmit_reason(record["read"], record["checks"])
            case["screening"]["workflow"] = "human-in-the-loop"
            workflow._audit(case, ACTOR, "Sent to a person", "; ".join(record["reasons"]))
    else:
        record["decision"] = "verified" if case["status"] == "Verified" else "screening"
    if record.get("verdict"):
        attach_verdict(record, record["verdict"])            # shown and counted; the rules above decided
    case["ai_agent"] = record
    return record


# ── what the agent settles by itself ─────────────────────────────────────────
# The documents agree with one another and only the record differs, in a way
# a known slip explains: the agent corrects the record and confirms, or asks
# the customer for the one document that settles it. Never on a case with a
# watchlist alert left, one a person holds, or one with anything else wrong.
# An unreadable or covered ID stays with a person (what it hides may be the
# point), and so does any spelling the documents themselves disagree on.

NAME_CHECKS = {"Name", "Name on proof of address", "Name on secondary ID"}


def _may_act_alone(case, alert_case, record):
    """Every document read, no watchlist alert left, nobody holding the case,
    and the screening left nothing for an analyst."""
    return not (record.get("error") or record.get("read_version", 1) < READ_VERSION or record.get("watchlist")
                or case.get("current_reviewer") or case["status"] != "DataConfirming"
                or ai_screening.classify(case, alert_case) != "auto-verified")


def _failed(found):
    return {c["check"] for c in found if not c["ok"]}


def _foreign(word, pool):
    """A word on no document, nor a spelling of one that is."""
    return max((similarity.token_score(word, w) for w in pool), default=0.0) < 0.7


def _names_on(case, got):
    """The names the documents print beside the record's, as folded words,
    and whether the secondary ID and the proof of address name the passport's
    holder."""
    idr, sec, poa = got.get("id") or {}, got.get("secondary") or {}, got.get("poa") or {}
    country = case["client"]["nationality"]
    s, g = similarity.tokens(idr.get("surname", "")), similarity.tokens(idr.get("given_names", ""))
    poa_name = " ".join(x for x in (poa.get("surname", ""), poa.get("given_names", "")) if x)
    return {"surname": s, "given": g, "record": similarity.tokens(case["client"]["legal_name"]),
            "secondary": similarity.tokens(sec.get("name", "")), "poa": similarity.tokens(poa_name),
            "words": set(s + g) | set(similarity.tokens(sec.get("name", ""))) | set(similarity.tokens(poa_name)),
            "printed": " ".join(x for x in (idr.get("given_names", ""), idr.get("surname", "")) if x),
            "sec_holder": holder_named(sec.get("name", ""), idr.get("surname", ""), idr.get("given_names", ""), country),
            "poa_holder": bool(poa_name_verdict(" ".join(g + s), poa, country=country))}


def _only_names_failed(found):
    failed = _failed(found)
    return "Name" in failed and failed <= NAME_CHECKS


def _naturalisation_certificate(case, got):
    """Is the secondary ID a naturalisation certificate, as uploaded and as
    Claude read it?"""
    uploaded = (_secondary_doc(case) or {}).get("name", "")
    printed = (got.get("secondary") or {}).get("document_type", "")
    return all(re.search(r"NATURALI[SZ]", similarity.fold(t)) for t in (uploaded, printed))


def name_slip(case, got, found):
    """(the documents' name, why, the name the customer goes by) when the
    record's name is a known slip for the documents' holder, else None. Only
    the name checks failed, so the record's date of birth and ID number are
    the passport's, and the secondary ID and the proof of address name the
    passport's holder too:
    - the father's name: the record kept the passport's later given name and
      the surname and dropped the holder's own (PRAKASH MORE for MORE,
      MANASI PRAKASH: the record named her father);
    - a surname no document prints, beside the holder's given name (NHUNG VU
      for PHAN, BICH NHUNG: a married name written on the form);
    - a first name no document prints, beside the passport's surname, when
      the secondary ID is the naturalisation certificate (KEVIN NGUYEN for
      NGUYEN, KHOA QUOC: an anglicised name). The certificate is the document
      that settles it, and it is on the case: the record takes the legal name
      and keeps the typed one as the name the customer goes by, as the
      file's analysts did ("anglicised first name kept as preferred name").
    The name is written in the order the proof of address or the secondary
    ID prints it; the name gone by is "" for the first two."""
    if not _only_names_failed(found):
        return None
    n = _names_on(case, got)
    s, g, rec = n["surname"], n["given"], n["record"]
    if not (s and g and rec and n["sec_holder"] and n["poa_holder"]):
        return None
    kept = [t for t in rec if any(_given_match(t, b) >= 0.9 for b in g)]
    other = [t for t in rec if _foreign(t, n["words"])]
    first = [t for t in rec if t not in s]
    goes_by = ""
    if len(g) >= 2 and set(s) <= set(rec) <= set(s + g) and g[0] not in rec and set(rec) - set(s):
        father = " ".join(first)
        why = f"the record kept the father's name {father} and the surname, and dropped the holder's own name {g[0]}"
    elif not set(s) & set(rec) and kept and other and set(kept) | set(other) == set(rec):
        why = f"the record carried the surname {' '.join(other)}, which none of the documents prints (a married name on the form)"
    elif set(s) <= set(rec) and first and set(first) <= set(other) and _naturalisation_certificate(case, got):
        goes_by = case["client"]["legal_name"]
        why = (f"the record carried the first name {' '.join(first)}, which none of the documents prints (an anglicised "
               f"name); the naturalisation certificate on the case names the holder as the passport does, and "
               f"{goes_by} is kept as the name the customer goes by")
    else:
        return None
    order = next((t for t in (n["poa"], n["secondary"]) if sorted(t) == sorted(s + g)), g + s)
    return " ".join(order), why, goes_by


def name_link_request(case, got, found):
    """The RFI that asks for the document linking two names, when only a
    legal document can, else "": the passport and the proof of address in
    one surname and the secondary ID in the record's (a married name: the
    marriage certificate), or the record's surname with given names none of
    the documents prints (an anglicised name: the naturalisation
    certificate, asked for only when the secondary ID is something else;
    when it is the certificate, name_slip settles the name), or a record
    that keeps both surnames beside a secondary ID in the one the passport
    does not print (_second_surname: the marriage certificate)."""
    if _failed(found) == {"Name on secondary ID"}:
        return _second_surname_request(case, got)
    if not _only_names_failed(found) or name_slip(case, got, found):
        return ""
    n = _names_on(case, got)
    s, g, rec = n["surname"], n["given"], n["record"]
    if not (s and g and rec):
        return ""
    kept = {t for t in rec if any(_given_match(t, b) >= 0.9 for b in g)}
    rest = set(rec) - kept
    if not set(s) & set(rec) and kept and rest and rest <= set(n["secondary"]) and n["poa_holder"] and not n["sec_holder"]:
        document = "your marriage certificate"
    elif (set(s) <= set(rec) and set(rec) - set(s) and all(_foreign(t, n["words"]) for t in set(rec) - set(s))
          and n["sec_holder"] and n["poa_holder"]):
        document = "your naturalisation certificate"
    else:
        return ""
    return (f"Please send {document}, or another legal change-of-name document, that links the name on your passport "
            f"({n['printed']}) to the name on your account ({case['client']['legal_name']}).")


def _second_surname_request(case, got):
    """The RFI for the marriage certificate when only the secondary ID's name
    failed, for a surname the passport does not print that the record keeps
    beside the passport's (_second_surname), the secondary ID keeps the
    holder's given name and the proof of address names the passport's
    holder; else ""."""
    n = _names_on(case, got)
    printed = (got.get("secondary") or {}).get("name", "")
    if not (_second_surname(case["client"]["legal_name"], got.get("id") or {}, printed) and n["poa_holder"]
            and any(_given_match(t, b) >= 0.9 for t in n["secondary"] for b in n["given"])):
        return ""
    second = (_secondary_doc(case) or {}).get("name", "secondary ID")
    return (f"Please send your marriage certificate, or another legal change-of-name document, that links the name on "
            f"your passport ({n['printed']}) to the name on your {second} ({printed}).")


def _addresses_on(case, got):
    """(the record's, the proof of address's, the secondary ID's) addresses,
    normalised; the last is None when the secondary ID prints none."""
    country = case["client"]["nationality"]
    sec = ((got.get("secondary") or {}).get("address") or "").strip()
    return (addresses.normalise(case["client"].get("address", ""), country),
            addresses.normalise(_poa_address((got.get("poa") or {}).get("address", ""), country), country),
            addresses.normalise(sec, country) if sec else None)


def address_slip(case, got, found):
    """The documents' address, to correct the record to, when the record
    holds only a PO Box (not a home address) and the proof of address and
    the secondary ID give the same home in the same place, else ""."""
    if _failed(found) != {"Address"}:
        return ""
    reg, poa, sec = _addresses_on(case, got)
    if not reg["po_box"] or sec is None or addresses.compare(sec, poa) != "same" \
            or addresses.compare(reg, poa) != "same_locality":
        return ""
    return _as_the_record_keeps(_poa_address(got["poa"]["address"], case["client"]["nationality"]), case["client"]["nationality"])


def address_request(case, got, found):
    """The RFI that asks which home is the customer's, when the proof of
    address and the secondary ID give one address and the record another
    (a second home: only the customer can say which is the main one), else ""."""
    if _failed(found) != {"Address"}:
        return ""
    reg, poa, sec = _addresses_on(case, got)
    if reg["po_box"] or sec is None or addresses.compare(sec, poa) != "same" or addresses.compare(reg, poa) != "different":
        return ""
    country, held = case["client"]["nationality"], case["client"].get("address", "")
    shown = _as_the_record_keeps(_poa_address(got["poa"]["address"], country), country)
    second = (_secondary_doc(case) or {}).get("name", "secondary ID")
    return (f"Please confirm your main home address. Your proof of address and your {second} show {shown}; your account "
            f"holds {held}. If you have moved, reply to confirm the new address; if {held} is still your main home, "
            f"please send a proof of address for it dated within the last {sop.poa_age_text()}.")


def record_fixes(case, alert_case, record):
    """What the agent corrects on the record by itself, {field: (value,
    why)}: a name slip (name_slip, with the name the customer goes by when
    it keeps one) or a PO Box (address_slip), and only when every check
    passes once it is corrected; {} otherwise."""
    if not _may_act_alone(case, alert_case, record):
        return {}
    got, found = record["read"], record["checks"]
    slip = name_slip(case, got, found)
    address = "" if slip else address_slip(case, got, found)
    fixes = ({"legal_name": slip[:2]} if slip else
             {"address": (address, "the record held only a PO Box, which is not a home address; the proof of address and "
                                   "the secondary ID give the same home in the same place")} if address else {})
    if not fixes:
        return {}
    if slip and slip[2]:
        fixes["preferred_name"] = (slip[2], "the name the customer goes by")
    trial = dict(case, client=dict(case["client"], **{k: v for k, (v, _) in fixes.items()}))
    return fixes if all(c["ok"] for c in checks(trial, got)[0]) else {}


def write_name(case, record, name, why):
    """Correct the record's name to the documents'."""
    before = case["client"]["legal_name"]
    case["client"]["legal_name"] = name
    workflow._change(case, ACTOR, "name", before, name)
    workflow._audit(case, ACTOR, "Name corrected", f"Record name corrected to the documents' {name} (was {before}): {why}.")
    record["name_corrected"] = {"from": before, "to": name, "why": why}


def keep_preferred_name(case, record, name):
    """Keep the name the record carried, which the customer goes by, beside
    the legal name it now holds (write_name)."""
    before = case["client"].get("preferred_name", "")
    case["client"]["preferred_name"] = name
    workflow._change(case, ACTOR, "preferred name", before, name)
    record["name_corrected"]["preferred"] = name


def correct_record(case, record, fixes):
    """Write the corrections. Claude's verdict was on the record before them:
    it is dropped, and asked again on the corrected record from the stored
    read (needs_verdict)."""
    for field, (value, why) in fixes.items():
        if field == "legal_name":
            write_name(case, record, value, why)
        elif field == "preferred_name":
            keep_preferred_name(case, record, value)
        else:
            write_address(case, record, value, why)
    record.pop("verdict", None)


def customer_request(case, alert_case, record):
    """What the agent asks the customer for by itself, {"kind", "text"}, or
    None: Resubmit POA when the proof of address is older than six months
    and that is the only thing wrong, or an RFI for the document that links
    two names (name_link_request) or for which home is the main one
    (address_request). A request approves nothing; what comes back is
    checked again."""
    if not _may_act_alone(case, alert_case, record):
        return None
    got, found = record["read"], record["checks"]
    failed = [c for c in found if not c["ok"]]
    if len(failed) == 1 and failed[0]["check"] == "Proof of address checks" \
            and set(failed[0].get("failure_keys") or ()) == {"poa_age"}:
        return {"kind": "resubmit", "text": sop.poa_old_reason()}
    text = name_link_request(case, got, found) or address_request(case, got, found)
    return {"kind": "rfi", "text": text} if text else None


def ask_customer(case, record, ask):
    """Send the request through the analysts' own path (Resubmit POA, or an
    RFI): the case waits for the customer, and nothing is left for an
    analyst."""
    if ask["kind"] == "resubmit":
        workflow.request_resubmission(case, ACTOR, ["Resubmit POA"], "", ask["text"],
                                      "Requested by the AI Agent: " + "; ".join(record["reasons"]))
    else:
        workflow.send_rfi(case, ACTOR, ask["text"])
    record.update(decision=ask["kind"], requested=ask["text"], resubmit="")
    case["screening"]["workflow"] = "auto-verified"


def needs_recheck(case):
    """A case the agent sent to a person under older checks, still in Data
    Confirming, and untouched since: no one reserved it or acted on it."""
    rec = case.get("ai_agent") or {}
    return (rec.get("decision") == "human" and bool(rec.get("read")) and not rec.get("error")
            and rec.get("rules", 1) != RULES_VERSION and case["status"] == "DataConfirming"
            and not case.get("current_reviewer")
            and all(e.get("actor") == ACTOR for e in case.get("audit", []) if e.get("at", "") >= rec.get("at", "")))


def _untouched(case):
    """No person has reserved or acted on the case since the agent's read."""
    rec = case.get("ai_agent") or {}
    return not case.get("current_reviewer") and all(
        e.get("actor") in (ACTOR, vocab.SYSTEM_USER) for e in case.get("audit", []) if e.get("at", "") >= rec.get("at", ""))


def needs_reread(case):
    """A case read before the checks wanted the proof of address's name, whose
    readable proof of address gave none (the read was not asked for it), and
    no person has touched since: it starts again and is read again
    (app._load_seed), rather than sent to a person for a name it printed."""
    rec = case.get("ai_agent") or {}
    poa = (rec.get("read") or {}).get("poa") or {}
    return (bool(case.get("poa_required")) and rec.get("rules", 1) < POA_NAME_RULES and bool(poa.get("readable"))
            and not (poa.get("surname") or poa.get("given_names")) and _untouched(case))


def confirmed_on_older_names(stored, seed):
    """A case the agent confirmed before the last change to the name checks
    (NAME_RULES), whose stored read the name checks (the ID's, the secondary
    ID's, the proof of address's) now refuse against the record as the file
    gave it (`seed`: the confirmation rewrote the name), and no person has
    touched since: it starts again and is decided again on its stored read
    (app._load_seed, redecide). A record the agent corrected itself
    (name_slip) was checked on the corrected name, and is left alone."""
    rec = stored.get("ai_agent") or {}
    if (rec.get("decision") not in ("verified", "screening") or rec.get("rules", 1) >= NAME_RULES
            or rec.get("name_corrected") or not rec.get("read") or not _untouched(stored)):
        return False
    found, _ = checks(seed, rec["read"])
    return any(not c["ok"] for c in found if c["check"] in NAME_CHECKS)


def asked_on_older_names(stored, seed):
    """A case the agent asked the customer about a name (an RFI) before the
    last change to the name rules (NAME_RULES), whose stored read they now
    settle on the record as the file gave it (`seed`) as a slip (name_slip:
    the naturalisation certificate it asked for is on the case), and no
    person holds or has worked (an RFI decided again on an earlier restart
    carries its seed's intake rows, dated after the read, so _untouched would
    not do): it starts again and is decided again on its stored read
    (app._load_seed, redecide)."""
    rec = stored.get("ai_agent") or {}
    if (rec.get("decision") != "rfi" or rec.get("rules", 1) >= NAME_RULES or not rec.get("read")
            or stored.get("current_reviewer") or _worked_by_a_person(stored)):
        return False
    found, _ = checks(seed, rec["read"])
    return bool(name_slip(seed, rec["read"], found))


def address_written_otherwise(stored, seed):
    """A case the agent confirmed, and whose registered address it rewrote to
    the proof of address's (address_update), before the last change to that
    rule (ADDRESS_RULES), where the rule now writes another address or none
    (`seed`: the record as the file gave it), and no person has touched
    since: it starts again and is decided again on its stored read. An
    address a record fix wrote (address_slip: the two did not match) is
    left alone."""
    rec = stored.get("ai_agent") or {}
    done = rec.get("address_updated")
    if (not done or rec.get("decision") not in ("verified", "screening") or rec.get("rules", 1) >= ADDRESS_RULES
            or not rec.get("read") or not _untouched(stored)):
        return False
    country = seed["client"]["nationality"]
    poa = _poa_address((rec["read"].get("poa") or {}).get("address", ""), country)
    if addresses.compare(addresses.normalise(done.get("from", ""), country), addresses.normalise(poa, country)) != "same":
        return False
    return address_update(seed, rec["read"]) != done.get("to", "")


def redecide(case, alert_case, wc, stored):
    """A case back on its seed (confirmed_on_older_names,
    asked_on_older_names, address_written_otherwise), decided again on its
    stored read: the same look at the images, today's checks. Claude's
    verdict is asked again (needs_verdict): the policy it is given may have
    changed with the checks."""
    workflow._audit(case, ACTOR, "Checked again", f"The agent's checks changed; its read of {stored.get('at', '')} is decided again.")
    return decide(case, alert_case, wc, {k: v for k, v in stored.items() if k in PERCEIVED and k != "verdict"})


def read_before_all_documents(stored):
    """A case the agent decided on a read of the passport's data page and the
    proof of address only (READ_VERSION 1), that no person has touched since:
    it starts again and is read again from every document (app._load_seed)."""
    rec = stored.get("ai_agent") or {}
    return bool(rec) and rec.get("read_version", 1) < READ_VERSION and _untouched(stored)


def needs_address_update(case):
    """A case the agent confirmed before it wrote the verified address back,
    whose record keeps an address its proof of address corrects, and no
    person has touched since (update_address)."""
    rec = case.get("ai_agent") or {}
    return (rec.get("decision") in ("verified", "screening") and rec.get("rules", 1) < RULES_VERSION
            and bool(rec.get("read")) and not rec.get("address_updated") and _untouched(case)
            and bool(address_update(case, rec["read"])))


def update_address(case):
    """The address write-back on a case confirmed before it existed: no new
    look at the images; the record's checks are now this version's."""
    rec = case["ai_agent"]
    write_address(case, rec, address_update(case, rec["read"]))
    rec["rules"] = RULES_VERSION
    return rec


def recheck(case, alert_case, wc):
    """The current checks on the stored read, with no new look at the images.
    A case that now passes them is confirmed as decide() confirms one; a case
    that still fails stays with a person, its reasons brought up to date."""
    old = case["ai_agent"]
    found, _ = checks(case, old["read"], scanned=old.get("ocr"))
    failing = any(not c["ok"] for c in found)
    if failing and not record_fixes(case, alert_case, dict(old, checks=found)):
        old.update(checks=found, reasons=[c["note"] for c in found if not c["ok"]], rules=RULES_VERSION, sop=sop.version(),
                   resubmit=resubmit_reason(old["read"], found))
        ask = customer_request(case, alert_case, old)
        if ask:
            ask_customer(case, old, ask)
        return old
    workflow._audit(case, ACTOR, "Checked again", "The agent's checks changed; the stored read now passes every one"
                    + (" once the record's slip is corrected." if failing else "."))
    case["screening"]["workflow"] = "auto-verified"
    return decide(case, alert_case, wc, {k: v for k, v in old.items() if k in PERCEIVED})


def needs_sop_recheck(case):
    """A case the agent decided under other SOP values (sop.version) and no
    person has touched since its read: one it left with a person in Data
    Confirming, or one waiting for the new proof of address it asked for."""
    rec = case.get("ai_agent") or {}
    if rec.get("sop", 0) == sop.version() or not rec.get("read") or rec.get("error"):
        return False
    waiting = rec.get("decision") == "resubmit" and case["status"] == "ResubmitPOA"
    with_a_person = rec.get("decision") == "human" and case["status"] == "DataConfirming"
    return (waiting or with_a_person) and _untouched(case)


def sop_recheck(case, alert_case, wc):
    """The SOP's current values on the stored read (no new look at the
    images). A case waiting for a new proof of address comes back and is
    decided when the one it holds now passes (a longer age limit); when it
    still fails, the request to the customer stands. A case with a person is
    checked again as recheck() does: confirmed when it now passes, its
    reasons brought up to date when not."""
    rec = case["ai_agent"]
    if rec["decision"] == "resubmit":
        found, _ = checks(case, rec["read"], scanned=rec.get("ocr"))
        if any(not c["ok"] for c in found):
            rec["sop"] = sop.version()
            return rec
        case["status"] = "DataConfirming"
        case["pending_reason"] = ""
        workflow._audit(case, ACTOR, "Checked again", "The SOP rules changed: the proof of address the customer was asked "
                        "to replace passes them now, so the request is withdrawn.")
        return decide(case, alert_case, wc, {k: v for k, v in rec.items() if k in PERCEIVED})
    recheck(case, alert_case, wc)
    case["ai_agent"]["sop"] = sop.version()
    return case["ai_agent"]


def run_case(case, alert_case, wc, render, call=None, sleep=time.sleep):
    """Read, check, decide - one case, start to finish."""
    return decide(case, alert_case, wc, perceive(case, render, call=call, sleep=sleep, wc=wc))
