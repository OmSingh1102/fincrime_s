"""Parsers for the reconciliation log's text fields.

Every function here turns one raw cell into structure and judges nothing.
The log's World-Check date-of-birth cell carries an annotation in
parentheses ("(Exact Match)", "(34 yrs diff)"); the parser keeps it as
`annotation`, and the screening rules never look at it - they work from the
date itself.
"""
import re
import unicodedata

# ── headers ──────────────────────────────────────────────────────────────────

def header_key(label: str) -> str:
    """'Name – Passport Data Page (VIZ)' → 'name_passport_data_page_viz'.
    En and em dashes become hyphens before everything non-alphanumeric
    collapses to one underscore."""
    text = unicodedata.normalize("NFKC", label or "").replace("–", "-").replace("—", "-").lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


# ── customer ─────────────────────────────────────────────────────────────────

_CORPORATE = re.compile(r"^Corporate\s*\(UBO:\s*(?P<ubo>.+?)\)\s*$")


def customer_type(text: str) -> dict:
    text = (text or "").strip()
    m = _CORPORATE.match(text)
    if m:
        return {"type": "Corporate", "corporate": True, "nri": False, "ubo": m.group("ubo").strip()}
    return {"type": text or "Individual", "corporate": False, "nri": "NRI" in text, "ubo": ""}


def customer_dob(text: str) -> str:
    """'UBO DOB: 1965-07-20' → '1965-07-20'."""
    return re.sub(r"^\s*UBO\s+DOB:\s*", "", text or "").strip()


COUNTRY_CODES = {"India": "IN", "Vietnam": "VN", "United States": "US"}
COUNTRY_NAMES = {v: k for k, v in COUNTRY_CODES.items()}


def country_code(name: str) -> str:
    return COUNTRY_CODES.get((name or "").strip(), (name or "").strip()[:2].upper())


# ── watchlist record ─────────────────────────────────────────────────────────

_PARENS = re.compile(r"\(([^()]*)\)")


def wc_name(text: str) -> dict:
    """'QURESHI, Farheen (AKA: FARHEEN KURESHI)' → surname, given, aka list.
    Handles 'SANGEETHA (single name)', 'SANGEETHA (single name) (AKA: …)'
    and a native-script alias in parentheses ('NGUYEN, Thanh Ha (Nguyễn Thanh Hạ)')."""
    text = (text or "").strip()
    parens = _PARENS.findall(text)
    head = _PARENS.sub("", text).strip().rstrip(",").strip()
    mononym = any(p.strip().lower() == "single name" for p in parens)
    aka, native = [], ""
    for p in parens:
        p = p.strip()
        if p.lower() == "single name":
            continue
        if p.upper().startswith("AKA:"):
            aka.extend(a.strip() for a in re.split(r"[;,]", p[4:]) if a.strip())
        else:
            native = p
            aka.append(p)
    if "," in head:
        surname, given = (s.strip() for s in head.split(",", 1))
    else:
        surname, given = head, ""
    return {"surname": surname, "given": given, "aka": aka, "native": native, "mononym": mononym or not given}


_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR = re.compile(r"^\d{4}$")
_RANGE = re.compile(r"^(\d{4})\s*[–—-]\s*(\d{4})$")


def wc_dob(text: str) -> dict:
    """The seven shapes of the listed date of birth, read from the date
    itself. The parenthetical is kept as `annotation` for measurement only."""
    text = (text or "").strip()
    annotation = ""
    m = _PARENS.search(text)
    if m:
        annotation = m.group(1).strip()
    head = _PARENS.sub("", text).strip()
    out = {"kind": "none", "date": "", "year": None, "year_to": None, "annotation": annotation, "raw": text}
    if _FULL.match(head):
        out.update(kind="full", date=head, year=int(head[:4]))
    elif _YEAR.match(head):
        out.update(kind="year", year=int(head))
    elif _RANGE.match(head):
        a, b = _RANGE.match(head).groups()
        out.update(kind="range", year=int(a), year_to=int(b))
    return out


def percent(text) -> int:
    m = re.search(r"-?\d+", str(text or ""))
    return int(m.group()) if m else 0


WC_GROUPS = ("PEP", "Adverse Media", "Law Enforcement", "Regulatory Enforcement", "Sanctions")


def wc_group(category: str) -> str:
    c = (category or "").strip()
    for g in WC_GROUPS:
        if c.startswith(g):
            return g
    return c.split(" - ")[0].split(" (")[0] or "Other"


_WC_COUNTRIES = {"USA": "United States", "U.S.A.": "United States", "US": "United States", "UAE": "United Arab Emirates",
                 "Vietnam": "Vietnam", "Viet Nam": "Vietnam", "India": "India", "Hong Kong SAR": "Hong Kong",
                 "Singapore": "Singapore", "UK": "United Kingdom"}


def wc_location(text: str) -> dict:
    """'Hai Phong (formerly Hai Duong), Vietnam' / 'Seattle, WA, USA; Bellevue, WA, USA'
    / 'Can Tho, Vietnam (district not listed)' / 'Dubai, UAE' → the first place,
    with every place the record lists under `places` (a profile names each
    place linked to the person: home, office, where they went)."""
    text = (text or "").strip()
    places = [_wc_place(p.strip()) for p in text.split(";") if p.strip()] or [_wc_place("")]
    return dict(places[0], places=places, all=[p.strip() for p in text.split(";") if p.strip()], raw=text)


