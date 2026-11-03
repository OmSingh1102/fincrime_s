"""Synthetic uploads for the file book's cases.

The reconciliation log carries no document images, so the demo plants a
deterministic defect plan on each case (a hash of the case id decides) and
draws the uploads as SPECIMEN SVGs the way the console's svg_cards does.
The console's AI Agent reads the passport and the proof of address from
these drawings, so the defect is synthetic but the detection is not.
"""
import hashlib
import math
import re
from datetime import timedelta

from fincrime.documents import svg_cards
from fincrime.documents.svg_cards import (FONT, ISO3, MONO, _check_digit, _defs, _field, _footer, _label, _mrz_lines, _pad,
                                          _silhouette, _stamp, _value, _watermark)
from fincrime.filebook import names
from fincrime.utils import dates
from fincrime.utils.text import esc, mrz_field

CAPTURE_PER_MILLE = 80             # cases with a capture defect on the passport photo
TAMPER_PER_MILLE = 20              # cases whose passport fails its own check digits
POA_OLD_PER_MILLE = 30             # cases whose proof of address is 7-11 months old
# Index 1 stays the flat scan the console's showcase case (KYC-2026-3118) carries.
_CAPTURE_BUCKETS = ("damaged", "flat_scan", "obstructed", "partial", "partial", "obstructed", "glare", "angled",
                    "blur", "angled", "expired", "expired")
# Drawn as a phone photo of the document lying on a cloth, the way real
# customers' failed uploads look (an analyst's reference frames): each one
# hides at least a date or the number, so the AI Agent's fixed checks fail
# even when the model does not name the problem.
PHOTO_DEFECTS = ("partial", "blur", "obstructed", "angled", "damaged")
# When those were last redrawn. The console stamps it on the case, and a
# stored case read from an older drawing starts again (app._load_seed).
REDRAWN = "2026-10-14"
# When a long proof-of-address address started to wrap: it used to run over
# a statement's date or off the edge of an Aadhaar card.
POA_REDRAWN = "2026-10-15"
# When an Aadhaar proof of address started to print its holder's name: before,
# it showed only the address, whose S/O or C/O line names a relative.
AADHAAR_NAMED = "2026-10-16"
# The cloth under the photo: base, dark thread, light thread.
SURFACES = (("#cbbfa8", "#8f8068", "#ebe2d0"), ("#3f5b80", "#283e5c", "#7290b5"),
            ("#5b4737", "#3a2c21", "#806753"), ("#7c7e79", "#575954", "#a7a9a3"))
COUNTRY_HEADER = {"IN": "REPUBLIC OF INDIA", "VN": "SOCIALIST REPUBLIC OF VIET NAM", "US": "UNITED STATES OF AMERICA"}
SECONDARY_TITLE = {"PAN Card": "PERMANENT ACCOUNT NUMBER CARD", "Aadhaar": "AADHAAR", "Voter ID (EPIC)": "ELECTOR PHOTO IDENTITY CARD",
                   "Driving Licence": "DRIVING LICENCE", "CCCD (chip Citizen ID)": "CĂN CƯỚC CÔNG DÂN",
                   "Căn cước (2024 ID card)": "CĂN CƯỚC", "Certificate of Naturalization": "CERTIFICATE OF NATURALIZATION"}


def _h(seed, case_id):
    return int(hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest(), 16)


def _iso(dt):
    return dt.strftime("%Y-%m-%d")


