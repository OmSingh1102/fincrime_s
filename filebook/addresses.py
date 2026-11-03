"""Address rules: the AI Agent's address check and the proof of address's write-back use them.

normalise() turns one address line into comparable parts (country, city,
postal code, unit, house number, street tokens) through the country's own
conventions: USPS abbreviations and ZIP+4 in the US; S/O-D/O-W/O prefixes,
landmarks, legacy city names, district names and PIN codes in India; ward
and district abbreviations, alley numbering and the 2025 administrative
mergers in Vietnam. compare() says whether two lines point at the same
home, the same locality, somewhere else, or another country. analyse()
runs the four addresses a case carries and reports what it found, with
flags that say whether a disagreement is explained by the documents.
"""
import re

from fincrime.filebook.similarity import fold

# ── tables ───────────────────────────────────────────────────────────────────

USPS = {"STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
        "PLACE": "PL", "HIGHWAY": "HWY", "PARKWAY": "PKWY", "CIRCLE": "CIR", "TERRACE": "TER", "SQUARE": "SQ",
        "TRAIL": "TRL", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW",
        "SOUTHEAST": "SE", "SOUTHWEST": "SW", "APARTMENT": "APT", "SUITE": "STE", "FLOOR": "FL", "BUILDING": "BLDG",
        "ROOM": "RM", "PLAZA": "PLZ", "EXPRESSWAY": "EXPY", "TURNPIKE": "TPKE", "CENTER": "CTR", "CENTRE": "CTR",
        "HEIGHTS": "HTS", "JUNCTION": "JCT", "MOUNT": "MT", "MOUNTAIN": "MTN", "SAINT": "ST", "FORT": "FT"}
UNIT_WORDS = {"APT", "STE", "UNIT", "FL", "RM", "BLDG", "FLAT", "HOUSE", "H", "NO", "VILLA", "WING", "BLOCK", "SO", "HEM",
              "NGO", "NGACH", "AP", "THON", "BOX", "PSC", "CMR", "FLOOR", "DOOR", "PLOT", "SHOP", "OFFICE"}
STREET_WORDS = {"ST", "AVE", "BLVD", "RD", "DR", "LN", "CT", "PL", "HWY", "PKWY", "CIR", "TER", "SQ", "TRL", "ROAD",
                "STREET", "MARG", "GALI", "CROSS", "MAIN", "LANE", "DUONG", "WAY"}
PO_BOX = re.compile(r"\b(?:P\.?\s?O\.?\s?BOX|POST\s+OFFICE\s+BOX|POBOX)\b", re.I)
MILITARY = re.compile(r"\b(?:APO|FPO|DPO)\s+(?:AA|AE|AP)\b", re.I)
RELATION = re.compile(r"^\s*(?:S/O|D/O|W/O|C/O|H/O|S\.O\.|D\.O\.|W\.O\.|C\.O\.)\s*:?\s*[^,]+,\s*", re.I)
LANDMARK = re.compile(r"^\s*(?:NEAR|OPP\.?|OPPOSITE|BEHIND|BESIDE|NEXT TO|ADJACENT TO|BACKSIDE)\b", re.I)

IN_LEGACY_CITY = {"MADRAS": "CHENNAI", "BOMBAY": "MUMBAI", "CALCUTTA": "KOLKATA", "BANGALORE": "BENGALURU",
                  "GURGAON": "GURUGRAM", "ALLAHABAD": "PRAYAGRAJ", "POONA": "PUNE", "BARODA": "VADODARA", "MYSORE": "MYSURU",
                  "TRIVANDRUM": "THIRUVANANTHAPURAM", "COCHIN": "KOCHI", "MANGALORE": "MANGALURU", "BELGAUM": "BELAGAVI",
                  "HUBLI": "HUBBALLI", "CAWNPORE": "KANPUR", "BENARES": "VARANASI", "BANARAS": "VARANASI", "SIMLA": "SHIMLA",
                  "PANJIM": "PANAJI", "TANJORE": "THANJAVUR", "TRICHY": "TIRUCHIRAPPALLI", "VIZAG": "VISAKHAPATNAM",
                  "OOTY": "UDHAGAMANDALAM", "CALICUT": "KOZHIKODE", "QUILON": "KOLLAM", "TUTICORIN": "THOOTHUKUDI",
                  "ORISSA": "ODISHA", "PONDICHERRY": "PUDUCHERRY", "DELHI": "NEW DELHI"}
