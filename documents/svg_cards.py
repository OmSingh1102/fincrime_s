"""Synthetic identity and address documents, drawn as SVG from the case data.

The Data Confirm modal needs something to look at, and the only honest thing
to show in a demo is a document that is obviously not real: every card
carries a diagonal SPECIMEN watermark, a grey silhouette instead of a
photograph, and numbers that are hashes of the case key. The layouts follow
the real document families closely enough that an analyst's checks make
sense — a machine-readable zone with correct check digits, an expiry date,
front and back — and the `quality` flag on a document (blurred, cropped,
expired) produces exactly the defect the resubmit reasons name.

Text inside an SVG that is loaded through <img> cannot use the page's web
font, so the cards name Arial, which is on every Windows and macOS box and
renders Cyrillic. Everything interpolated is XML-escaped.
"""
import re
import unicodedata

from fincrime.data import vocab
from fincrime.utils import dates
from fincrime.utils.text import esc, mrz_field, mrz_name, split_name, translit

COUNTRY_NAMES = {
    "US": "UNITED STATES OF AMERICA", "GB": "UNITED KINGDOM", "DE": "BUNDESREPUBLIK DEUTSCHLAND", "FR": "REPUBLIQUE FRANCAISE",
    "SG": "REPUBLIC OF SINGAPORE", "MX": "ESTADOS UNIDOS MEXICANOS", "VN": "SOCIALIST REPUBLIC OF VIET NAM",
    "RU": "RUSSIAN FEDERATION", "PK": "ISLAMIC REPUBLIC OF PAKISTAN", "BR": "REPUBLICA FEDERATIVA DO BRASIL",
    "AE": "UNITED ARAB EMIRATES", "JO": "HASHEMITE KINGDOM OF JORDAN", "CH": "SWISS CONFEDERATION", "ES": "REINO DE ESPANA",
    "IT": "REPUBBLICA ITALIANA", "JP": "JAPAN", "IN": "REPUBLIC OF INDIA", "CO": "REPUBLICA DE COLOMBIA", "NG": "FEDERAL REPUBLIC OF NIGERIA",
    "IE": "IRELAND", "LB": "REPUBLIC OF LEBANON", "AU": "AUSTRALIA", "KR": "REPUBLIC OF KOREA", "CZ": "CZECH REPUBLIC",
    "EG": "ARAB REPUBLIC OF EGYPT", "DK": "KINGDOM OF DENMARK", "PT": "REPUBLICA PORTUGUESA", "KZ": "REPUBLIC OF KAZAKHSTAN",
    "MA": "KINGDOM OF MOROCCO", "SE": "KINGDOM OF SWEDEN", "MY": "MALAYSIA", "CA": "CANADA",
}
ISO3 = {"US": "USA", "GB": "GBR", "DE": "DEU", "FR": "FRA", "SG": "SGP", "MX": "MEX", "VN": "VNM", "RU": "RUS", "PK": "PAK",
        "BR": "BRA", "AE": "ARE", "JO": "JOR", "CH": "CHE", "ES": "ESP", "IT": "ITA", "JP": "JPN", "IN": "IND", "CO": "COL",
        "NG": "NGA", "IE": "IRL", "LB": "LBN", "AU": "AUS", "KR": "KOR", "CZ": "CZE", "EG": "EGY", "DK": "DNK", "PT": "PRT",
        "KZ": "KAZ", "MA": "MAR", "SE": "SWE", "MY": "MYS", "CA": "CAN"}
# The states a US licence can be issued by, for its header (us_state).
US_STATES = {"AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS", "CA": "CALIFORNIA", "CO": "COLORADO",
             "CT": "CONNECTICUT", "DE": "DELAWARE", "DC": "DISTRICT OF COLUMBIA", "FL": "FLORIDA", "GA": "GEORGIA",
             "HI": "HAWAII", "ID": "IDAHO", "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS",
             "KY": "KENTUCKY", "LA": "LOUISIANA", "ME": "MAINE", "MD": "MARYLAND", "MA": "MASSACHUSETTS",
             "MI": "MICHIGAN", "MN": "MINNESOTA", "MS": "MISSISSIPPI", "MO": "MISSOURI", "MT": "MONTANA",
             "NE": "NEBRASKA", "NV": "NEVADA", "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY", "NM": "NEW MEXICO",
             "NY": "NEW YORK", "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA", "OH": "OHIO", "OK": "OKLAHOMA",
             "OR": "OREGON", "PA": "PENNSYLVANIA", "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA", "SD": "SOUTH DAKOTA",
             "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH", "VT": "VERMONT", "VA": "VIRGINIA", "WA": "WASHINGTON",
             "WV": "WEST VIRGINIA", "WI": "WISCONSIN", "WY": "WYOMING"}