def synthesise(case):
    """Decide the uploads and their defects for one case. Deterministic:
    the same case always gets the same plan."""
    cid = case["id"]
    # the seeds keep the names they were born with: renaming one redraws every case and changes its dates
    h = _h("hub-doc", cid)
    tampered = _h("hub-tamper", cid) % 1000 < TAMPER_PER_MILLE
    capture = "none"
    if not tampered and h % 1000 < CAPTURE_PER_MILLE:
        capture = _CAPTURE_BUCKETS[(h >> 8) % len(_CAPTURE_BUCKETS)]
    poa_old = _h("hub-poa", cid) % 1000 < POA_OLD_PER_MILLE
    received = dates.BOOT - timedelta(hours=h % 40, minutes=(h >> 16) % 60)     # within the last 40 h: a few P1s past their 24 h
    poa_age = 210 + (h >> 24) % 120 if poa_old else 7 + (h >> 24) % 140
    issued = received - timedelta(days=poa_age)
    years_held = 1 + (h >> 32) % 9
    pp_issue = received - timedelta(days=365 * years_held + (h >> 40) % 300)
    if capture == "expired":
        pp_expiry = received - timedelta(days=30 + (h >> 48) % 370)
    else:
        pp_expiry = pp_issue + timedelta(days=3652)
    sex = holder_sex(case) or ("F" if (h >> 56) % 2 else "M")       # a coin toss only when nothing says
    docs = case["documents"]
    defects = []
    if capture != "none":
        defects.append(capture)
    if tampered:
        defects.append("tampered")
    files = [
        {"id": "passport", "kind": "passport", "side": "front", "label": "Passport data page", "defects": defects,
         "capture": "flat_scan" if capture == "flat_scan" else "photo", "surface": (h >> 64) % len(SURFACES)},
        {"id": "passport-back", "kind": "passport", "side": "back", "label": "Passport back page", "defects": [], "capture": "photo"},
        {"id": "secondary", "kind": docs["secondary"]["kind"], "side": "front", "label": docs["secondary"]["type"],
         "defects": [], "capture": "photo"},
        {"id": "poa", "kind": docs["poa"]["kind"], "side": "page 1", "label": docs["poa"]["type"], "defects": [],
         "capture": "flat_scan", "issued_on": _iso(issued)},
    ]
    case["synth"] = {"capture": capture, "tampered": tampered, "poa_age_days": poa_age, "poa_issued_on": _iso(issued),
                     "received_at": dates.fmt(received), "passport_issued": _iso(pp_issue), "passport_expiry": _iso(pp_expiry),
                     "sex": sex, "id_number": f"{_h('hub-id', cid) % 10 ** 10:010d}",
                     "drawn": max(d for d, redrawn in (("", True), (REDRAWN, capture in PHOTO_DEFECTS),
                                                      (POA_REDRAWN, poa_wraps(case)),
                                                      (AADHAAR_NAMED, docs["poa"]["kind"] == "aadhaar")) if redrawn)}
    case["files"] = files
    return case


# What the file says about the holder's sex, strongest first: a relationship
# on an Indian address (son / daughter / wife of), an honorific on the name,
# a married-name change the analysts recorded, then the given names.
RELATION_SEX = re.compile(r"\b([SDW])\s*/\s*O\b", re.I)
HONORIFIC_SEX = {"SMT": "F", "MRS": "F", "MS": "F", "MISS": "F", "KUMARI": "F", "SHRI": "M", "MR": "M"}
MARRIED_NAME = ("married vs maiden surname", "western married surname on form")


def holder_sex(case):
    """'F', 'M' or '' when nothing in the file says."""
    docs, raw = case["documents"], case.get("raw") or {}
    for text in (docs["poa"].get("address", ""), docs["secondary"]["address"].get("text", "")):
        m = RELATION_SEX.match((text or "").strip())
        if m:
            return "M" if m.group(1).upper() == "S" else "F"
    for token in re.findall(r"[A-Za-z]+", case["customer"]["name"])[:1]:
        if token.upper() in HONORIFIC_SEX:
            return HONORIFIC_SEX[token.upper()]
    if (raw.get("name_variance_type") or "").strip().lower() in MARRIED_NAME:
        return "F"
    return names.sex_from_given(docs["passport"]["viz"].get("given", ""), case["country"])


