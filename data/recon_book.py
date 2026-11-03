"""The console's case book from the reconciliation log.

The 1,000 rows of fincrime/filebook/data/kyc_wc_recon_log.csv (the file the user
uploaded; every person and company in it is fictional) become the console's
open onboarding cases: each row is one KYC case at L1 Data Confirming with
its one World-Check record, a screening case and a watchlist case, built by
the same seed builders as the scripted scenarios. The AI screening rules run
at intake exactly as they do for those (seed.pair_screening).

Parsing is the file book's (fincrime/filebook/loader.py, parse.py: Flask-free, no
state) and so are the document images (fincrime/filebook/documents.py). The file
has no images; the defect plan is synthetic and the case page says so.

What an analyst recorded for the row (disposition, notes, variance types,
handling time) rides along as case["recon"]. Nothing in the console's logic
reads it: it is shown on the case page, and the AI Screening page measures
the rules against it.
"""
import csv
import hashlib
import io
import re
from datetime import date, timedelta

from fincrime.data import vocab
from fincrime.engines.errors import WorkflowError
from fincrime.data.seed import AMARA, JON, MARIO, agency_info, kyc_case, pair_screening, wc_match
from fincrime.filebook import documents as file_documents
from fincrime.filebook import loader as file_loader
from fincrime.filebook import names as file_names
from fincrime.utils import dates

KEY_PREFIX = "recon-"

# Analyst-facing words for the synthetic capture defects: the console's
# document quality badge and what the ID-verification vendor said. The vendor
# declines only what it detects; a phone lying on the page, a shot at an
# angle or a cracked card passes it (as real uploads did), and only the AI
# Agent's look at the image stops them.
QUALITY = {"flat_scan": "scan", "partial": "cropped", "blur": "blurred", "glare": "glare", "expired": "expired",
           "obstructed": "covered", "angled": "angled", "damaged": "damaged"}
VENDOR_REASON = {
    "flat_scan": "The image is a flat scan or PDF of the document, not a camera photo.",
    "partial": "Document edges not visible.",
    "blur": "The image is too blurred to read the data page.",
    "glare": "Glare covers part of the data page.",
    "expired": "The passport has expired.",
    "tampered": "Security-feature check failed: the date-of-birth check digit in the MRZ does not match.",
}

# Proof-of-address kinds: the file book's parse.poa_type → the console's library
# (vocab.ACCEPTED_POA_TYPES); and the country an overseas POA was issued in.
POA_KIND = {"utility_bill": "utility_bill", "bank_statement": "bank_statement", "lease": "tenancy_agreement",
            "aadhaar": "aadhaar", "residence_confirmation": "residence_confirmation", "military_orders": "military_orders"}
POA_COUNTRY = (("Dubai", "AE"), ("Abu Dhabi", "AE"), ("Canada", "CA"), ("Qatar", "QA"), ("Oman", "OM"),
               ("Singapore", "SG"), ("Australia", "AU"))
SECONDARY = {"national_id": ("national_id", "NationalID"), "driving_licence": ("driving_licence", "DrivingLicense"),
             "certificate": ("national_id", "Certificate")}

# A company's registration document, from its name's suffix and its country.
LEGAL_FORM = (("Pvt Ltd", "Private Limited Company"), ("LLP", "Limited Liability Partnership"),
              ("LLC", "Limited Liability Company"), ("Inc", "Corporation"), ("JSC", "Joint Stock Company"),
              ("Co., Ltd", "Limited Liability Company"))
ACTIVITY = (("Agro Exports", "Export of agricultural produce"), ("Home Health", "Home health care services"),
            ("Construction", "Construction and civil works"), ("Logistics", "Freight forwarding and logistics"),
            ("Textiles", "Manufacture and trade of textiles"), ("Capital Partners", "Investment advisory and private capital"),
            ("Pharma Distributors", "Wholesale distribution of pharmaceuticals"),
            ("Infra Projects", "Infrastructure project contracting"),
            ("Precision Components", "Manufacture of precision engineering components"),
            ("Gems & Jewellery", "Trade in gems and jewellery"), ("Seafood", "Seafood processing and export"),
            ("Garment", "Garment manufacturing"), ("Import Export", "Import and export trading"),
            ("Imports", "Import and wholesale trade"), ("Auto Parts", "Wholesale of auto parts"),
            ("Trading", "General trading"))