IN_DISTRICT = {"BIDHANNAGAR": "KOLKATA", "NORTH 24 PARGANAS": "KOLKATA", "SOUTH 24 PARGANAS": "KOLKATA",
               "GAUTAM BUDDHA NAGAR": "NOIDA", "GREATER NOIDA": "NOIDA", "SECUNDERABAD": "HYDERABAD", "SAS NAGAR": "MOHALI",
               "RANGAREDDY": "HYDERABAD", "MEDCHAL": "HYDERABAD", "THANE": "THANE", "BENGALURU URBAN": "BENGALURU",
               "KANCHEEPURAM": "CHENNAI", "TIRUVALLUR": "CHENNAI", "ERNAKULAM": "KOCHI", "DAKSHINA KANNADA": "MANGALURU",
               "KAMRUP METROPOLITAN": "GUWAHATI", "KAMRUP": "GUWAHATI"}
IN_STATES = {"ANDHRA PRADESH", "ARUNACHAL PRADESH", "ASSAM", "BIHAR", "CHHATTISGARH", "GOA", "GUJARAT", "HARYANA",
             "HIMACHAL PRADESH", "JHARKHAND", "KARNATAKA", "KERALA", "MADHYA PRADESH", "MAHARASHTRA", "MANIPUR", "MEGHALAYA",
             "MIZORAM", "NAGALAND", "ODISHA", "ORISSA", "PUNJAB", "RAJASTHAN", "SIKKIM", "TAMIL NADU", "TELANGANA", "TRIPURA",
             "UTTAR PRADESH", "UTTARAKHAND", "WEST BENGAL", "DELHI", "CHANDIGARH", "PUDUCHERRY", "PONDICHERRY",
             "JAMMU AND KASHMIR", "LADAKH", "ANDAMAN AND NICOBAR ISLANDS", "LAKSHADWEEP", "DADRA AND NAGAR HAVELI"}

US_CITY_GROUPS = [
    ["NEW YORK", "BROOKLYN", "QUEENS", "BRONX", "STATEN ISLAND", "MANHATTAN", "FLUSHING", "JAMAICA", "ASTORIA",
     "LONG ISLAND CITY", "HARLEM", "WILLIAMSBURG", "FOREST HILLS", "BAYSIDE", "JACKSON HEIGHTS"],
    ["SEATTLE", "BALLARD", "CAPITOL HILL", "FREMONT", "QUEEN ANNE", "WEST SEATTLE"],
    ["MIAMI", "MIAMI BEACH", "SOUTH BEACH", "LITTLE HAVANA", "CORAL GABLES", "BRICKELL"],
    ["LOS ANGELES", "HOLLYWOOD", "VENICE", "SILVER LAKE", "WESTWOOD", "KOREATOWN", "ECHO PARK", "VAN NUYS", "SHERMAN OAKS"],
    ["WESTMINSTER", "LITTLE SAIGON"], ["SAN FRANCISCO", "MISSION DISTRICT", "SUNSET DISTRICT", "RICHMOND DISTRICT"],
    ["CHICAGO", "WICKER PARK", "LINCOLN PARK", "HYDE PARK", "UPTOWN"], ["WASHINGTON", "GEORGETOWN", "CAPITOL HILL DC"],
    ["HOUSTON", "MONTROSE", "THE HEIGHTS"], ["PHILADELPHIA", "CENTER CITY", "SOUTH PHILLY"], ["BOSTON", "BACK BAY", "SOUTH END"],
    ["DALLAS", "UPTOWN DALLAS", "DEEP ELLUM"], ["SAN JOSE", "WILLOW GLEN"], ["PORTLAND", "PEARL DISTRICT"],
    ["PHOENIX", "ARCADIA"], ["ATLANTA", "BUCKHEAD", "MIDTOWN"], ["DENVER", "CAPITOL HILL DENVER", "LODO"],
]
_US_GROUP = {}
for _g in US_CITY_GROUPS:          # the USPS city comes first in each group
    for _c in _g:
        _US_GROUP[_c] = _g[0]