# An address with no ZIP code (the scripted book's) is matched on these, as it always was.
_FIRST_STATES = ("OH", "WA", "NY", "CA", "TX", "IL")
_STATE_ZIP = re.compile(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")


def us_state(address):
    """The state a US address names before its ZIP code (..., Portland, OR
    97266), for a licence's header; an address with no ZIP is matched on the
    first six states as before; OHIO when none is found."""
    text = (address or "").upper()
    found = [m.group(1) for m in _STATE_ZIP.finditer(text) if m.group(1) in US_STATES]
    if found:
        return US_STATES[found[-1]]
    for code in _FIRST_STATES:
        if f", {code} " in text or text.endswith(f" {code}"):
            return US_STATES[code]
    return "OHIO"

FONT = "Arial, 'Segoe UI', Helvetica, sans-serif"
MONO = "'Courier New', Consolas, monospace"


# ── MRZ ──────────────────────────────────────────────────────────────────────

def _check_digit(field):
    weights = (7, 3, 1)
    total = 0
    for i, ch in enumerate(field):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch.upper()) - 55
        else:
            v = 0
        total += v * weights[i % 3]
    return str(total % 10)


def _yymmdd(iso):
    d = dates.parse(iso)
    return d.strftime("%y%m%d") if d else "000000"


def _pad(s, n):
    return (s[:n]).ljust(n, "<")


def _mrz_td3(client, number, expiry):
    """Two lines of 44 for a passport."""
    nat = ISO3.get(client["nationality"], "XXX")
    surname, given = split_name(client["legal_name"].title())
    line1 = _pad("P<" + nat + mrz_name(surname, given), 44)
    num = _pad(mrz_field(number), 9)
    dob = _yymmdd(client["dob"])
    exp = _yymmdd(expiry)
    sex = "F" if client.get("gender") == "F" else "M"
    body = num + _check_digit(num) + nat + dob + _check_digit(dob) + sex + exp + _check_digit(exp) + "<" * 14
    composite = _check_digit(num + _check_digit(num) + dob + _check_digit(dob) + exp + _check_digit(exp) + "<" * 14)
    line2 = _pad(body + "<" + composite, 44)
    return line1, line2


def _mrz_td1(client, number, expiry, doc_code="ID"):
    """Three lines of 30 for an ID card."""
    nat = ISO3.get(client["nationality"], "XXX")
    surname, given = split_name(client["legal_name"].title())
    num = _pad(mrz_field(number), 9)
    line1 = _pad(f"{doc_code}{nat}{num}{_check_digit(num)}", 30)
    dob = _yymmdd(client["dob"])
    exp = _yymmdd(expiry)
    sex = "F" if client.get("gender") == "F" else "M"
    line2 = _pad(f"{dob}{_check_digit(dob)}{sex}{exp}{_check_digit(exp)}{nat}", 30)
    line2 = line2[:29] + _check_digit(line1[5:] + line2[:29])
    line3 = _pad(mrz_name(surname, given), 30)
    return line1, line2, line3


# ── drawing primitives ───────────────────────────────────────────────────────

def _defs():
    return (
        '<defs>'
        '<filter id="blur" x="-5%" y="-5%" width="110%" height="110%"><feGaussianBlur stdDeviation="3.2"/></filter>'
        '<pattern id="guilloche" width="14" height="14" patternUnits="userSpaceOnUse">'
        '<circle cx="7" cy="7" r="5" fill="none" stroke="rgba(0,0,0,0.05)" stroke-width="0.8"/></pattern>'
        '<pattern id="bars" width="3" height="40" patternUnits="userSpaceOnUse">'
        '<rect width="1.2" height="40" fill="#222"/></pattern>'
        '</defs>'
    )


def _watermark(w, h):
    return (
        f'<g opacity="0.16" transform="rotate(-24 {w/2} {h/2})" pointer-events="none">'
        f'<text x="{w/2}" y="{h/2 - 10}" text-anchor="middle" font-family="{FONT}" font-size="{int(h*0.13)}" font-weight="700" fill="#b91c1c">SPECIMEN</text>'
        f'<text x="{w/2}" y="{h/2 + 34}" text-anchor="middle" font-family="{FONT}" font-size="{int(h*0.045)}" font-weight="700" fill="#b91c1c" letter-spacing="3">SYNTHETIC DEMO DOCUMENT</text>'
        f'</g>'
    )