def poa_wraps(case):
    """Whether the proof of address prints its address on more than one line
    (so it is drawn differently since POA_REDRAWN)."""
    poa = case["documents"]["poa"]
    if poa["kind"] == "aadhaar":
        return svg_cards.wraps(poa["address"] or "Address on file", 12, svg_cards.CARD_ADDRESS_WIDTH)
    return svg_cards.wraps(svg_cards.service_address(poa["address"], case["country"]), 12, svg_cards.BILL_ADDRESS_WIDTH)


# ── MRZ line 2 ───────────────────────────────────────────────────────────────

def _yymmdd(iso):
    d = dates.parse(iso)
    return d.strftime("%y%m%d") if d else "000000"


def mrz_line2(case, tampered=None):
    """Line 2 of the passport MRZ with its 7-3-1 check digits, from the
    passport number, the date of birth, the holder's sex (as the data page
    prints it; no check digit covers it) and the expiry. A tampered page
    carries a wrong date-of-birth check digit."""
    s = case["synth"]
    issuer = case["documents"]["passport"]["mrz"].get("issuer") or ISO3.get(case["country"], "XXX")
    num = _pad(mrz_field(case["documents"]["passport"]["number"]), 9)
    dob = _yymmdd(case["customer"]["dob"])
    exp = _yymmdd(s["passport_expiry"])
    dob_cd = _check_digit(dob)
    if tampered if tampered is not None else s.get("tampered"):
        dob_cd = str((int(dob_cd) + 1) % 10)
    body = num + _check_digit(num) + issuer + dob + dob_cd + (s.get("sex") or "<") + exp + _check_digit(exp) + "<" * 14
    composite = _check_digit(num + _check_digit(num) + dob + dob_cd + exp + _check_digit(exp) + "<" * 14)
    return _pad(body + "<" + composite, 44)


def _long_date(iso):
    d = dates.parse(iso)
    return d.strftime("%d %b %Y").upper() if d else ""


def _surface(file):
    """The cloth the document lies on, and a vignette for the photo's frame."""
    base, dark, light = SURFACES[file.get("surface", 0) % len(SURFACES)]
    return (f'<defs><pattern id="cloth" width="10" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(32)">'
            f'<rect width="10" height="10" fill="{base}"/><path d="M0 2.5H10M0 7.5H10" stroke="{dark}" stroke-width="2.2" opacity="0.45"/>'
            f'<path d="M2.5 0V10M7.5 0V10" stroke="{light}" stroke-width="1.2" opacity="0.35"/></pattern>'
            f'<radialGradient id="vignette" cx="0.5" cy="0.5" r="0.75"><stop offset="0.5" stop-color="#000" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="#000" stop-opacity="0.55"/></radialGradient>'
            f'<filter id="soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="9"/></filter></defs>'
            f'<rect x="-3000" y="-3000" width="7000" height="7000" fill="url(#cloth)"/>')


def _crack(points, width=1.6):
    """A crack in the laminate: a dark edge under a light split."""
    d = "M" + " L".join(f"{x} {y}" for x, y in points)
    return (f'<path d="{d}" fill="none" stroke="#4a4234" stroke-width="{width + 1.8}" stroke-linejoin="round" opacity="0.75"/>'
            f'<path d="{d}" fill="none" stroke="#fbf8f0" stroke-width="{width}" stroke-linejoin="round" transform="translate(-0.8 -0.6)"/>')