IN_STATES = (("Tamil Nadu", "TN"), ("West Bengal", "WB"), ("Kolkata", "WB"), ("Maharashtra", "MH"), ("Gujarat", "GJ"),
             ("Uttar Pradesh", "UP"), ("Karnataka", "KA"), ("Kerala", "KL"), ("Andhra Pradesh", "AP"), ("Telangana", "TG"),
             ("Madhya Pradesh", "MP"), ("Delhi", "DL"))

# Showcase cases routed at intake, below capacity so "Request cases" still
# has room (checked by tests/test_console_book.py):
#   Amara  3011 AI-cleared, auto-verifies on confirm · 3012 exact DOB, swapped name fields, a true match
#          3146 garbled (mojibake) CRM name, AI-cleared
#   Jon    3046 day/month-transposed DOB, held · 3029 a company (KYB) · 3037 proof of address 223 days old
#   Mario  3118 flat scan, resubmit the ID · 3043 year-only DOB, held · 3040 passport fails its MRZ check digit
STARTERS = {
    "KYC-2026-3011": AMARA, "KYC-2026-3012": AMARA, "KYC-2026-3146": AMARA,
    "KYC-2026-3046": JON, "KYC-2026-3029": JON, "KYC-2026-3037": JON,
    "KYC-2026-3118": MARIO, "KYC-2026-3043": MARIO, "KYC-2026-3040": MARIO,
}

# The file-book view of each case (build_case + synthesise), kept for the
# document images; rebuilt by every build().
VIEWS = {}


def _h(seed, case_id):
    return int(hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest(), 16)


# ── documents ────────────────────────────────────────────────────────────────

def _poa_country(poa, home):
    for word, code in POA_COUNTRY:
        if word in poa["type"]:
            return code
    tail = poa["address"].rsplit(",", 1)[-1].strip().upper()
    if tail in ("USA", "U.S.A.", "UNITED STATES"):
        return "US"
    return home


def _docs(v, company):
    s, d = v["synth"], v["documents"]
    defect = QUALITY.get(s["capture"], "ok")
    sec, poa = d["secondary"], d["poa"]
    sec_kind, sec_category = SECONDARY.get(sec["kind"], ("national_id", "NationalID"))
    out = []
    if company:
        out.append({"name": company["document_title"].capitalize(), "category": "TradeLicense", "kind": "trade_licence",
                    "side": "page 1", "quality": "ok"})
    out += [
        {"name": "Passport", "category": "Passport", "kind": "passport", "side": "front", "quality": defect,
         "hub_file": "passport"},
        {"name": "Passport", "category": "Passport", "kind": "passport", "side": "back", "quality": "ok",
         "hub_file": "passport-back"},
        {"name": sec["type"], "category": sec_category, "kind": sec_kind, "side": "front", "quality": "ok",
         "hub_file": "secondary"},
        {"name": f"Proof of address — {poa['type']}", "category": "ProofOfAddress", "kind": POA_KIND[poa["kind"]],
         "side": "page 1", "quality": "ok", "hub_file": "poa", "issued_days_ago": s["poa_age_days"],
         "country": _poa_country(poa, v["country"])},
    ]
    return out


def view(case_id):
    """The file-book record the document images are drawn from, with the
    canonical name the proof of address prints (names.analyse)
    (worked out on first use: names.analyse is the costly step)."""
    v = VIEWS.get(case_id)
    if v is None:
        return None
    if not v["identity"]:
        pp = v["documents"]["passport"]
        v["identity"] = {"name": file_names.analyse(v["country"], v["customer"]["name"], pp["viz"], pp["mrz"],
                                                   v["documents"]["secondary"]["name"], v["customer"]["corporate"],
                                                   v["customer"]["ubo"])}
    return v


def render(case, doc):
    """The SVG for a console document of a file-backed case, or None."""
    v = view((case.get("recon") or {}).get("id", ""))
    return file_documents.render(v, doc["hub_file"]) if v and doc.get("hub_file") else None


# ── the company (KYB) ───────────────────────────────────────────────────────