def _silhouette(x, y, w, h, tone="#9aa3ad"):
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#dfe4ea" stroke="#c4ccd4"/>'
        f'<circle cx="{x + w/2}" cy="{y + h*0.36}" r="{h*0.18}" fill="{tone}"/>'
        f'<path d="M{x + w*0.12} {y + h*0.98} C{x + w*0.12} {y + h*0.62} {x + w*0.88} {y + h*0.62} {x + w*0.88} {y + h*0.98} Z" fill="{tone}"/>'
    )


def _label(x, y, text, size=9, fill="#5b6470"):
    return f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{fill}">{esc(text)}</text>'


def _value(x, y, text, size=13, weight="700", fill="#111", family=FONT):
    return f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{fill}">{esc(text)}</text>'


def _field(x, y, label, value, size=12):
    return _label(x, y, label) + _value(x, y + 14, value, size)


# Arial Bold advance widths (per 1000 em), enough to tell whether a line of
# bold text fits its column; an accented letter measures as its base letter.
_BOLD = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
                                                667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611)))
_BOLD.update(zip("abcdefghijklmnopqrstuvwxyz", (556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
                                                611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500)))
_BOLD.update({ch: 556 for ch in "0123456789#$"})
_BOLD.update({" ": 278, ",": 278, ".": 278, "-": 333, "/": 278, "(": 333, ")": 333, "&": 722, "'": 238, ":": 333,
              "Đ": 722, "đ": 611})


def text_width(text, size):
    """Roughly how wide `text` prints in bold Arial at `size` px."""
    return sum(_BOLD.get(ch, _BOLD.get(unicodedata.normalize("NFD", ch)[:1], 600)) for ch in text) * size / 1000


def _lines(text, size, width):
    out, line = [], ""
    for word in text.split():
        joined = f"{line} {word}" if line else word
        if line and text_width(joined, size) > width:
            out.append(line)
            line = word
        else:
            line = joined
    return out + [line]


def wraps(text, size, width):
    """Whether `text` needs more than one line in `width` px."""
    return text_width(text, size) > width


def _value_lines(x, y, text, width, max_lines, size=12, step=16):
    """A value that wraps inside its column instead of running into the next
    one; when it needs more than `max_lines` lines the type steps down. Text
    that fits draws exactly as _value draws it."""
    if not wraps(text, size, width):
        return _value(x, y, text, size, "600")
    while size > 8 and len(_lines(text, size, width)) > max_lines:
        size -= 0.5
    return "".join(_value(x, y + i * step * size / 12, line, size, "600") for i, line in enumerate(_lines(text, size, width)))


def _mrz_lines(x, y, lines, w):
    out = f'<rect x="{x - 10}" y="{y - 18}" width="{w}" height="{len(lines) * 22 + 12}" fill="#f7f7f2"/>'
    for i, line in enumerate(lines):
        out += (f'<text x="{x}" y="{y + i*22}" font-family="{MONO}" font-size="16.5" fill="#111" '
                f'xml:space="preserve" textLength="{w - 20}" lengthAdjust="spacing">{esc(line)}</text>')
    return out


def _footer(w, h, text="Generated for demonstration only. Not a real document."):
    return f'<text x="{w/2}" y="{h - 8}" text-anchor="middle" font-family="{FONT}" font-size="8" fill="#8b949e">{esc(text)}</text>'


def _stamp(x, y, text, color="#b91c1c"):
    return (f'<g transform="rotate(-18 {x} {y})" opacity="0.85">'
            f'<rect x="{x - 70}" y="{y - 20}" width="140" height="40" rx="6" fill="none" stroke="{color}" stroke-width="3"/>'
            f'<text x="{x}" y="{y + 8}" text-anchor="middle" font-family="{FONT}" font-size="22" font-weight="800" fill="{color}" letter-spacing="2">{esc(text)}</text></g>')


def _city(client):
    """The city from a comma-separated address, without any postcode."""
    parts = [p.strip() for p in (client.get("address") or "").split(",") if p.strip()]
    if len(parts) < 2:
        return ""
    words = [w for w in parts[-2].split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words).upper()


def _long_date(iso):
    d = dates.parse(iso)
    return d.strftime("%d %b %Y").upper() if d else ""


def _expiry(case, years=8):
    """An expiry `years` after the seed's issue date; in the past when the
    document is flagged expired."""
    issue = case["created_at"][:10]
    expired = any(d.get("quality") == "expired" for d in case["documents"])
    return dates.add_days(issue + " 00:00:00", -400 if expired else years * 365)[:10]


def _issue_date(case):
    return dates.add_days(case["created_at"][:10] + " 00:00:00", -730)[:10]


# ── card families ────────────────────────────────────────────────────────────