def _damage(W, H):
    """A cracked, worn card: two cracks across the page, a fold, chipped
    corners, and worn patches where the date of birth, the expiry and the
    passport number were printed and across the MRZ's dates."""
    out = _crack([(296, 0), (316, 38), (350, 60), (372, 96), (420, 106), (470, 90), (522, 128), (566, 150), (W, 172)])
    out += _crack([(0, 302), (58, 288), (122, 302), (180, 270), (262, 262), (332, 238), (408, 244), (468, 206), (540, 216),
                   (600, 196), (W, 204)], 1.4)
    out += _crack([(180, 270), (196, 330), (176, 372), (198, H)], 1.1)
    out += f'<path d="M150 0 L84 {H}" stroke="#fff" stroke-width="3" opacity="0.45"/><path d="M152 0 L86 {H}" stroke="#6b604c" stroke-width="1" opacity="0.35"/>'
    # rubbed-off print: jagged-edged, grainy scuffs with abrasion streaks,
    # so they read as wear on the card rather than something lying on it
    def scuff(x0, y0, x1, y1, seed):
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
        pts = []
        for i in range(36):
            a = 2 * math.pi * i / 36
            j = 0.82 + 0.3 * ((seed * 7 + i * 13) % 11) / 10
            pts.append(f"{cx + rx * j * math.cos(a):.1f} {cy + ry * j * math.sin(a):.1f}")
        return '<path d="M' + " L".join(pts) + ' Z" fill="#e6dfd0"/>'
    patches = (scuff(428, 192, 572, 226, 1), scuff(368, 82, 494, 110, 2), scuff(436, 266, 566, 294, 3),
               scuff(180, 397, 398, 429, 4))
    streaks = "".join(f'<path d="M{x} {y} l{n} {d}" stroke="#fffdf6" stroke-width="1.3" opacity="0.8"/>'
                      for x, y, n, d in ((436, 204, 128, -3), (444, 214, 116, 2), (376, 92, 110, -2), (382, 101, 100, 3),
                                         (444, 278, 114, -2), (452, 286, 104, 1), (190, 408, 200, -3), (198, 418, 186, 2)))
    out += ('<defs><filter id="grit" x="0" y="0" width="100%" height="100%">'
            '<feTurbulence type="fractalNoise" baseFrequency="1.1" numOctaves="2" seed="11" result="g"/>'
            '<feColorMatrix in="g" type="matrix" values="0 0 0 0 0.5  0 0 0 0 0.45  0 0 0 0 0.38  0 0 0 1.1 -0.32" result="speck"/>'
            '<feComposite in="speck" in2="SourceGraphic" operator="in" result="specks"/>'
            '<feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="specks"/></feMerge></filter></defs>'
            '<g filter="url(#grit)">' + "".join(patches) + '</g>' + streaks)
    out += (f'<ellipse cx="96" cy="350" rx="70" ry="34" fill="#8a6a3a" opacity="0.16"/>'
            f'<path d="M{W - 34} 0 L{W} 0 L{W} 30 L{W - 12} 22 Z" fill="url(#cloth)"/><path d="M0 {H - 26} L22 {H - 14} L30 {H} L0 {H} Z" fill="url(#cloth)"/>')
    return out


def _phone(cx, cy, angle):
    """A phone lying on the page, with its shadow."""
    body = (f'<rect x="-88" y="-185" width="176" height="370" rx="26"/>')
    return (f'<g transform="translate({cx + 10} {cy + 14}) rotate({angle})" fill="#000" opacity="0.5" filter="url(#soft)">{body}</g>'
            f'<g transform="translate({cx} {cy}) rotate({angle})">'
            f'<rect x="-88" y="-185" width="176" height="370" rx="26" fill="#141414" stroke="#3a3a3a" stroke-width="3"/>'
            f'<rect x="-80" y="-177" width="160" height="354" rx="20" fill="none" stroke="#262626" stroke-width="1.5"/>'
            f'<rect x="-74" y="-170" width="74" height="74" rx="18" fill="#1d1d1d" stroke="#2e2e2e" stroke-width="2"/>'
            f'<circle cx="-52" cy="-148" r="13" fill="#060606" stroke="#444" stroke-width="2"/>'
            f'<circle cx="-52" cy="-118" r="13" fill="#060606" stroke="#444" stroke-width="2"/>'
            f'<circle cx="-22" cy="-133" r="11" fill="#060606" stroke="#444" stroke-width="2"/>'
            f'<circle cx="-16" cy="-160" r="4" fill="#d8d2b8" opacity="0.8"/>'
            f'<path d="M60 -170 L60 170" stroke="#2a2a2a" stroke-width="6" opacity="0.6"/></g>')