def _company(v):
    name, country, cid = v["customer"]["name"], v["country"], v["id"]
    h = _h("recon-reg", cid)
    form = next((f for suffix, f in LEGAL_FORM if name.endswith(suffix)), "Company")
    activity = next((a for word, a in ACTIVITY if word in name), "General business")
    year = 2008 + h % 16
    if country == "US":
        state = (re.search(r",\s*([A-Z]{2})\s+\d{5}", v["customer"]["address"]) or [None, ""])[1]
        number = f"{h % 90 + 10}-{(h >> 8) % 10 ** 7:07d}"
        title = "CERTIFICATE OF FORMATION" if form == "Limited Liability Company" else "CERTIFICATE OF INCORPORATION"
        authority = f"SECRETARY OF STATE{' - ' + state if state else ''} (DEMO)"
    elif country == "IN":
        state = next((code for word, code in IN_STATES if word in v["customer"]["address"]), "MH")
        if form == "Limited Liability Partnership":
            number = f"AA{chr(65 + h % 26)}-{(h >> 8) % 10 ** 4:04d}"
            title = "CERTIFICATE OF REGISTRATION"
        else:
            number = f"U{(h >> 8) % 10 ** 5:05d}{state}{year}PTC{(h >> 32) % 10 ** 6:06d}"
            title = "CERTIFICATE OF INCORPORATION"
        authority = "MINISTRY OF CORPORATE AFFAIRS - REGISTRAR OF COMPANIES (DEMO)"
    else:
        number = f"0{(h >> 8) % 10 ** 9:09d}"
        title = "ENTERPRISE REGISTRATION CERTIFICATE"
        authority = "DEPARTMENT OF PLANNING AND INVESTMENT (DEMO)"
    return {"registration_number": number, "legal_form": form, "activity": activity, "document_title": title,
            "authority": authority}


# ── the World-Check record ──────────────────────────────────────────────────

def _category(w):
    """(types, category, sub_category, pep_status, special_interest, position)
    from the file's category text, e.g. 'PEP - Level 2 (State Legislator)'."""
    group, text = w["group"], w["category"]
    head, _, tail = text.partition(" - ")
    paren = re.search(r"\(([^()]*)\)", text)
    inner = paren.group(1) if paren else ""
    if group == "PEP":
        rca = "Relative" in text
        return (("RCA",) if rca else ("PEP",)), "PEP", tail, ("RCA" if rca else tail.split(" (")[0]), [], inner
    if group == "Adverse Media":
        return ("SIC",), "ADVERSE MEDIA", tail, "", [tail], ""
    code = {"Law Enforcement": "LE", "Regulatory Enforcement": "RE", "Sanctions": "SAN"}.get(group, "SIC")
    return (code,), group.upper(), inner or tail, "", [], ""


def _dobs(dob):
    """The record's date of birth the way the rules and the console read it:
    a full date, a bare year, a range (which the rules cannot read, so it
    stays with an analyst) or nothing."""
    if dob["kind"] == "full":
        return [dob["date"]]
    if dob["kind"] == "year":
        return [str(dob["year"])]
    if dob["kind"] == "range":
        return [f"{dob['year']}–{dob['year_to']}"]
    return []


def wc_places(v):
    """The record's listed places as its World-Check match keeps them: the
    country line ("Singapore; Bangladesh", each country once, the first
    place's first, as the console compares a client's country against it)
    and one location row per place."""
    w = v["wc"]
    rows = [{"street": "", "city": p["city"], "region": p["region"], "country": p["country"] or v["country_name"], "postcode": ""}
            for p in (w["location"].get("places") or [w["location"]])]
    return "; ".join(dict.fromkeys(r["country"] for r in rows)), rows


def _match(v, key):
    w = v["wc"]
    types, category, sub, pep, special, position = _category(w)
    country, locations = wc_places(v)
    h = _h("recon-wc", v["id"])
    entered = date(2012, 1, 1) + timedelta(days=h % 4000)
    updated = min(entered + timedelta(days=30 + (h >> 16) % 1500), date(2026, 9, 30))
    name = w["surname"] if (w["mononym"] or not w["given"]) else f"{w['surname']}, {w['given']}"
    m = wc_match(
        key=f"{key}-1", wc_id=w["uid"], name=name, strength=w["score"] / 100, types=types, gender="",
        dobs=_dobs(w["dob"]),
        citizenship=v["country_name"] if "Nationality" in w["matched_attributes"] else locations[0]["country"],
        country=country, category=category, sub_category=sub, pep_status=pep, special_interest=special, position=position,
        location=locations[0], bio=w["narrative"], aliases=w["aka"], entered=entered.isoformat(), updated=updated.isoformat(),
    )
    m["key_data"]["locations"] = locations
    return m