def passport_svg(case, doc):
    c = case["client"]
    W, H = 640, 440
    nat = c["nationality"]
    country = COUNTRY_NAMES.get(nat, nat)
    number = c["id_number"]
    expiry = _expiry(case, 10)
    issue = _issue_date(case)
    surname, given = split_name(c["legal_name"].title())
    body = _defs()
    body += f'<rect width="{W}" height="{H}" rx="14" fill="#f4efe4"/><rect width="{W}" height="{H}" rx="14" fill="url(#guilloche)"/>'
    body += f'<rect x="0" y="0" width="{W}" height="54" rx="14" fill="#7b1e2b"/>'
    body += _value(24, 34, country, 15, "700", "#fff")
    body += _value(W - 24, 34, "PASSPORT / PASSEPORT", 13, "700", "#f5d6da").replace('x="616"', f'x="{W-24}" text-anchor="end"')
    if doc.get("side") == "back":
        body += _label(24, 90, "ENDORSEMENTS / OBSERVATIONS")
        body += _value(24, 112, "NONE", 12, "600")
        body += _label(24, 150, "EMERGENCY CONTACT")
        body += _value(24, 170, "Not recorded", 12, "500", "#444")
        body += _label(24, 210, "ISSUING AUTHORITY")
        body += _value(24, 230, f"PASSPORT OFFICE - {ISO3.get(nat, nat)}", 12, "600")
        body += f'<rect x="{W-200}" y="80" width="160" height="60" fill="url(#bars)"/>'
        body += _label(W - 200, 158, "SPECIMEN BARCODE")
    else:
        body += _silhouette(28, 78, 150, 190)
        x = 210
        body += _label(x, 86, "Type / Type") + _value(x, 102, "P", 13)
        body += _label(x + 70, 86, "Code / Code") + _value(x + 70, 102, ISO3.get(nat, nat), 13)
        body += _label(x + 170, 86, "Passport No. / No du passeport") + _value(x + 170, 102, number, 14, family=MONO)
        if c.get("native_name"):
            body += _label(x, 126, "Surname / Given names (native)") + _value(x, 142, c["native_name"], 13, "600")
            y0 = 160
        else:
            y0 = 126
        body += _field(x, y0 + 4, "Surname / Nom", surname.upper(), 15)
        body += _field(x, y0 + 40, "Given names / Prenoms", given.upper(), 14)
        body += _field(x, y0 + 76, "Nationality / Nationalite", country.title(), 12)
        body += _field(x + 230, y0 + 76, "Sex / Sexe", "F" if c.get("gender") == "F" else "M", 12)
        body += _field(x, y0 + 112, "Date of birth / Date de naissance", _long_date(c["dob"]), 12)
        body += _field(x + 230, y0 + 112, "Place of birth / Lieu de naissance", _city(c) or country.title(), 11)
        body += _field(x, y0 + 148, "Date of issue / Date de delivrance", _long_date(issue), 12)
        body += _field(x + 230, y0 + 148, "Date of expiry / Date d'expiration", _long_date(expiry), 12)
        l1, l2 = _mrz_td3(c, number, expiry)
        body += _mrz_lines(28, 396, [l1, l2], W - 46)
    if any(d.get("quality") == "expired" for d in case["documents"]):
        body += _stamp(W - 130, 300, "EXPIRED")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap(W, H, body, doc)


# A US surname's particles belong to the surname a licence prints in field 1:
# ANDREW R DE LUCA is DE LUCA, ANDREW R (not LUCA, ANDREW R DE).
US_SURNAME_PARTICLES = {"DE", "DEL", "DELA", "DA", "DI", "DU", "LA", "LE", "VAN", "VON", "DER", "DEN", "MC", "MAC", "ST"}


def us_split_name(full):
    """(surname, given names) as a US licence prints them: the last word and
    the particles before it."""
    parts = (full or "").split()
    i = len(parts) - 1
    while i > 1 and parts[i - 1].upper() in US_SURNAME_PARTICLES:
        i -= 1
    if len(parts) < 3 or i == len(parts) - 1:
        return split_name(full)
    return " ".join(parts[i:]), " ".join(parts[:i])