def _photo(W, H, body, file, defect):
    """A phone photo of the page on a cloth, with the planted defect:
    obstructed  a phone lies over the dates, on the page and in the MRZ;
    partial     shot too close: the number, the dates and the MRZ fall outside;
    angled      tilted and skewed in a dim frame, a phone over its lower corner;
    blur        camera shake in low light;
    damaged     cracks, a fold and worn patches over the dates and the number."""
    cx, cy = W / 2, H / 2
    card = (f'<g transform="rotate(4 {cx} {cy}) translate(6 10)"><rect width="{W}" height="{H}" rx="14" fill="#000" opacity="0.4" filter="url(#soft)"/></g>')
    over, shade = "", 0.0
    if defect == "obstructed":
        view, turn = (-60, -50, W + 120, H + 100), -3
        over = _phone(380, 300, 52)
    elif defect == "partial":
        view, turn = (-40, -36, 412, 388), 2.5
        shade = 0.12
    elif defect == "angled":
        view, turn = (-200, -150, W + 400, H + 300), 0
        shade = 0.34
        over = _phone(510, 385, 48)
    elif defect == "blur":
        view, turn = (-140, -110, W + 280, H + 220), -5
        shade = 0.36
    else:                                     # damaged
        view, turn = (-50, -44, W + 100, H + 88), 1.5
        body += _damage(W, H)
    if defect == "angled":
        place = f"translate(420 300) rotate(-14) skewX(-20) scale(0.9 0.64) translate({-cx} {-cy})"
    else:
        place = f"rotate({turn} {cx} {cy})"
    scene = (_surface(file) + f'<g transform="{place}">{card}{body}</g>' + over)
    vx, vy, vw, vh = view
    scene += f'<rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="url(#vignette)"/>'
    if shade:
        scene += f'<rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="#120c04" opacity="{shade}"/>'
    if defect == "blur":
        scene = (f'<defs><filter id="shake" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="6 2.2"/></filter></defs>'
                 f'<g filter="url(#shake)">{scene}</g>')
    width = 760
    height = round(width * vh / vw)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx} {vy} {vw} {vh}" width="{width}" height="{height}" font-family="{FONT}" '
            f'style="background:#2b2b2b" data-capture="photo" data-defect="{defect}">{scene}</svg>')


def _wrap_page(W, H, body, file):
    """Close the SVG with the capture look and the planted defect."""
    defects = set(file.get("defects") or [])
    for defect in PHOTO_DEFECTS:
        if defect in defects:
            return _photo(W, H, body, file, defect)
    view = f"-28 -28 {W + 56} {H + 56}"            # the whole card in shot, all four corners on the table
    inner = body
    if "glare" in defects:
        inner += (f'<defs><radialGradient id="glare" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#fff" stop-opacity="0.98"/>'
                  f'<stop offset="0.55" stop-color="#fff" stop-opacity="0.85"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient></defs>'
                  f'<ellipse cx="{W * 0.62}" cy="{H * 0.74}" rx="{W * 0.34}" ry="{H * 0.3}" fill="url(#glare)"/>')
    if file.get("capture") == "flat_scan" or "flat_scan" in defects:
        inner = (f'<defs><filter id="scan"><feColorMatrix type="saturate" values="0.35"/></filter></defs>'
                 f'<g filter="url(#scan)">{inner}</g>'
                 f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" fill="none" stroke="#b7b7b2" stroke-width="1"/>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" '
                f'style="background:#e9e9e6" data-capture="flat_scan"><rect x="-2000" y="-2000" width="5000" height="5000" fill="#e9e9e6"/>{inner}</svg>')
    # a camera photo: the card lies on a dark table, slightly turned, with a shadow
    inner = (f'<rect x="-2000" y="-2000" width="5000" height="5000" fill="#2b2b2b"/>'
             f'<g transform="rotate(-1.6 {W / 2} {H / 2}) translate(4 6)"><rect x="0" y="0" width="{W}" height="{H}" rx="14" fill="#000" opacity="0.45"/></g>'
             f'<g transform="rotate(-1.6 {W / 2} {H / 2})">{inner}</g>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" width="{W + 56}" height="{H + 56}" font-family="{FONT}" '
            f'style="background:#2b2b2b" data-capture="photo">{inner}</svg>')