# ── one row → three records ─────────────────────────────────────────────────

def _recorded(v):
    """What the file says an analyst did with the row: for comparison only."""
    hist, w = v["history"], v["wc"]
    return {"id": v["id"], "wc_uid": w["uid"], "disposition": hist["disposition"], "outcome": hist["outcome"],
            "action": hist["action"], "corrected": hist["corrected"], "analyst": hist["analyst"],
            "review_date": hist["review_date"], "notes": hist["notes"], "discrepancy": hist["discrepancy"],
            "matched_attributes": w["matched_attributes"], "category": w["category"], "wc_dob": w["dob"]["raw"],
            "jurisdiction": w["location"]["raw"], "name_variance": hist["name_variance"],
            "address_variance": hist["address_variance"], "pain_point": hist["pain_point"],
            "resolution": hist["resolution"], "impact": hist["impact"], "score": w["score"],
            "score_after": hist["score_after"], "minutes": hist["minutes"]}


def case_records(v, routed_to=""):
    """(kyc, alert, wc) for one file-book view."""
    key = (UPLOAD_PREFIX + re.sub(r"[^a-z0-9]+", "-", v["id"].lower()).strip("-") if v.get("uploaded")
           else KEY_PREFIX + v["id"].rsplit("-", 1)[-1])
    cust, s, pp = v["customer"], v["synth"], v["documents"]["passport"]
    nat = v["country"]
    received = dates.parse(s["received_at"]) or dates.BOOT
    hours = max(0.25, (dates.BOOT - received).total_seconds() / 3600)
    defect = s["capture"] if s["capture"] != "none" else ("tampered" if s["tampered"] else "")
    declined = defect in VENDOR_REASON
    company = _company(v) if cust["corporate"] else None
    common = dict(
        key=key, case_id=v["id"], nationality=nat, issuing=nat, status="DataConfirming", us_person=nat == "US",
        tags=(["Priority Review"] if cust["risk"] == "High" else []) + (["Uploaded"] if v.get("uploaded") else []),
        poa_required=True,
        manual_review_reason=["Namescreen hit - WC or CMS"] + (["Vendor rejected"] if declined else []),
        created_hours_ago=hours, vendor=vocab.VENDORS[_h("recon-vendor", v["id"]) % len(vocab.VENDORS)],
        vendor_result="Declined" if declined else "Approved", vendor_reason=VENDOR_REASON.get(defect, ""),
        docs=_docs(v, company), address=cust["address"], country=v["country_name"], screening_hit=True,
        risk_rating=cust["risk"], routed_to=routed_to,
    )
    if company:
        kyc = kyc_case(name=cust["name"], dob="", tenant="ttshop", id_type="TradeLicense", gender="",
                       id_number=company["registration_number"],
                       agency=dict(agency_info(company=cust["name"], registration_number=company["registration_number"],
                                               incorporation_country=v["country_name"], address=cust["address"],
                                               representative=cust["ubo"], ubos=[(cust["ubo"], 100, nat)],
                                               onboarding_time=dates.hours_ago(hours)),
                                   legal_form=company["legal_form"], activity=company["activity"],
                                   document_title=company["document_title"], authority=company["authority"]),
                       **common)
    else:
        viz = pp["viz"]
        printed = f"{viz['surname']} {viz['given']}".strip()
        kyc = kyc_case(name=cust["name"], dob=cust["dob"], id_type="Passport", id_number=pp["number"],
                       native_name=printed if any(ord(ch) > 127 for ch in printed) else "", gender=s["sex"], **common)
    mrz = pp["mrz"]
    kyc["vendor_check"]["extracted"].update(name=" ".join(mrz["primary_tokens"] + mrz["secondary"]), dob=cust["dob"],
                                            id_number=pp["number"], id_type="Passport", expiry=s["passport_expiry"])
    # drawn: when this case's ID or proof-of-address image was last redrawn (filebook documents.REDRAWN,
    # POA_REDRAWN, AADHAAR_NAMED), "" when never
    kyc["recon"] = dict(_recorded(v), uploaded=bool(v.get("uploaded")), drawn=s.get("drawn", ""))
    alert, wc = pair_screening(kyc, key, [_match(v, key)])
    if v.get("awaiting_documents") and not company:          # a company's documents stay with the analysts
        _awaiting(kyc, v)
    return kyc, alert, wc