def driving_licence_svg(case, doc):
    c = case["client"]
    W, H = 640, 400
    addr = c.get("address") or ""
    state = us_state(addr)
    number = c["id_number"]
    expiry = _expiry(case, 5)
    issue = _issue_date(case)
    surname, given = us_split_name(c["legal_name"].title())
    body = _defs()
    body += f'<rect width="{W}" height="{H}" rx="18" fill="#eef3f8"/><rect width="{W}" height="{H}" rx="18" fill="url(#guilloche)"/>'
    body += f'<rect x="0" y="0" width="{W}" height="58" rx="18" fill="#1d4f91"/><rect x="0" y="40" width="{W}" height="18" fill="#1d4f91"/>'
    body += _value(24, 38, state, 22, "800", "#fff")
    body += f'<text x="{W-24}" y="38" text-anchor="end" font-family="{FONT}" font-size="14" font-weight="700" fill="#cfe0f5">DRIVER LICENSE</text>'
    if doc.get("side") == "back":
        body += f'<rect x="24" y="80" width="{W-48}" height="90" fill="url(#bars)"/>'
        body += _label(24, 190, "Rev. 07/2018")
        body += _field(24, 214, "CLASS", "D", 12)
        body += _field(150, 214, "ENDORSEMENTS", "NONE", 12)
        body += _field(320, 214, "RESTRICTIONS", "NONE", 12)
        body += _label(24, 262, "Name or address change must be reported within 30 days.")
        body += _label(24, 278, "Not valid for federal identification unless marked.")
        body += f'<circle cx="{W-90}" cy="290" r="40" fill="none" stroke="#1d4f91" stroke-width="2" stroke-dasharray="4 3"/>'
        body += _label(W - 90, 294, "STATE SEAL", 8, "#1d4f91").replace(f'x="{W-90}"', f'x="{W-90}" text-anchor="middle"')
    else:
        body += _silhouette(24, 78, 130, 165)
        x = 176
        body += _field(x, 82, "4d DLN", number, 14).replace(f'font-family="{FONT}" font-size="14"', f'font-family="{MONO}" font-size="14"')
        body += _field(x + 200, 82, "4b EXP", dates.parse(expiry).strftime("%m/%d/%Y") if dates.parse(expiry) else "", 14)
        body += _field(x, 122, "1", surname.upper(), 16)
        body += _field(x, 158, "2", given.upper(), 15)
        body += _field(x, 194, "8", addr.upper() or f"{state} - ADDRESS ON FILE", 11)
        body += _field(x, 230, "3 DOB", dates.parse(c["dob"]).strftime("%m/%d/%Y") if dates.parse(c["dob"]) else c["dob"], 14)
        body += _field(x + 150, 230, "4a ISS", dates.parse(issue).strftime("%m/%d/%Y") if dates.parse(issue) else "", 13)
        body += _field(x + 300, 230, "15 SEX", "F" if c.get("gender") == "F" else "M", 13)
        body += _field(x, 268, "9 CLASS", "D", 12)
        body += _field(x + 80, 268, "9a END", "NONE", 12)
        body += _field(x + 170, 268, "12 RES", "NONE", 12)
        body += _field(x + 260, 268, "16 HGT", "5'-11\"", 12)
        body += _field(x + 350, 268, "18 EYES", "BRO", 12)
        body += _value(24, 300, "DONOR", 9, "700", "#b91c1c")
        body += f'<text x="{W-24}" y="{H-40}" text-anchor="end" font-family="{FONT}" font-size="20" font-weight="700" fill="#1d4f91" opacity="0.7">{esc(dates.parse(c["dob"]).strftime("%m/%d/%y") if dates.parse(c["dob"]) else "")}</text>'
        body += f'<path d="M30 {H-58} c20 -18 40 6 60 -8 s30 12 55 -4" fill="none" stroke="#333" stroke-width="1.5"/>'
    if any(d.get("quality") == "expired" for d in case["documents"]):
        body += _stamp(W - 130, 320, "EXPIRED")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap(W, H, body, doc)