def passport_page(case, file):
    s, docs = case["synth"], case["documents"]
    pp = docs["passport"]
    W, H = 640, 440
    tampered = "tampered" in (file.get("defects") or [])
    country = COUNTRY_HEADER.get(case["country"], case["country_name"].upper())
    body = _defs()
    body += f'<rect width="{W}" height="{H}" rx="14" fill="#f4efe4"/><rect width="{W}" height="{H}" rx="14" fill="url(#guilloche)"/>'
    body += f'<rect x="0" y="0" width="{W}" height="54" rx="14" fill="#5b2a86"/>'
    body += _value(24, 34, country, 15, "700", "#fff")
    body += f'<text x="{W - 24}" y="34" text-anchor="end" font-family="{FONT}" font-size="13" font-weight="700" fill="#e4d4f5">PASSPORT / PASSEPORT</text>'
    body += _silhouette(28, 78, 150, 190)
    x = 210
    body += _label(x, 86, "Type / Type") + _value(x, 102, "P", 13)
    body += _label(x + 70, 86, "Code / Code") + _value(x + 70, 102, pp["mrz"].get("issuer") or ISO3.get(case["country"], ""), 13)
    body += _label(x + 170, 86, "Passport No. / No du passeport") + _value(x + 170, 102, pp["number"], 14, family=MONO)
    body += _field(x, 130, "Surname / Nom", pp["viz"]["surname"] or "", 15)
    body += _field(x, 166, "Given names / Prenoms", pp["viz"]["given"], 14)
    body += _field(x, 202, "Nationality / Nationalite", case["country_name"], 12)
    dob_value = _long_date(case["customer"]["dob"])
    if tampered:
        body += _label(x + 230, 202, "Date of birth / Date de naissance")
        body += _value(x + 230, 216, dob_value, 12, "700", "#111", family=MONO)
    else:
        body += _field(x + 230, 202, "Date of birth / Date de naissance", dob_value, 12)
    body += _field(x, 238, "Place of birth / Lieu de naissance", pp["address"]["pob"] or case["wc"]["location"].get("city", "") or case["country_name"], 11)
    body += _field(x + 230, 238, "Sex / Sexe", s["sex"], 12)
    body += _field(x, 274, "Date of issue / Date de delivrance", _long_date(s["passport_issued"]), 12)
    body += _field(x + 230, 274, "Date of expiry / Date d'expiration", _long_date(s["passport_expiry"]), 12)
    if pp["address"]["printed"]:
        body += _label(x, 312, "Address / Adresse")
        text = pp["address"]["text"]
        body += _value(x, 326, text[:62], 9, "600")
        if len(text) > 62:
            body += _value(x, 338, text[62:124], 9, "600")
    body += _mrz_lines(28, 396, [pp["mrz"]["line1"], mrz_line2(case, tampered)], W - 46)
    if "expired" in (file.get("defects") or []):
        body += _stamp(W - 130, 300, "EXPIRED")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap_page(W, H, body, file)