def _awaiting(kyc, v):
    """An uploaded row without its documents' contents: the case waits for
    its real images (case_documents.attach). Nothing is drawn in the slots,
    no ID vendor has seen an image, and a slot's type is only what the row
    names (an unnamed proof of address gets its type when its image comes)."""
    kyc["awaiting_documents"] = True
    kyc["pending_reason"] = AWAITING
    kyc["vendor_check"].update(result="Not run", reason="", extracted={},
                               vendor_reason="No ID image yet: the case waits for its documents.")
    raw = v.get("raw_types", {})
    for d in kyc["documents"]:
        if not d.get("hub_file"):
            continue
        d.update(awaiting=True, quality="ok")
        if d["hub_file"] == "secondary" and not raw.get("secondary_id_type"):
            d["name"] = "Secondary ID"
        if d["hub_file"] == "poa":
            d.pop("issued_on", None)
            if not raw.get("proof_of_address_type"):
                d.update(name="Proof of address", kind="")


def build():
    """(kyc_cases, alert_cases, wc_cases) for the whole file."""
    VIEWS.clear()
    kyc, alerts, wcs = [], [], []
    for seq, row in enumerate(file_loader.read_rows(), start=1):
        v = file_documents.synthesise(file_loader.build_case(row, seq))
        v.pop("raw", None)
        VIEWS[v["id"]] = v
        k, a, w = case_records(v, STARTERS.get(v["id"], ""))
        kyc.append(k)
        alerts.append(a)
        wcs.append(w)
    return kyc, alerts, wcs


# ── cases uploaded in the console ────────────────────────────────────────────
# A new onboarding case has no outcome yet, so an upload needs the log's
# columns up to what the analyst decided; those history columns may be
# absent or empty. The rows then go through the same path as the file's.
UPLOAD_PREFIX = "upload-"
HISTORY_COLUMNS = ("discrepancy_analysis_dob_address", "case_analyst", "review_date", "disposition_status",
                   "compliance_action_regulatory_notes", "name_variance_type", "address_variance_type",
                   "pain_point_detail", "normalisation_resolution_applied", "screening_impact",
                   "match_score_after_normalisation", "analyst_handling_time_mins")
# What a row prints on its documents, the images being drawn from it. Optional
# on upload: a row with all of them empty is a case whose real images come as
# files (the ZIP beside the CSV, or Upload images on the case), and it waits
# for them. The document types (secondary_id_type, proof_of_address_type)
# stay optional too; given, they name the slots.
DOCUMENT_COLUMNS = ("name_passport_data_page_viz", "name_passport_mrz_line_1", "name_secondary_id", "address_passport",
                    "address_secondary_id", "address_proof_of_address")
TYPE_COLUMNS = ("secondary_id_type", "proof_of_address_type")
UPLOAD_COLUMNS = tuple(k for k in file_loader.REQUIRED if k not in HISTORY_COLUMNS + DOCUMENT_COLUMNS + TYPE_COLUMNS)
UPLOAD_MAX_ROWS = 500
AWAITING = "Awaiting Documents"


def read_upload(text):
    """The rows of an uploaded CSV, keyed like the log's. Refuses a file
    without the columns a case needs, or with more rows than one upload takes."""
    reader = csv.DictReader(io.StringIO(text))
    keys = {file_loader.parse.header_key(h) for h in (reader.fieldnames or [])}
    missing = [k for k in UPLOAD_COLUMNS if k not in keys]
    if missing:
        raise WorkflowError(f"The file lacks {len(missing)} column(s) a case needs: {', '.join(missing[:6])}"
                            + (" …" if len(missing) > 6 else "") + ". Download the sample for the layout.")
    rows = []
    for raw in reader:
        row = {file_loader.parse.header_key(h): (v or "").strip() for h, v in raw.items() if h is not None}
        if not any(row.values()):
            continue
        for k in file_loader.REQUIRED:
            row.setdefault(k, "")
        rows.append(row)
    if not rows:
        raise WorkflowError("The file has no case rows.")
    if len(rows) > UPLOAD_MAX_ROWS:
        raise WorkflowError(f"One upload takes at most {UPLOAD_MAX_ROWS} cases; the file has {len(rows)}.")
    return rows