def national_id_svg(case, doc, title="NATIONAL IDENTITY CARD", code="ID", holder=False, name_label="", address=""):
    """`holder` prints the holder's name above the address on the back, as
    an Aadhaar's address side does when it is the proof of address.
    `name_label` prints the front's name in one field under that label, as
    the card writes it (a Vietnamese name family name first, an Indian one
    surname first or with an initial), instead of a surname and given names;
    `address` is the address the card prints on its front, if any."""
    c = case["client"]
    W, H = 640, 400
    nat = c["nationality"]
    country = COUNTRY_NAMES.get(nat, nat)
    number = c["id_number"]
    expiry = _expiry(case, 10)
    issue = _issue_date(case)
    surname, given = split_name(c["legal_name"].title())
    body = _defs()
    body += f'<rect width="{W}" height="{H}" rx="18" fill="#e9f1ea"/><rect width="{W}" height="{H}" rx="18" fill="url(#guilloche)"/>'
    body += f'<rect x="0" y="0" width="{W}" height="54" rx="18" fill="#1f6b3a"/><rect x="0" y="36" width="{W}" height="18" fill="#1f6b3a"/>'
    body += _value(24, 34, country, 14, "700", "#fff")
    body += f'<text x="{W-24}" y="34" text-anchor="end" font-family="{FONT}" font-size="12" font-weight="700" fill="#d4ecd9">{esc(title)}</text>'
    if doc.get("side") == "back":
        l1, l2, l3 = _mrz_td1(c, number, expiry, code)
        y = 84
        if holder:
            body += _field(24, y, "Name", c["legal_name"], 13)
            y = 130                                                     # the rest moves down under the name
        body += _label(24, y, "Address / Adresse")
        body += _value_lines(24, y + 18, c.get("address") or "Address on file", CARD_ADDRESS_WIDTH, 2)   # above the authority
        body += _label(24, y + 56, "Issuing authority")
        body += _value(24, y + 74, f"CIVIL REGISTRY - {ISO3.get(nat, nat)}", 12, "600")
        body += _field(24, y + 106, "Date of issue", _long_date(issue), 12)
        body += _field(220, y + 106, "Date of expiry", _long_date(expiry), 12)
        body += _mrz_lines(28, 312, [l1, l2, l3], W - 46)
    else:
        body += _silhouette(24, 74, 130, 165)
        x = 176
        if name_label:
            body += _field(x, 82, name_label, (c.get("native_name") or c["legal_name"]).upper(), 15)
            y0 = 120
        else:
            body += _field(x, 82, "Surname", surname.upper(), 15)
            body += _field(x, 120, "Given names", given.upper(), 14)
            if c.get("native_name"):
                body += _field(x, 158, "Name (native script)", c["native_name"], 13)
                y0 = 196
            else:
                y0 = 158
        body += _field(x, y0, "Nationality", country.title(), 12)
        body += _field(x + 230, y0, "Sex", "F" if c.get("gender") == "F" else "M", 12)
        body += _field(x, y0 + 38, "Date of birth", _long_date(c["dob"]), 12)
        body += _field(x + 230, y0 + 38, "Date of expiry", _long_date(expiry), 12)
        body += _field(x, y0 + 76, "ID number", number, 14).replace(f'font-family="{FONT}" font-size="14"', f'font-family="{MONO}" font-size="14"')
        if address:
            ya = max(y0 + 112, 252)                                     # below the photo, which ends at y 239
            body += _label(24, ya, "Address")
            body += _value_lines(24, ya + 18, address, CARD_FRONT_ADDRESS_WIDTH, 3, 11)
        body += f'<rect x="{W-200}" y="{H-70}" width="170" height="26" fill="url(#bars)"/>'
    if any(d.get("quality") == "expired" for d in case["documents"]):
        body += _stamp(W - 130, 320, "EXPIRED")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap(W, H, body, doc)


def residence_permit_svg(case, doc):
    return national_id_svg(case, doc, title="RESIDENCE PERMIT / PERMIS DE SEJOUR", code="IR")