def _wc_place(first: str) -> dict:
    """One place of a record's listed places."""
    notes = [n.strip() for n in _PARENS.findall(first)]
    plain = _PARENS.sub("", first)
    parts = [p.strip() for p in plain.split(",") if p.strip()]
    former = ""
    for n in notes:
        if n.lower().startswith("formerly "):
            former = n[9:].strip()
    city, region, country = "", "", ""
    if len(parts) >= 3:
        city, region, country = parts[0], parts[1], parts[-1]
    elif len(parts) == 2:
        city, country = parts
    elif parts:
        country = parts[0]
        if country not in _WC_COUNTRIES and country not in _WC_COUNTRIES.values():
            city = country
    country = _WC_COUNTRIES.get(country, country)
    return {"city": city, "region": region, "country": country, "former": former, "notes": notes, "raw": first}


# ── passport ─────────────────────────────────────────────────────────────────

_VIZ = re.compile(r"^Surname:\s*(?P<s>.*?)\s*\|\s*Given\s+Names?(?:\(s\))?\s*:\s*(?P<g>.*?)\s*$", re.I)


def viz(text: str) -> dict:
    """'Surname: VŨ | Given Names: CÔNG KHÁNH' and the Indian
    'Surname: UPADHYAY | Given Name(s): ASHISH'; '(blank)' is an empty surname."""
    m = _VIZ.match((text or "").strip())
    if not m:
        return {"surname": "", "given": (text or "").strip(), "blank_surname": True, "raw": text or ""}
    s, g = m.group("s").strip(), m.group("g").strip()
    blank = s.lower() in ("(blank)", "blank", "-", "")
    return {"surname": "" if blank else s, "given": g, "blank_surname": blank, "raw": text}


def mrz1(text: str) -> dict:
    """Line 1 of a TD3 MRZ: 'P<INDUPADHYAY<<ASHISH<<<…'. The primary
    identifier (surname) sits before '<<', the secondary (given names) after;
    single '<' separates tokens. A 44-character line that does not end in a
    filler has been truncated."""
    line = (text or "").strip()
    out = {"line1": line, "doc_type": "", "issuer": "", "primary": "", "primary_tokens": [], "secondary": [],
           "truncated": False, "mononym": False}
    m = re.match(r"^([A-Z<]{1,2})<([A-Z]{3})(.*)$", line)
    if not m:
        return out
    out["doc_type"], out["issuer"], rest = m.group(1).replace("<", ""), m.group(2), m.group(3)
    primary, _, secondary = rest.partition("<<")
    out["primary"] = primary.replace("<", " ").strip()
    out["primary_tokens"] = [t for t in primary.split("<") if t]
    out["secondary"] = [t for t in secondary.split("<") if t]
    out["mononym"] = primary == ""
    out["truncated"] = len(line) >= 44 and not line.endswith("<")
    return out


# ── addresses on documents ───────────────────────────────────────────────────

def doc_address(text: str) -> dict:
    """An address field that may instead say what the document does not
    print: 'Not printed on US passport (Place of birth: ILLINOIS, U.S.A.)'."""
    text = (text or "").strip()
    if text.lower().startswith("not printed"):
        pob = ""
        m = re.search(r"Place of birth:\s*([^)]*)\)", text)
        if m:
            pob = m.group(1).strip()
        return {"printed": False, "text": "", "pob": pob, "note": text}
    return {"printed": True, "text": text, "pob": "", "note": ""}


def secondary_type(text: str) -> dict:
    t = (text or "").strip()
    m = re.match(r"^Driver.?s License \((\w\w)\)$", t)
    if m:
        return {"type": t, "kind": "driving_licence", "us_state": m.group(1)}
    if t.lower().startswith("driving licence"):
        return {"type": t, "kind": "driving_licence", "us_state": ""}
    if "naturalization" in t.lower():
        return {"type": t, "kind": "certificate", "us_state": ""}
    return {"type": t, "kind": "national_id", "us_state": ""}


def poa_type(text: str) -> dict:
    t = (text or "").strip()
    low = t.lower()
    if "bank statement" in low:
        kind = "bank_statement"
    elif any(w in low for w in ("lease", "rent", "tenancy")):
        kind = "lease"
    elif "ct07" in low or "residence confirmation" in low:
        kind = "residence_confirmation"
    elif "aadhaar" in low:
        kind = "aadhaar"
    elif "military" in low:
        kind = "military_orders"
    else:
        kind = "utility_bill"
    return {"type": t, "kind": kind}


# ── history (the analyst's outcome; measurement only) ────────────────────────

_OUTCOME = {"False Positive": "FP", "True Positive": "TP", "Pending": "Pending", "Escalated": "Escalated"}


def disposition(text: str) -> dict:
    t = (text or "").strip()
    family, _, action = t.partition(" - ")
    return {"disposition": t, "outcome": _OUTCOME.get(family.strip(), family.strip() or "Unknown"),
            "action": action.strip(), "corrected": "KYC Record Corrected" in t}


def minutes(text) -> int:
    return percent(text)