VN_ABBR = {"P": "PHUONG", "PHUONG": "PHUONG", "WARD": "PHUONG", "Q": "QUAN", "QUAN": "QUAN", "DISTRICT": "QUAN",
           "H": "HUYEN", "HUYEN": "HUYEN", "TX": "THIXA", "TP": "TP", "CITY": "TP", "TINH": "TINH", "PROVINCE": "TINH",
           "XA": "XA", "COMMUNE": "XA", "THON": "THON", "VILLAGE": "THON", "AP": "AP", "HAMLET": "AP", "KP": "KP",
           "SO": "SO", "HEM": "HEM", "NGO": "HEM", "NGACH": "NGACH", "ALLEY": "HEM", "LANE": "HEM", "DUONG": "", "STREET": "",
           "TT": "THITRAN", "TOWN": "THITRAN"}
VN_CITY = {"HO CHI MINH": "HO CHI MINH", "HCM": "HO CHI MINH", "HCMC": "HO CHI MINH", "SAI GON": "HO CHI MINH",
           "SAIGON": "HO CHI MINH", "HANOI": "HA NOI", "HA NOI": "HA NOI", "DANANG": "DA NANG", "DA NANG": "DA NANG",
           "CAN THO": "CAN THO", "HAI PHONG": "HAI PHONG", "HUE": "HUE", "THU DUC": "HO CHI MINH"}
# The 2025 provincial mergers (effective 1 July 2025): old province → the unit it joined.
VN_MERGED = {"BINH DUONG": "HO CHI MINH", "BA RIA - VUNG TAU": "HO CHI MINH", "BA RIA VUNG TAU": "HO CHI MINH",
             "VUNG TAU": "HO CHI MINH", "THUA THIEN HUE": "HUE", "HAI DUONG": "HAI PHONG", "BAC GIANG": "BAC NINH",
             "QUANG NAM": "DA NANG", "HAU GIANG": "CAN THO", "SOC TRANG": "CAN THO", "BEN TRE": "VINH LONG",
             "TRA VINH": "VINH LONG", "KIEN GIANG": "AN GIANG", "BINH THUAN": "LAM DONG", "DAK NONG": "LAM DONG",
             "NINH THUAN": "KHANH HOA", "BINH PHUOC": "DONG NAI", "LONG AN": "TAY NINH", "PHU YEN": "DAK LAK",
             "BINH DINH": "GIA LAI", "KON TUM": "QUANG NGAI", "QUANG BINH": "QUANG TRI", "THAI BINH": "HUNG YEN",
             "HA NAM": "NINH BINH", "NAM DINH": "NINH BINH", "VINH PHUC": "PHU THO", "HOA BINH": "PHU THO",
             "BAC KAN": "THAI NGUYEN", "HA GIANG": "TUYEN QUANG", "YEN BAI": "LAO CAI", "TIEN GIANG": "DONG THAP",
             "BAC LIEU": "CA MAU"}
VN_MUNICIPAL = {"HA NOI", "HO CHI MINH", "DA NANG", "HAI PHONG", "CAN THO", "HUE"}

COUNTRY_WORDS = {"UNITED ARAB EMIRATES": "AE", "UAE": "AE", "CANADA": "CA", "USA": "US", "UNITED STATES": "US",
                 "U S A": "US", "AUSTRALIA": "AU", "GERMANY": "DE", "SOUTH KOREA": "KR", "KOREA": "KR", "SINGAPORE": "SG",
                 "QATAR": "QA", "OMAN": "OM", "INDIA": "IN", "VIETNAM": "VN", "VIET NAM": "VN", "UNITED KINGDOM": "GB",
                 "UK": "GB", "JAPAN": "JP", "FRANCE": "FR", "MALAYSIA": "MY", "THAILAND": "TH", "SAUDI ARABIA": "SA",
                 "KUWAIT": "KW", "BAHRAIN": "BH", "HONG KONG": "HK", "TAIWAN": "TW", "CHINA": "CN", "NEW ZEALAND": "NZ"}