def trade_licence_svg(case, doc):
    """The company's registration document. The agency record may name the
    document, the issuing authority, the legal form and the licensed
    activity (the reconciliation-log book does, from the company's name and
    country); otherwise the card reads as a free-zone trade licence."""
    c = case["client"]
    a = case.get("agency") or {}
    W, H = 620, 800
    title = a.get("document_title") or "TRADE LICENSE"
    authority = a.get("authority") or f"{COUNTRY_NAMES.get(c['nationality'], c['nationality'])} - FREE ZONE AUTHORITY (DEMO)"
    body = _defs()
    body += f'<rect width="{W}" height="{H}" fill="#fffdf7"/><rect x="18" y="18" width="{W-36}" height="{H-36}" fill="none" stroke="#8a6d1f" stroke-width="3"/>'
    body += f'<rect x="26" y="26" width="{W-52}" height="{H-52}" fill="none" stroke="#c9b26a" stroke-width="1"/>'
    body += f'<text x="{W/2}" y="80" text-anchor="middle" font-family="{FONT}" font-size="{26 if len(title) <= 16 else 20}" font-weight="800" fill="#5b4413" letter-spacing="{4 if len(title) <= 16 else 2}">{esc(title)}</text>'
    body += f'<text x="{W/2}" y="104" text-anchor="middle" font-family="{FONT}" font-size="12" fill="#7a6a3a">{esc(authority)}</text>'
    body += f'<line x1="60" y1="120" x2="{W-60}" y2="120" stroke="#c9b26a"/>'
    # A registration certificate carries one number, the registration itself.
    rows = ([] if a.get("document_title") else [("License No.", c["id_number"])]) + [
        ("Legal name", a.get("company_name") or c["legal_name"]),
        ("Registration No.", a.get("registration_number", "")), ("Legal form", a.get("legal_form") or "Free Zone Establishment"),
        ("Registered address", a.get("registered_address") or c.get("address", "")),
        ("Principal activity" if a.get("document_title") else "Licensed activity",
         a.get("activity") or "General trading; import and export of electronic goods"),
        ("Manager / Representative", a.get("representative", "")),
        ("Issue date", _long_date(_issue_date(case))), ("Expiry date", _long_date(_expiry(case, 1))),
    ]
    y = 160
    for label, value in rows:
        body += _label(60, y, label.upper(), 9, "#7a6a3a")
        body += _value(60, y + 18, value, 14 if len(str(value)) < 40 else 11, "600")
        y += 52
    body += _label(60, y + 10, "BENEFICIAL OWNERS", 9, "#7a6a3a")
    yy = y + 30
    for u in a.get("ubos", []):
        body += _value(60, yy, f"{u['name']}  -  {u['pct']}%  ({u['nationality']})", 12, "500")
        yy += 20
    body += f'<circle cx="{W-120}" cy="{H-120}" r="52" fill="none" stroke="#8a6d1f" stroke-width="2.5" stroke-dasharray="6 4"/>'
    body += f'<text x="{W-120}" y="{H-116}" text-anchor="middle" font-family="{FONT}" font-size="10" font-weight="700" fill="#8a6d1f">AUTHORITY SEAL</text>'
    body += f'<path d="M70 {H-90} c30 -30 60 10 100 -12 s40 18 80 -6" fill="none" stroke="#333" stroke-width="1.5"/>'
    body += _label(70, H - 70, "Authorised signatory")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap(W, H, body, doc)


BILL_ADDRESS_WIDTH = 320       # a statement's service address column
CARD_ADDRESS_WIDTH = 592       # the address line across the back of an ID card
CARD_FRONT_ADDRESS_WIDTH = 400  # the address under the front's fields, clear of the bar code at x 440


def service_address(address, country_code):
    """The service address as a statement prints it, with the country."""
    country = vocab.COUNTRY_NAMES.get((country_code or "").upper(), country_code or "")
    address = address or "Address on file"
    return f"{address}, {country}" if country and country.lower() not in address.lower() else address