def add_rows(rows, start_seq):
    """(kyc, alert, wc) triples for uploaded rows, screened at intake like the
    file's; a row that cannot be built is returned as (row number, reason)."""
    built, failed = [], []
    for i, row in enumerate(rows):
        try:
            v = file_documents.synthesise(file_loader.build_case(row, start_seq + i))
            v.pop("raw", None)
            v["uploaded"] = True
            v["awaiting_documents"] = not any(row.get(k) for k in DOCUMENT_COLUMNS)
            v["raw_types"] = {k: row.get(k, "") for k in TYPE_COLUMNS}
            VIEWS[v["id"]] = v
            built.append(case_records(v))
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            failed.append((i + 2, f"{type(exc).__name__}: {exc}"))          # +2: the header is line 1
    return built, failed


def sample_rows(first_number, taken=()):
    """A sample upload: rows of the log under new ids with the history
    columns blanked, chosen to show each path: two the AI Agent should verify
    by itself, one whose CRM spells the surname another way than the
    documents, which disagree among themselves too (it goes to an analyst:
    no slip the agent settles), an exact date-of-birth hit and a company (both the analysts'
    from the start). Ids are numbered from `first_number`, skipping `taken`
    and any id whose drawn capture defects would spoil the first three."""
    from fincrime.engines import ai_agent                    # late: the engines import the seed
    from fincrime.filebook import addresses
    rows = file_loader.read_rows()
    picks = []
    for want in ("clean", "clean", "name", "exact", "company"):
        for seq, row in enumerate(rows, start=1):
            if row in picks:
                continue
            v = file_loader.build_case(row, seq)
            corp, exact = v["customer"]["corporate"], "Exact" in row["world_check_listed_dob"]
            dob_conflict = v["wc"]["dob"].get("kind") == "full" and not exact and "transposed" not in row["world_check_listed_dob"]
            viz = v["documents"]["passport"]["viz"]
            same_name = ai_agent.name_verdict(v["customer"]["name"], viz["surname"], viz["given"])[0]
            home = addresses.compare(addresses.normalise(v["customer"]["address"], v["country"]),
                                     addresses.normalise(v["documents"]["poa"]["address"], v["country"])) == "same"
            ok = {"clean": not corp and dob_conflict and same_name and home,
                  "name": not corp and dob_conflict and not same_name and viz["surname"]
                          and row.get("name_variance_type", "").startswith("Transliteration"),
                  "exact": not corp and exact, "company": corp}[want]
            if ok:
                picks.append(row)
                break
    out, n = [], first_number
    for want, row in zip(("clean", "clean", "name", "exact", "company"), picks):
        while True:
            new_id = f"KYC-2026-{n}"
            n += 1
            if new_id in taken:
                continue
            s_ = file_documents.synthesise(file_loader.build_case(dict(row, kyc_case_id=new_id), 0))["synth"]
            # capture defects are drawn per case id: the cases meant to reach the
            # agent's decision get an id whose documents carry none
            if want in ("exact", "company") or (s_["capture"] == "none" and not s_["tampered"] and s_["poa_age_days"] <= 183):
                break
        r = {k: ("" if k in HISTORY_COLUMNS else row[k]) for k in UPLOAD_COLUMNS + DOCUMENT_COLUMNS + TYPE_COLUMNS + HISTORY_COLUMNS}
        r["kyc_case_id"] = new_id
        out.append(r)
    return out


def sample_images(rows):
    """A ZIP of JPEG photos of the sample rows' documents, named for the
    image-first upload (<case id>_passport.jpg, _passport_back, _secondary,
    _poa): the images drawn from each row (fictional, marked SPECIMEN), as a
    batch of real ID photos would come. A company has none: its documents
    stay with the analysts."""
    import zipfile
    from PIL import Image
    from fincrime.documents.raster import svg_to_png
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for row in rows:
            v = file_documents.synthesise(file_loader.build_case(row, 0))
            if v["customer"]["corporate"]:
                continue
            for hub, slot in (("passport", "passport"), ("passport-back", "passport_back"), ("secondary", "secondary"), ("poa", "poa")):
                with Image.open(io.BytesIO(svg_to_png(file_documents.render(v, hub)))) as im:
                    photo = io.BytesIO()
                    im.convert("RGB").save(photo, "JPEG", quality=90)
                z.writestr(f"{row['kyc_case_id']}_{slot}.jpg", photo.getvalue())
    return buf.getvalue()