def passport_back(case, file):
    W, H = 640, 440
    country = COUNTRY_HEADER.get(case["country"], case["country_name"].upper())
    body = _defs()
    body += f'<rect width="{W}" height="{H}" rx="14" fill="#f4efe4"/><rect width="{W}" height="{H}" rx="14" fill="url(#guilloche)"/>'
    body += f'<rect x="0" y="0" width="{W}" height="54" rx="14" fill="#5b2a86"/>'
    body += _value(24, 34, country, 15, "700", "#fff")
    body += _label(24, 90, "ENDORSEMENTS / OBSERVATIONS") + _value(24, 112, "NONE", 12, "600")
    body += _label(24, 150, "EMERGENCY CONTACT") + _value(24, 170, "Not recorded", 12, "500", "#444")
    body += _label(24, 210, "ISSUING AUTHORITY") + _value(24, 230, f"PASSPORT OFFICE - {case['documents']['passport']['mrz'].get('issuer') or ''}", 12, "600")
    body += f'<rect x="{W - 200}" y="80" width="160" height="60" fill="url(#bars)"/>' + _label(W - 200, 158, "SPECIMEN BARCODE")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap_page(W, H, body, file)


def _adapter(case, name, address, native=""):
    s = case["synth"]
    return {"client": {"legal_name": name, "native_name": native, "dob": case["customer"]["dob"], "nationality": case["country"],
                       "gender": s["sex"], "id_number": s["id_number"], "address": address, "country": case["country_name"]},
            "created_at": s["received_at"], "documents": [], "agency": None}


def render(case, file_id):
    """The SVG for one upload, or None when the id is unknown."""
    file = next((f for f in case.get("files", []) if f["id"] == file_id), None)
    if file is None:
        return None
    docs = case["documents"]
    if file["id"] == "passport":
        return passport_page(case, file)
    if file["id"] == "passport-back":
        return passport_back(case, file)
    sec, poa = docs["secondary"], docs["poa"]
    if file["id"] == "secondary":
        name = sec["name"] or case["identity"].get("name", {}).get("canonical", case["customer"]["name"])
        native = name if any(ord(ch) > 127 for ch in name) else ""
        address = sec["address"]["text"] or case["customer"]["address"]
        adapter = _adapter(case, name if not native else svg_cards.translit(name), address, native)
        doc = {"id": "secondary", "kind": sec["kind"], "side": "front", "quality": "ok", "country": case["country"]}
        if sec["kind"] == "driving_licence" and case["country"] == "US":
            adapter["client"]["address"] = address
            return svg_cards.driving_licence_svg(adapter, doc)
        title = SECONDARY_TITLE.get(sec["type"], sec["type"].upper())
        # the card's name on one line as the file gives it (a Vietnamese card's "Full name", family name first);
        # the address only where the file says the card prints one
        return svg_cards.national_id_svg(adapter, doc, title=title, code="ID",
                                         name_label="Full name" if case["country"] == "VN" else "Name",
                                         address=sec["address"]["text"] if sec["address"]["printed"] else "")
    if file["id"] == "poa":
        name = case["identity"].get("name", {}).get("canonical", case["customer"]["name"])
        adapter = _adapter(case, name, poa["address"])
        doc = {"id": "poa", "kind": "utility_bill", "side": "page 1", "quality": "ok", "country": case["country"],
               "issued_on": file.get("issued_on", "")}
        kind, ptype = poa["kind"], poa["type"]
        if kind == "bank_statement":
            return svg_cards.bank_statement_svg(adapter, doc)
        if kind == "aadhaar":
            doc2 = dict(doc, side="back")
            return svg_cards.national_id_svg(adapter, doc2, title="AADHAAR", code="ID", holder=True)
        if kind == "lease":
            return svg_cards.utility_bill_svg(adapter, doc, provider="Residential lease", kind=ptype.upper())
        if kind == "residence_confirmation":
            return svg_cards.utility_bill_svg(adapter, doc, provider="Công an phường", kind="XÁC NHẬN THÔNG TIN CƯ TRÚ (CT07)")
        if kind == "military_orders":
            return svg_cards.utility_bill_svg(adapter, doc, provider="Department of Defense", kind="LEAVE AND EARNINGS STATEMENT")
        provider = "EVN - Điện lực" if "EVN" in ptype else "Northgate Utilities"
        return svg_cards.utility_bill_svg(adapter, doc, provider=provider, kind=ptype.upper())
    return None