def utility_bill_svg(case, doc, provider="Northgate Utilities", kind="ELECTRICITY STATEMENT"):
    c = case["client"]
    W, H = 620, 800
    page2 = doc.get("side") == "page 2"
    issue = doc.get("issued_on") or case["created_at"][:10]
    period_end = dates.add_days(issue + " 00:00:00", -12)[:10]
    period_start = dates.add_days(period_end + " 00:00:00", -30)[:10]
    # A stored issue date is the truth the POA checks read; the `expired` quality flag only moves an undated bill.
    old = (not doc.get("issued_on")) and any(d.get("quality") == "expired" for d in case["documents"] if d.get("kind") in ("utility_bill", "bank_statement"))
    if old:
        period_end = dates.add_days(issue + " 00:00:00", -240)[:10]
        period_start = dates.add_days(period_end + " 00:00:00", -30)[:10]
    body = _defs()
    body += f'<rect width="{W}" height="{H}" fill="#ffffff"/>'
    body += f'<rect x="0" y="0" width="{W}" height="70" fill="#0f766e"/>'
    body += _value(40, 44, provider, 22, "800", "#fff")
    body += f'<text x="{W-40}" y="44" text-anchor="end" font-family="{FONT}" font-size="12" font-weight="700" fill="#ccfbf1">{esc(kind)}</text>'
    if page2:
        body += _value(40, 110, "Usage detail", 16, "700")
        body += _label(40, 140, "PERIOD") + _label(200, 140, "READING") + _label(340, 140, "USAGE (kWh)") + _label(480, 140, "AMOUNT")
        y = 165
        for i in range(6):
            body += _value(40, y, dates.add_days(period_start + " 00:00:00", i * 5)[:10], 11, "500")
            body += _value(200, y, f"{48210 + i*37}", 11, "500")
            body += _value(340, y, f"{37 + i}", 11, "500")
            body += _value(480, y, f"{11.20 + i*0.3:.2f}", 11, "500")
            y += 22
        body += f'<line x1="40" y1="{y}" x2="{W-40}" y2="{y}" stroke="#ddd"/>'
        body += _value(40, y + 30, "How to pay", 13, "700")
        body += _label(40, y + 50, "Online, by bank transfer or at any authorised payment point. Reference your account number.")
        body += _label(40, y + 70, "Page 2 of 2")
    else:
        body += _label(40, 100, "ACCOUNT HOLDER")
        body += _value(40, 118, c["legal_name"], 14)
        body += _label(40, 140, "SERVICE ADDRESS")
        # wrapped left of the statement date (x 380), above the amount box (y 230)
        body += _value_lines(40, 158, service_address(c.get("address"), doc.get("country")), BILL_ADDRESS_WIDTH, 4)
        body += _label(380, 100, "ACCOUNT NUMBER") + _value(380, 118, (doc.get("country") or "NG").upper() + "-" + c["id_number"][-8:].replace("-", ""), 13, family=MONO)
        body += _label(380, 140, "STATEMENT DATE") + _value(380, 158, _long_date(issue if not old else period_end), 12)
        body += _label(380, 180, "BILLING PERIOD") + _value(380, 198, f"{period_start} to {period_end}", 11)
        body += f'<rect x="40" y="230" width="{W-80}" height="90" rx="8" fill="#f0fdfa" stroke="#99f6e4"/>'
        body += _label(56, 254, "AMOUNT DUE") + _value(56, 290, "$ 84.60", 28, "800", "#0f766e")
        body += _label(300, 254, "DUE DATE") + _value(300, 290, _long_date(dates.add_days(issue + " 00:00:00", 14)[:10]), 16)
        body += _value(40, 360, "Summary of charges", 14, "700")
        for i, (k, v) in enumerate([("Previous balance", "$ 79.10"), ("Payment received - thank you", "-$ 79.10"),
                                    ("Electricity supply (231 kWh)", "$ 62.40"), ("Distribution and metering", "$ 17.90"),
                                    ("Taxes and levies", "$ 4.30"), ("Total new charges", "$ 84.60")]):
            y = 388 + i * 24
            body += _value(40, y, k, 11, "700" if i == 5 else "500")
            body += f'<text x="{W-40}" y="{y}" text-anchor="end" font-family="{FONT}" font-size="11" font-weight="{"700" if i == 5 else "500"}" fill="#111">{esc(v)}</text>'
        body += f'<line x1="40" y1="{388+5*24+10}" x2="{W-40}" y2="{388+5*24+10}" stroke="#ddd"/>'
        body += _label(40, 580, "Questions about this statement? Contact us through the customer portal quoting your account number.")
        body += _label(40, 600, "Page 1 of 2")
    body += _watermark(W, H) + _footer(W, H)
    return _wrap(W, H, body, doc)


def bank_statement_svg(case, doc):
    return utility_bill_svg(case, doc, provider="Harbourline Bank", kind="ACCOUNT STATEMENT")


# ── assembly ─────────────────────────────────────────────────────────────────

RENDERERS = {
    "passport": passport_svg, "driving_licence": driving_licence_svg, "national_id": national_id_svg,
    "residence_permit": residence_permit_svg, "trade_licence": trade_licence_svg,
    "utility_bill": utility_bill_svg, "bank_statement": bank_statement_svg,
}


def _wrap(W, H, body, doc):
    """Apply the quality defect and close the SVG. `cropped` cuts the viewBox
    so the card's right and bottom edges are missing, the way a badly framed
    photo is; `blurred` puts the whole card behind a Gaussian blur."""
    quality = doc.get("quality", "ok")
    view = f"0 0 {W} {H}"
    inner = body
    if quality == "cropped":
        view = f"{int(W*0.18)} {int(H*0.12)} {int(W*0.62)} {int(H*0.62)}"
    if quality == "blurred":
        inner = f'<g filter="url(#blur)">{body}</g>'
    # A photographed card sits on a dark table; the outer frame gives the
    # viewer something to zoom against.
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" width="{W}" height="{H}" '
            f'font-family="{FONT}" style="background:#2b2b2b">'
            f'<rect x="-2000" y="-2000" width="5000" height="5000" fill="#2b2b2b"/>'
            f'{inner}</svg>')


def render(case, doc):
    fn = RENDERERS.get(doc.get("kind"), national_id_svg)
    return fn(case, doc)


def awaiting_svg(title):
    """The slot of a document whose real image has not been uploaded yet."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 400" width="640" height="400" font-family="{FONT}">'
            '<rect x="8" y="8" width="624" height="384" rx="18" fill="#f1f5f9" stroke="#94a3b8" stroke-width="3" stroke-dasharray="14 10"/>'
            f'<text x="320" y="186" text-anchor="middle" font-size="26" font-weight="600" fill="#334155">{esc(title)}</text>'
            '<text x="320" y="228" text-anchor="middle" font-size="17" fill="#64748b">Awaiting upload: no image yet</text></svg>')