US_STATE_CODES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
                  "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
                  "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "AA", "AE", "AP"}


# ── one address ──────────────────────────────────────────────────────────────

def _country_of(text_folded, default):
    for name, code in sorted(COUNTRY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(^|[ ,])" + re.escape(name) + r"\s*$", text_folded):
            return code
    if re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$", text_folded) or MILITARY.search(text_folded):
        return "US"
    if re.search(r"\b\d{6}\b", text_folded) and any(s in text_folded for s in IN_STATES):
        return "IN"
    if re.search(r"\b(?:TP|PHUONG|QUAN|TINH|HUYEN|XA|THON)\b", text_folded):
        return "VN"
    return default


def _unit(segment_folded):
    """Flat 704 / Flat No. 704 / H.No. 70 / #393 / B-302 / B/202 / 801-A / A-401 /
    Apt 9F / Unit 8B / #1D → (unit, wing)."""
    s = segment_folded
    m = re.search(r"\b(?:FLAT|HOUSE|H|VILLA|APT|APARTMENT|UNIT|STE|SUITE|RM|ROOM|PLOT|DOOR|SHOP)\b\s*(?:NO)?\s*#?\s*(\d+[A-Z]?(?:/[A-Z0-9]+)?)", s)
    if m:
        wing = ""
        w = re.search(r"\b([A-Z])\s*-?\s*WING\b", s)
        if w:
            wing = w.group(1)
        return m.group(1), wing
    m = re.search(r"#\s*(\d+[A-Z]?(?:/[A-Z0-9]+)?)", s)
    if m:
        return m.group(1), ""
    m = re.search(r"\b([A-Z])\s*[-/]\s*(\d{2,4})\b", s)
    if m:
        return m.group(2), m.group(1)
    m = re.search(r"\b(\d{2,4})\s*-\s*([A-Z])\b", s)
    if m:
        return m.group(1), m.group(2)
    return "", ""


def normalise(text, country):
    """One address line → its comparable parts. `country` is the case's
    passport country, the default when the line names no other."""
    display = (text or "").strip()
    flags, changes = [], []
    if not display:
        return {"display": "", "empty": True, "country": "", "city": "", "city_key": "", "region": "", "postal": "",
                "postal5": "", "unit": "", "wing": "", "house": "", "numbers": [], "street": [], "flags": [], "changes": [],
                "po_box": False, "military": False}
    work = display.replace("#", " UNIT ")
    if RELATION.match(work):
        work = RELATION.sub("", work)
        flags.append("co_prefix_stripped")
        changes.append("relationship prefix (S/O, D/O, W/O, C/O) removed")
    segments = [s.strip() for s in work.split(",") if s.strip()]
    kept = []
    for seg in segments:
        if LANDMARK.match(seg):
            flags.append("landmark_stripped")
            changes.append(f"landmark '{seg}' dropped")
        else:
            kept.append(seg)
    segments = kept
    folded_all = fold(", ".join(segments)).replace(" ,", ",")
    cc = _country_of(folded_all, country)
    po_box = bool(PO_BOX.search(display))
    military = bool(MILITARY.search(display))
    if po_box:
        flags.append("po_box")
    if military:
        flags.append("military")

    postal, postal5 = "", ""
    if cc == "US":
        # the ZIP ends the address, before any country after it: a five-digit
        # unit or house number earlier on is not it (Unit 15324 Box 77, APO AP 96205, United States)
        head = list(segments)
        while len(head) > 1 and fold(head[-1]) in COUNTRY_WORDS:
            head.pop()
        m = re.search(r"\b(\d{5})(?:-(\d{4}))?\s*$", fold(", ".join(head))) or re.search(r"\b(\d{5})(?:-(\d{4}))?\b", folded_all)
        if m:
            postal = m.group(0).strip()
            postal5 = m.group(1)
            if m.group(2):
                flags.append("zip4_trimmed")
                changes.append("ZIP+4 compared on its five digits")
    elif cc == "IN":
        m = re.search(r"PIN\s*:?\s*(\d{6})", folded_all) or re.search(r"\b(\d{6})\b", folded_all)
        if m:
            postal = postal5 = m.group(1)
    else:
        m = re.search(r"\b(\d{5,6})\b", folded_all)
        if m and cc not in ("VN",):
            postal = postal5 = m.group(1)

    folded_segments = [fold(s) for s in segments]
    city, region, city_key = "", "", ""
    street_segments = []

    if cc == "US":
        # the country after the address ('..., FL 33432, United States') is neither the city nor the street
        while folded_segments and folded_segments[-1].replace(".", "") in COUNTRY_WORDS:
            folded_segments.pop()
        tail = [s for s in folded_segments if re.search(r"\b[A-Z]{2}\s+\d{5}", s) or MILITARY.search(s)]
        body = [s for s in folded_segments if s not in tail]
        if tail:
            t = tail[-1]
            m = re.match(r"^(.*?)\b([A-Z]{2})\s+\d{5}", t)
            if m:
                region = m.group(2)
                if m.group(1).strip():
                    city = m.group(1).strip()
            if MILITARY.search(t):
                city = "APO"
                region = re.search(r"\b(AA|AE|AP)\b", t).group(1)
        if not city and body:
            city = body[-1]
            body = body[:-1]
        if city in _US_GROUP and _US_GROUP[city] != city:
            flags.append("neighbourhood_alias")
            changes.append(f"'{city.title()}' is a neighbourhood; the USPS city is {_US_GROUP[city].title()}")
        city_key = _US_GROUP.get(city, city)
        street_segments = body
    elif cc == "IN":
        state = ""
        body = []
        for s in folded_segments:
            s2 = re.sub(r"\s*-\s*\d{6}\s*$", "", s).replace("PIN:", "PIN ").strip()
            s2 = re.sub(r"\bPIN\s*\d{6}\b", "", s2).strip()
            if s2 in IN_STATES or s2 == "INDIA" or re.sub(r"\s+\d{6}$", "", s2) in IN_STATES:
                state = state or re.sub(r"\s+\d{6}$", "", s2)
                continue
            if re.fullmatch(r"\d{6}", s2) or not s2:
                continue
            body.append(s2)
        region = state
        if body:
            last = body[-1]
            # the passport prints everything in one segment: the city is the last word(s) before PIN
            words = last.split()
            cand2 = " ".join(words[-2:]) if len(words) >= 2 else ""
            if len(body) == 1 and len(words) > 3:
                if cand2 in IN_LEGACY_CITY or cand2 in IN_DISTRICT or cand2 in ("NAVI MUMBAI", "NEW DELHI"):
                    city = cand2
                    street_segments = [" ".join(words[:-2])]
                else:
                    city = words[-1]
                    street_segments = [" ".join(words[:-1])]
            else:
                city = last
                street_segments = body[:-1]
        if city in IN_LEGACY_CITY:
            flags.append("legacy_city")
            changes.append(f"legacy city name '{city.title()}' read as {IN_LEGACY_CITY[city].title()}")
            city_key = IN_LEGACY_CITY[city]
        elif city in IN_DISTRICT:
            flags.append("district_alias")
            changes.append(f"'{city.title()}' is the district or twin city of {IN_DISTRICT[city].title()}")
            city_key = IN_DISTRICT[city]
        else:
            city_key = city
        # a district name that sits in the locality list (Bidhannagar, North 24 Parganas, Gautam Buddha Nagar)
        for seg in list(street_segments):
            if seg in IN_DISTRICT:
                street_segments.remove(seg)
                if not city_key or city_key == seg:
                    city_key = IN_DISTRICT[seg]
                flags.append("district_alias")
    elif cc == "VN":
        province, vn_city, wards, districts = "", "", [], []
        body = []
        for s in folded_segments:
            toks = s.split()
            if not toks:
                continue
            head = toks[0]
            rest = " ".join(toks[1:])
            if head in ("TP", "THANH") or s.endswith(" CITY") or s in VN_CITY:
                name = rest if head in ("TP", "THANH") else s.replace(" CITY", "")
                name = name.replace("PHO ", "").strip()
                name = VN_CITY.get(name, name)
                if name == "THU DUC":
                    flags.append("pre2025_unit")
                    changes.append("Thu Duc City (2021-2025) read as Ho Chi Minh City")
                    name = "HO CHI MINH"
                vn_city = vn_city or name
                continue
            if head in ("TINH", "T") or s.endswith(" PROVINCE"):
                province = rest if head in ("TINH", "T") else s.replace(" PROVINCE", "")
                if head == "T":
                    flags.append("vn_abbreviation")
                continue
            if head in ("P", "PHUONG") or s.endswith(" WARD"):
                wards.append(rest if head in ("P", "PHUONG") else s.replace(" WARD", ""))
                if head == "P" or s.endswith(" WARD"):
                    flags.append("vn_abbreviation")
                continue
            if head in ("Q", "QUAN", "HUYEN", "H", "TX") or s.endswith(" DISTRICT"):
                districts.append(rest if head in ("Q", "QUAN", "HUYEN", "H", "TX") else s.replace(" DISTRICT", ""))
                if head in ("Q", "H") or s.endswith(" DISTRICT"):
                    flags.append("vn_abbreviation")
                continue
            if re.match(r"^(XA|THON|AP|KP|TT)\b", s):
                body.append(s)
                continue
            body.append(s)
        if province in VN_MERGED:
            flags.append("pre2025_unit")
            changes.append(f"{province.title()} province merged into {VN_MERGED[province].title()} (2025)")
            province = VN_MERGED[province]
        if vn_city in VN_MERGED:
            vn_city = VN_MERGED[vn_city]
        if districts:
            flags.append("pre2025_unit")
            changes.append("district level ('" + "', '".join(d.title() for d in districts) + "') dropped: abolished 1 July 2025")
        if any(w.isdigit() for w in wards) or wards:
            pass
        city = vn_city or province
        city_key = province if (province and vn_city not in VN_MUNICIPAL) else (vn_city or province)
        if vn_city in VN_MUNICIPAL:
            city_key = vn_city
        region = province or vn_city
        street_segments = body
        if any(re.search(r"\b(?:DUONG|STREET)\b", s) for s in body) or any(re.match(r"^(SO|HEM|NGO|NGACH)\b", s) for s in body):
            if any(re.match(r"^(SO|HEM|NGO|NGACH)\b", s) for s in body):
                flags.append("alley_format")
    else:
        body = list(folded_segments)
        tail = body[-1] if body else ""
        if tail in COUNTRY_WORDS or tail.replace(".", "") in COUNTRY_WORDS:
            body = body[:-1]
        if body:
            city = re.sub(r"\b[A-Z]{2,3}\s+[A-Z0-9 ]{3,8}$", "", body[-1]).strip() or body[-1]
            body = body[:-1]
        city_key = city
        street_segments = body

    # the street-level parts
    unit, wing = "", ""
    for seg in street_segments:
        u, w = _unit(seg)
        unit = unit or u
        wing = wing or w
        if u and re.search(r"\b([A-Z])\s*[-/]\s*\d{2,4}\b|\b\d{2,4}\s*-\s*[A-Z]\b|#\s*\d", seg):
            flags.append("unit_format")
    numbers = set()
    street = []
    for seg in street_segments:
        toks = seg.replace("/", " / ").split()
        for t in toks:
            if t == "/":
                continue
            if cc == "US" and t in USPS:
                if t != USPS[t]:
                    flags.append("usps_normalised")
                t = USPS[t]
            if cc == "VN" and t in VN_ABBR:
                t = VN_ABBR[t]
                if not t:
                    continue
            if re.fullmatch(r"\d+[A-Z]?", t):
                numbers.add(t)
                continue
            if re.fullmatch(r"[A-Z]-\d+[A-Z]?|\d+[A-Z]?-[A-Z]", t):     # B-401, 801-A: unit forms already parsed
                numbers.add(re.sub(r"[^0-9]", "", t))
                continue
            if t in UNIT_WORDS or t in ("PIN",) or len(t) < 2 and not t.isdigit():
                continue
            if cc == "IN" and t in ("NO",):
                continue
            street.append(t)
    house = ""
    if street_segments:
        m = re.match(r"^\s*#?\s*(\d+[A-Z]?(?:/\d+[A-Z]?)*)", street_segments[0])
        if m:
            house = m.group(1)
        if not house:
            m = re.search(r"\b(?:FLAT|HOUSE|H|VILLA|APT|UNIT)\b\s*(?:NO)?\s*#?\s*(\d+[A-Z]?)", street_segments[0])
            if m:
                house = m.group(1)
    if cc == "VN":
        for seg in street_segments:
            for n in re.findall(r"\d+[A-Z]?", seg):
                numbers.add(n)
    if not house and unit:
        house = unit
    out = {"display": display, "empty": False, "country": cc, "city": city, "city_key": city_key, "region": region,
           "postal": postal, "postal5": postal5, "unit": unit, "wing": wing, "house": house, "numbers": sorted(numbers),
           "street": [t for t in street if t not in STREET_WORDS or len(street) <= 2],
           "flags": sorted(set(flags)), "changes": changes, "po_box": po_box, "military": military}
    return out


# ── two addresses ────────────────────────────────────────────────────────────

def _postal_state(a, b):
    """True equal, 'near' same first three digits, False different, None unknown."""
    pa, pb = a.get("postal5", ""), b.get("postal5", "")
    if not pa or not pb:
        return None
    if pa == pb:
        return True
    if pa[:3] == pb[:3] or sorted(pa) == sorted(pb):
        return "near"
    return False


def compare(a, b):
    """'same' home, 'same_locality', 'different', 'overseas' (other
    countries) or 'unknown' (an address missing)."""
    if a.get("empty") or b.get("empty"):
        return "unknown"
    if a["country"] and b["country"] and a["country"] != b["country"]:
        return "overseas"
    postal = _postal_state(a, b)
    city = bool(a["city_key"]) and a["city_key"] == b["city_key"]
    if postal is True and not city:
        city = True            # the ZIP / PIN anchors the place; the city token was a neighbourhood or district name
    if postal is False and not city:
        return "different"
    sa, sb = set(a["street"]), set(b["street"])
    overlap = len(sa & sb) / max(1, min(len(sa), len(sb))) if sa and sb else 0.0
    if a.get("military") and b.get("military") and not sa and not sb:
        # an APO/FPO address has no street: its unit or PSC and box numbers, all of them, are the home
        overlap = 1.0 if a["numbers"] and set(a["numbers"]) == set(b["numbers"]) else 0.0
    house = (a["house"] and a["house"] == b["house"]) or (a["numbers"] and set(a["numbers"]) == set(b["numbers"])) \
        or (a["unit"] and a["unit"] == b["unit"] and a["house"] and b["house"] and a["house"] == b["house"])
    if house and overlap >= 0.5 and (city or postal in (True, "near")):
        return "same"
    if a.get("po_box") or b.get("po_box"):
        return "same_locality" if (city or postal in (True, "near")) else "different"
    if (city or postal is True) and postal is not False:
        return "same_locality"
    return "different"


# ── the agent's work ─────────────────────────────────────────────────────────

def analyse(case):
    """Normalise the registered address, the proof of address, the secondary
    ID's address and the passport's, compare them and say what explains
    any disagreement."""
    country = case["country"]
    docs = case["documents"]
    reg = normalise(case["customer"]["address"], country)
    poa = normalise(docs["poa"]["address"], country)
    sec = normalise(docs["secondary"]["address"]["text"], country)
    pp = normalise(docs["passport"]["address"]["text"], country)
    agreement = {"registered_vs_poa": compare(reg, poa), "poa_vs_secondary": compare(poa, sec),
                 "registered_vs_secondary": compare(reg, sec), "registered_vs_passport": compare(reg, pp),
                 "poa_vs_passport": compare(poa, pp)}
    flags = sorted(set(reg["flags"]) | set(poa["flags"]) | set(sec["flags"]) | set(pp["flags"]))
    changes = []
    for label, part in (("registered", reg), ("proof of address", poa), ("secondary ID", sec), ("passport", pp)):
        changes.extend(f"{label}: {c}" for c in part["changes"])
    notes = []
    poa_kind = docs["poa"]["kind"]
    sec_kind = docs["secondary"]["kind"]
    resolved = agreement["registered_vs_poa"] in ("same", "same_locality")
    explained = []
    if reg["po_box"]:
        flags.append("po_box_registered")
        notes.append("The registered address is a PO Box; a residential address is needed for CIP.")
    if reg["military"]:
        explained.append("military")
        notes.append("Military APO/FPO address; the licence shows the home of record.")
    if agreement["registered_vs_poa"] == "overseas" or agreement["registered_vs_passport"] == "overseas" or agreement["registered_vs_secondary"] == "overseas":
        if reg["country"] and reg["country"] != country:
            flags.append("overseas_residence")
            explained.append("overseas")
            notes.append("Current residence overseas; the home-country document address is the permanent one.")
    if country == "US" and sec_kind == "driving_licence" and agreement["poa_vs_secondary"] in ("different", "overseas") and resolved:
        flags.append("dl_address_stale")
        explained.append("stale_dl")
        notes.append("The driver's licence address is out of date; a recent proof of address evidences the current residence.")
    if country == "VN" and agreement["poa_vs_secondary"] in ("different", "overseas") and resolved:
        flags.append("permanent_vs_temporary")
        explained.append("temporary_residence")
        if poa_kind == "residence_confirmation":
            notes.append("ID card shows the permanent (hometown) residence; Form CT07 confirms the temporary residence.")
        else:
            notes.append("ID card shows the permanent (hometown) residence; the proof of address shows where the customer lives.")
    if country == "IN" and pp["display"] and agreement["registered_vs_passport"] in ("different",) and resolved:
        flags.append("passport_address_outdated")
        explained.append("passport_outdated")
        notes.append("The passport address is the one at issue; Aadhaar / utility bill carry the current address.")
    if country == "IN" and case["customer"]["nri"]:
        explained.append("nri")
    if "pin_mismatch" not in flags and _postal_state(reg, poa) == "near":
        flags.append("pin_mismatch" if country == "IN" else "zip_mismatch")
        notes.append(("PIN" if country == "IN" else "ZIP") + f" code keyed with transposed or wrong digits ({reg['postal5']} vs {poa['postal5']}); the documents' code governs.")
    if "neighbourhood_alias" in flags:
        notes.append("Neighbourhood name used for the city; the USPS city for the ZIP governs.")
    if "legacy_city" in flags:
        notes.append("Legacy city name read as the current one.")
    if "district_alias" in flags:
        notes.append("District name read as the city it belongs to.")
    if "pre2025_unit" in flags:
        notes.append("Pre-2025 administrative units crosswalked to the post-merger ones.")
    if "alley_format" in flags:
        notes.append("Alley (hẻm/ngõ) numbering read in both orders.")
    docs_agree = agreement["poa_vs_secondary"] in ("same", "same_locality")
    conflict = not resolved and not explained and not reg["po_box"]
    if conflict and docs_agree and poa["display"] and sec["display"]:
        flags.append("crm_address_wrong")
        notes.append("The documents agree with each other but not with the registered address.")
    elif conflict:
        flags.append("address_conflict")
        notes.append("The addresses on file point at different residences and nothing on file explains it.")
    current = poa if poa["display"] else reg
    return {"registered": reg, "poa": poa, "secondary": sec, "passport": pp, "agreement": agreement,
            "resolved": resolved, "conflict": conflict, "explained": explained, "flags": sorted(set(flags)),
            "changes": changes, "notes": notes, "current": {"display": current["display"], "city": current["city_key"],
                                                              "region": current["region"], "country": current["country"]}}
