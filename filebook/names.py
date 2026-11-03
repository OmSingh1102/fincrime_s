"""Name rules: the AI Agent's name checks and the canonical name the proof of address prints.

analyse() reads the CRM name, the passport's visual zone (VIZ) and machine-
readable zone (MRZ) and the secondary ID, and returns the legal screening
name (always the passport spelling, the control the variance playbook
recommends), the other forms to screen as AKAs, and a list of what changed
and why. Each rule maps to one of the playbook's variance types; the
historical label in the file is never read.
"""
import re

from fincrime.filebook.similarity import fold, has_marks, squash, token_score, tokens

HONORIFICS = {"MR", "MRS", "MS", "MISS", "DR", "PROF", "ER", "ADV", "CA", "CAPT", "COL", "MAJ", "SHRI", "SRI", "SHREE",
              "SMT", "SHRIMATI", "KUMARI", "KUM", "SARDAR", "HAJI", "SYT", "ONG", "BA", "ANH", "CHI"}
ABBREVIATIONS = {"MOHD": "MOHAMMED", "MD": "MOHAMMED", "MOHAMMAD": "MOHAMMED", "MUHAMMAD": "MOHAMMED",
                 "MOHAMED": "MOHAMMED", "MOHMED": "MOHAMMED", "MUHAMMED": "MOHAMMED", "ABD": "ABDUL"}
FILLERS = {"FNU", "LNU", "NA", "N/A", "XXX", "UNKNOWN", "NONE", "BLANK", "NIL", "NULL", "-", "."}
SUFFIXES = {"JR", "SR", "II", "III", "IV"}
PREFIXES = {"VAN", "VON", "DE", "DEL", "DELA", "DA", "DI", "DU", "LA", "O", "MC", "MAC", "AL", "EL", "BIN", "BINTI",
            "BINTE", "ST", "SAN", "SANTA", "VANDER", "VANDEN", "TER", "TEN", "DOS"}

# Short and pet forms of given names; a CRM token on the left screens as any
# of the names on the right. Prefix forms (CHRIS → CHRISTINE) are matched by
# rule, so the table carries the irregular ones.
NICKNAMES = {
    "BILL": {"WILLIAM"}, "BILLY": {"WILLIAM"}, "WILL": {"WILLIAM"}, "WILLIE": {"WILLIAM"}, "LIAM": {"WILLIAM"},
    "BOB": {"ROBERT"}, "BOBBY": {"ROBERT"}, "ROB": {"ROBERT"}, "ROBBIE": {"ROBERT"}, "BERT": {"ROBERT", "ALBERT", "HERBERT"},
    "LIZ": {"ELIZABETH"}, "BETH": {"ELIZABETH"}, "BETTY": {"ELIZABETH"}, "LISA": {"ELIZABETH"}, "ELIZA": {"ELIZABETH"},
    "MEG": {"MARGARET"}, "PEGGY": {"MARGARET"}, "PEG": {"MARGARET"}, "MAGGIE": {"MARGARET"}, "MARGE": {"MARGARET"},
    "JACK": {"JOHN"}, "JOHNNY": {"JOHN"}, "JON": {"JONATHAN", "JOHN"}, "TED": {"EDWARD", "THEODORE"}, "TEDDY": {"EDWARD", "THEODORE"},
    "ED": {"EDWARD", "EDWIN", "EDMUND"}, "EDDIE": {"EDWARD"}, "NED": {"EDWARD"}, "DICK": {"RICHARD"}, "RICK": {"RICHARD"},
    "RICKY": {"RICHARD"}, "RICH": {"RICHARD"}, "HANK": {"HENRY"}, "HARRY": {"HENRY", "HAROLD"}, "JIM": {"JAMES"}, "JIMMY": {"JAMES"},
    "JAMIE": {"JAMES"}, "LARRY": {"LAWRENCE"}, "TONY": {"ANTHONY"}, "MIKE": {"MICHAEL"}, "MICK": {"MICHAEL"}, "DAVE": {"DAVID"},
    "STEVE": {"STEVEN", "STEPHEN"}, "ANDY": {"ANDREW"}, "DREW": {"ANDREW"}, "MATT": {"MATTHEW"}, "NICK": {"NICHOLAS"},
    "BEN": {"BENJAMIN"}, "BENNY": {"BENJAMIN"}, "JEN": {"JENNIFER"}, "JENNY": {"JENNIFER"}, "KATE": {"KATHERINE", "CATHERINE", "KATHLEEN"},
    "KATHY": {"KATHERINE", "CATHERINE", "KATHLEEN"}, "KATIE": {"KATHERINE", "CATHERINE"}, "CATHY": {"CATHERINE", "KATHERINE"},
    "SUE": {"SUSAN"}, "SUSIE": {"SUSAN"}, "CHUCK": {"CHARLES"}, "CHARLIE": {"CHARLES"}, "JERRY": {"GERALD", "JEROME"},
    "GERRY": {"GERALD"}, "DANNY": {"DANIEL"}, "DAN": {"DANIEL"}, "SAM": {"SAMUEL", "SAMANTHA"}, "SAMMY": {"SAMUEL"},
    "JOE": {"JOSEPH"}, "JOEY": {"JOSEPH"}, "TOM": {"THOMAS"}, "TOMMY": {"THOMAS"}, "RON": {"RONALD"}, "RONNIE": {"RONALD"},
    "DON": {"DONALD"}, "DONNIE": {"DONALD"}, "GREG": {"GREGORY"}, "FRED": {"FREDERICK", "ALFRED"}, "FREDDIE": {"FREDERICK"},
    "LOU": {"LOUIS", "LOUISE"}, "LOUIE": {"LOUIS"}, "KEN": {"KENNETH"}, "KENNY": {"KENNETH"}, "JEFF": {"JEFFREY", "GEOFFREY"},
    "GEOFF": {"GEOFFREY"}, "RAY": {"RAYMOND"}, "KIM": {"KIMBERLY"}, "CHRIS": {"CHRISTOPHER", "CHRISTINE", "CHRISTINA", "CHRISTIAN"},
    "PAT": {"PATRICK", "PATRICIA"}, "PATTY": {"PATRICIA"}, "TRISH": {"PATRICIA"}, "ALEX": {"ALEXANDER", "ALEXANDRA"},
    "SANDY": {"SANDRA", "ALEXANDRA"}, "SANDRA": {"ALEXANDRA"}, "TERRY": {"TERENCE", "TERESA"}, "TERI": {"TERESA"},
    "BECKY": {"REBECCA"}, "BECCA": {"REBECCA"}, "DEBBIE": {"DEBORAH"}, "DEB": {"DEBORAH"}, "VICKY": {"VICTORIA"}, "VICKI": {"VICTORIA"},
    "TORI": {"VICTORIA"}, "NANCY": {"ANN", "ANNE"}, "ANNIE": {"ANN", "ANNE"}, "MANDY": {"AMANDA"}, "MOLLY": {"MARY"}, "POLLY": {"MARY"},
    "JAKE": {"JACOB"}, "ABE": {"ABRAHAM"}, "AL": {"ALBERT", "ALAN", "ALLEN", "ALFRED"}, "ART": {"ARTHUR"}, "BART": {"BARTHOLOMEW"},
    "CAL": {"CALVIN"}, "CLIFF": {"CLIFFORD"}, "CURT": {"CURTIS"}, "DOUG": {"DOUGLAS"}, "GABE": {"GABRIEL"}, "GENE": {"EUGENE"},
    "GUS": {"AUGUSTUS", "GUSTAV"}, "HAL": {"HAROLD", "HENRY"}, "HOWIE": {"HOWARD"}, "JOSH": {"JOSHUA"}, "LEN": {"LEONARD"},
    "LES": {"LESLIE", "LESTER"}, "MAX": {"MAXWELL", "MAXIMILIAN"}, "NATE": {"NATHAN", "NATHANIEL"}, "PHIL": {"PHILIP", "PHILLIP"},
    "RUSS": {"RUSSELL"}, "STAN": {"STANLEY"}, "TIM": {"TIMOTHY"}, "TIMMY": {"TIMOTHY"}, "VINCE": {"VINCENT"}, "WALT": {"WALTER"},
    "ZACH": {"ZACHARY"}, "ZEKE": {"EZEKIEL"}, "SRINI": {"SRINIVAS", "SRINIVASAN"}, "VENKAT": {"VENKATA", "VENKATESH"},
    "JACKIE": {"JACQUELINE", "JACQUELYN", "JACKSON"}, "JACQUI": {"JACQUELINE"}, "ABBY": {"ABIGAIL"}, "ALLIE": {"ALISON", "ALEXANDRA"},
    "BRAD": {"BRADLEY"}, "CHARLOTTE": {"CHARLOTTE"}, "ELLIE": {"ELEANOR", "ELIZABETH"}, "FRANK": {"FRANCIS", "FRANKLIN"},
    "FRANKIE": {"FRANCES", "FRANCIS"}, "GINNY": {"VIRGINIA"}, "JOSIE": {"JOSEPHINE"}, "LIBBY": {"ELIZABETH"}, "LOTTIE": {"CHARLOTTE"},
    "MAGGY": {"MARGARET"}, "MANNY": {"MANUEL", "EMMANUEL"}, "MARTY": {"MARTIN"}, "MITCH": {"MITCHELL"}, "NELL": {"ELEANOR", "HELEN"},
    "RANDY": {"RANDALL", "RANDOLPH"}, "ROSIE": {"ROSEMARY", "ROSE"}, "SALLY": {"SARAH"}, "SHELLY": {"MICHELLE", "SHELLEY"},
    "TESS": {"TERESA", "THERESA"}, "TINA": {"CHRISTINA"}, "TRUDY": {"GERTRUDE"}, "WENDY": {"GWENDOLYN"},
    "RAJ": {"RAJESH", "RAJIV", "RAJEEV", "RAJENDRA"}, "SURI": {"SURESH"}, "SUBBU": {"SUBRAMANIAN", "SUBRAMANIAM"},
}

# Anglicised first names that naturalised citizens use for a Vietnamese given name.
ANGLICISED = {"KEVIN": {"KHOA"}, "LANA": {"LAN"}, "DAVID": {"DUY"}, "LARRY": {"LONG"}, "TONY": {"TUAN"}, "PHILIP": {"PHUC"},
              "DANNY": {"DANH"}, "HENRY": {"HUNG", "HIEU"}, "MICHAEL": {"MINH"}, "VINCENT": {"VINH"}, "ANNA": {"AN"},
              "JOHNNY": {"NHAN"}, "PETER": {"PHONG"}, "ANDY": {"AN"}, "TOMMY": {"TAM"}, "JIMMY": {"GIANG"}, "KENNY": {"KHANH"}}
for _k, _v in ANGLICISED.items():
    NICKNAMES.setdefault(_k, set()).update(_v)

TRANSLIT_CLASSES = [
    {"CHAUDHARY", "CHOUDHARY", "CHOWDHURY", "CHAUDHRY", "CHOUDHURY", "CHOWDHARY"},
    {"MOHAMMED", "MOHAMMAD", "MUHAMMAD", "MOHAMED", "MOHD", "MD", "MUHAMMED", "MOHMED"},
    {"QURESHI", "KURESHI", "QUREISHI", "KURESHY", "QURAISHI"}, {"SHAIKH", "SHEIKH", "SHEKH", "SHAIK", "SHAIKH"},
    {"SYED", "SAYED", "SAIYED", "SAYYED", "SAYYID"}, {"HUSSAIN", "HUSAIN", "HUSSEIN", "HUSEN", "HOSSAIN"},
    {"AHMED", "AHMAD", "AHAMED", "AHMET", "AHMAD"}, {"AGARWAL", "AGRAWAL", "AGARWALA", "AGGARWAL"},
    {"BHAT", "BHATT"}, {"UPADHYAY", "UPADHYAYA", "UPADHYA"}, {"KRISHNAN", "KRISHNA", "KRISHNAA"},
    {"SRINIVAS", "SRINIVASAN", "SRINIVASA"}, {"IYER", "AIYAR", "AYYAR", "AIYER"}, {"PILLAI", "PILLAY"}, {"NAIR", "NAYAR"},
    {"PATHAN", "PATAN"}, {"KHAN", "KAHN"}, {"SHARMA", "SARMA"}, {"VERMA", "VARMA"}, {"MENON", "MENNON"},
    {"SUBRAMANIAN", "SUBRAMANIAM", "SUBRAMANYAM"}, {"BANERJEE", "BANERJI", "BANDYOPADHYAY"}, {"CHATTERJEE", "CHATTERJI"},
    {"MUKHERJEE", "MUKHERJI", "MUKHOPADHYAY"}, {"GHOSH", "GHOSE"}, {"SEN", "SENGUPTA"}, {"NGUYEN", "NGUYEN"},
]
_CLASS_OF = {}
for _i, _cls in enumerate(TRANSLIT_CLASSES):
    for _t in _cls:
        _CLASS_OF[_t] = _i

VN_SURNAMES = {"NGUYEN", "TRAN", "LE", "PHAM", "HOANG", "HUYNH", "PHAN", "VU", "VO", "DANG", "BUI", "DO", "HO", "NGO",
               "DUONG", "LY", "TRUONG", "DINH", "DOAN", "LAM", "MAI", "TRINH", "LUU", "CAO", "TA", "HA", "DAO", "TON", "QUACH",
               "LUONG", "THAI", "CHAU", "TANG", "TO", "DANG", "BACH", "KIEU", "GIANG", "VUONG"}
COMMON_SURNAMES = {"NGUYEN", "TRAN", "LE", "PHAM", "HOANG", "HUYNH", "PHAN", "VU", "VO", "DANG", "BUI", "DO", "HO", "NGO",
                   "DUONG", "LY", "SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER", "DAVIS", "RODRIGUEZ",
                   "MARTINEZ", "HERNANDEZ", "LOPEZ", "GONZALEZ", "WILSON", "ANDERSON", "THOMAS", "TAYLOR", "MOORE", "JACKSON",
                   "MARTIN", "LEE", "PEREZ", "THOMPSON", "WHITE", "HARRIS", "SANCHEZ", "CLARK", "KUMAR", "SINGH", "SHARMA",
                   "PATEL", "REDDY", "RAO", "GUPTA", "KHAN", "SHAH", "MEHTA", "JAIN", "DAS", "NAIR", "IYER", "YADAV", "VERMA",
                   "MISHRA", "CHAUDHARY", "AGARWAL", "PRASAD", "KRISHNAN", "RAJENDRAN", "RAMASAMY"}

VARIANCE = {   # the playbook's labels, by rule and country
    "encoding": "Diacritics stripped / encoding corruption",
    "dstroke": "Đ (D-stroke) transliteration",
    "honorific": "Honorific or abbreviation in name",
    "filler": "Mononym – blank surname on passport",
    "suffix": "Generational suffix (Jr./Sr./III)",
    "patronymic": "Patronymic initial vs expanded surname",
    "initials": {"IN": "Long name – initials / MRZ truncation", "US": "Middle name vs initial vs omitted",
                 "VN": "Middle name dropped / compressed"},
    "compound": "Compound / hyphenated surname",
    "prefix": "Apostrophe / prefix surname tokenisation",
    "nickname": "Nickname / preferred name in CRM",
    "order": {"IN": "Given name / surname fields swapped", "VN": "Family-name-first order reversed",
              "US": "Naturalised citizen – reordered / anglicised name"},
    "middle": {"IN": "Father's name as middle name", "US": "Middle name vs initial vs omitted",
               "VN": "Middle name dropped / compressed"},
    "anglicised": {"US": "Naturalised citizen – reordered / anglicised name", "IN": "Nickname / preferred name in CRM",
                   "VN": "Nickname / preferred name in CRM"},
    "surname": {"IN": "Married vs maiden surname", "US": "Married vs maiden surname", "VN": "Western married surname on form"},
    "translit": "Transliteration / spelling variant",
    "common": "High-frequency family name collision",
    "truncation": "Long name – initials / MRZ truncation",
    "collapse": "Diacritic collapse – distinct names merge",
}


def label(rule, country):
    v = VARIANCE.get(rule, rule)
    return v.get(country, next(iter(v.values()))) if isinstance(v, dict) else v


# ── helpers ──────────────────────────────────────────────────────────────────

_MOJIBAKE = re.compile(r"[ÃÂÅÆ][\u0080-ÿ]|á»|áº")


def repair_mojibake(text: str):
    """UTF-8 read as Windows-1252 ('VÅ© CÃ´ng KhÃ¡nh') back to 'Vũ Công Khánh'.
    Returns (text, None) when nothing looks broken, (repaired, True) when the
    round trip works, (text, False) when bytes were lost on the way."""
    if not text or not _MOJIBAKE.search(text):
        return text, None
    try:
        fixed = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text, False
    return (fixed, True) if not _MOJIBAKE.search(fixed) else (text, False)


def mojibake_lost(text: str) -> str:
    """A broken name whose round trip fails because the text itself lost
    bytes on the way (the third byte of 'ọ' has no Windows-1252 character):
    the name decoded with each lost character as U+FFFD, else ''.
    'Phan Ngá»c Äáº¡t' → 'Phan Ng\ufffdc \ufffdạt'."""
    if not text or not _MOJIBAKE.search(text):
        return ""
    try:
        fixed = text.encode("cp1252").decode("utf-8", errors="replace")
    except UnicodeEncodeError:
        return ""
    return re.sub("\ufffd+", "\ufffd", fixed) if "\ufffd" in fixed else ""


# ── the holder's sex, from the given names ───────────────────────────────────
# The file has no gender column; the drawn passport's sex marker and the
# record's gender come from the names it does carry. Folded spellings; a
# name both sexes use (Kiran, Snehal, Anh, Thanh) is in neither list.
FEMALE_GIVEN = frozenset("""
    AARTI ALEXANDRA AMANDA ANA ANITA ANITHA ANKITA ANNAMMA ASHLEY ASHWINI ASWATHY BARBARA BHAVYA BINDU CARMEN CAROL
    CHRISTINE DANIELA DEBORAH DEEPA DEEPIKA DHARA DIVYA DONNA ELENA ELIZABETH EMILY FARHEEN FATIMA FORAM GABRIELA
    GAYATHRI ISABEL ITISHREE JACQUELINE JENNIFER JESSICA JYOTI KAREN KATHERINE KAVITA KAVITHA KAVYA KEERTHANA KIMBERLY
    KOMAL KRUPA LAKSHMI LAURA LEKSHMI LINDA LIPIKA LISA LUCIA MADHURI MANASI MANSI MARGARET MARIA MARY MEENA MEERA MEGHA
    MELISSA MICHELLE MONIKA MOUMITA NANCY NANDHINI NASREEN NAZIA NEETHU NEHA NIDHI PALLAVI PAROMITA PATRICIA PAYEL POOJA
    PRAJAKTA PRIYA RASHMI REBECCA RESHMA REVATHI RITU RITUPARNA ROSA RUKSANA RUPALI SAKSHI SAMANTHA SANA SANGEETHA
    SARAH SARANYA SASMITA SEEMA SHABANA SHALINI SHARMILA SHRUTI SHWETA SOFIA SONALI SOWMYA STEPHANIE SUDESHNA SUSAN
    SWATI TANUSHREE USHA VERONICA VICTORIA ZAINAB DURGA
    BICH CHAU CHI HA HAN HANG HANH HOA HONG HUONG KIM LAN LINH LOAN MAI MY NGA NGAN NGOC NHUNG OANH PHUONG QUYEN THAO
    THU THUY TRANG TRINH TUYET UYEN VY YEN""".split())
MALE_GIVEN = frozenset("""
    AARON ABDUL ALEJANDRO ALEXANDER ALOK ANAND ANDREW ANIKET ANIL ANIRBAN ANKIT ANOOP ANTHONY ARINDAM ARJUN ARNAB ARUN
    ASHISH BALAJI BENJAMIN BHAVESH BILL BISWAJIT BRIAN CARLOS CHARLES CHRISTOPHER DANIEL DAVID DINESH EDUARDO EDWARD
    ERIC FAISAL FERNANDO GARY GAURAV GOKUL GREGORY HARISH HEMANT HITESH IMTIAZ IRFAN JACOB JAMES JASON JAVIER JAYESH
    JEFFREY JOHN JONATHAN JORGE JOSHUA KALPESH KANNAN KARTHIK KENNETH KEVIN LUIS MAHESH MANAS MANIKANDAN MANJUNATH
    MARCUS MARK MATTHEW MICHAEL MOHAMMED MUKESH NICHOLAS NIKHIL NILESH PARESH PARTHA PATRICK PRABHU PRADEEP
    PRASAD PRASANTA PRASHANT RAGHAVENDRA RAHUL RAJ RAJESH RAMANA RAMESH RAYMOND RICARDO RICHARD ROBERT ROBERTO ROHIT RONALD RYAN SACHIN
    SAMUEL SANJAY SANJEEB SANTOSH SARAVANAN SATHISH SAURABH SAYAN SCOTT SENTHIL SIVA SREEJITH STEPHEN STEVEN SUBHAJIT
    SRINIVAS SUBRAT SUMIT SUNIL SWAPNIL TANMOY THOMAS TIMOTHY TUSHAR TYLER VARUN VIGNESH VIJAY VIKRAM VINEETH VINOTH WASIM
    WILLIAM YOGESH
    BAO BINH CONG CUONG DAT DINH DUC DUY GIA HAI HIEU HOANG HUNG HUU HUY KHANG KHOA KIET LONG MINH NAM NGHIA PHONG PHUC QUAN
    QUANG QUOC SON TAI THANG TIEN TOAN TRUNG TUAN VIET VINH""".split())
# Vietnamese: the middle name THỊ or VĂN says it outright; a few personal
# names differ only by their marks once folded (Dũng, a man; Dung, a woman).
VN_MIDDLE_SEX = {"THI": "F", "VAN": "M"}
# Telugu names open with a deity's name either sex carries (VENKATA LAKSHMI
# DURGA, NAGA SAI SRI LAKSHMI): the holder's own name follows them.
GIVEN_PREFIXES = {"VENKATA", "NAGA", "SAI", "SRI", "SREE", "SATYA"}
VN_MARKED_SEX = {"DŨNG": "M", "DUNG": "F"}


def sex_from_given(given: str, country: str = "") -> str:
    """'F', 'M' or '' from given names as printed ('THỊ THU TRANG',
    'JENNIFER KAY'): a Vietnamese name by its middle names, then its personal
    (last) name; any other by its first given name after a Telugu prefix."""
    printed = (given or "").upper().split()
    folded = [fold(t) for t in printed]
    if not folded:
        return ""
    if country == "VN":
        for t in folded[:-1]:
            if t in VN_MIDDLE_SEX:
                return VN_MIDDLE_SEX[t]
        if printed[-1] in VN_MARKED_SEX:
            return VN_MARKED_SEX[printed[-1]]
        for t in [folded[-1]] + folded[:-1]:
            if t in FEMALE_GIVEN or t in MALE_GIVEN:
                return "F" if t in FEMALE_GIVEN else "M"
        return ""
    t = next((t for t in folded if t not in GIVEN_PREFIXES), "")
    return "F" if t in FEMALE_GIVEN else "M" if t in MALE_GIVEN else ""


def strip_parenthetical(text: str):
    """'Aswathy Panicker (George)' → ('Aswathy Panicker', ['George'])."""
    extras = re.findall(r"\(([^()]*)\)", text or "")
    return re.sub(r"\s*\([^()]*\)", "", text or "").strip(), [e.strip() for e in extras if e.strip()]


def is_initial(tok: str) -> bool:
    return len(squash(tok)) == 1 and squash(tok).isalpha()


def same_class(a: str, b: str) -> bool:
    return a in _CLASS_OF and b in _CLASS_OF and _CLASS_OF[a] == _CLASS_OF[b]


def d_stroke_forms(tok: str) -> set:
    """'DJINH' and 'DDINH' (Telex and legacy typings of Đinh) → 'DINH'."""
    out = {tok}
    if tok.startswith(("DJ", "DD")) and len(tok) > 2:
        out.add("D" + tok[2:])
    return out


def given_match(a: str, b: str) -> float:
    """Two folded given-name tokens: 1 exact, 0.95 nickname, 0.9 short form
    or spelling variant, 0.6 an initial, 0 apart."""
    if not a or not b:
        return 0.0
    if a == b or squash(a) == squash(b) or d_stroke_forms(a) & d_stroke_forms(b):
        return 1.0
    if b in NICKNAMES.get(a, ()) or a in NICKNAMES.get(b, ()):
        return 0.95
    if same_class(a, b):
        return 0.95
    if (len(a) >= 3 and b.startswith(a)) or (len(b) >= 3 and a.startswith(b)):
        return 0.9
    if is_initial(a) and b.startswith(squash(a)) or is_initial(b) and a.startswith(squash(b)):
        return 0.6
    return token_score(a, b)


def surname_forms(toks) -> set:
    """Every folded form a surname goes by: joined, spaced, each token,
    hyphen and apostrophe parts, D-stroke typings."""
    toks = [t for t in toks if t]
    forms = set()
    if not toks:
        return forms
    forms.add(squash("".join(toks)))
    forms.add(" ".join(toks))
    for t in toks:
        forms.add(t)
        forms.add(squash(t))
        forms |= d_stroke_forms(squash(t))
        for part in re.split(r"[-']", t):
            if part:
                forms.add(part)
    return {f for f in forms if f}


def surname_score(client_tokens, wc_tokens) -> float:
    """1 same surname (any typing), 0.8 one component of a compound,
    0.9 a spelling variant, 0.6 an initial, else apart."""
    c_forms, w_forms = surname_forms(client_tokens), surname_forms(wc_tokens)
    if not c_forms or not w_forms:
        return 0.0
    c_full, w_full = squash("".join(client_tokens)), squash("".join(wc_tokens))
    if c_full == w_full or d_stroke_forms(c_full) & d_stroke_forms(w_full):
        return 1.0
    if c_forms & w_forms:
        return 0.8
    best = 0.0
    for a in client_tokens:
        for b in wc_tokens:
            if same_class(a, b):
                best = max(best, 0.95)
            elif is_initial(a) or is_initial(b):
                if squash(a)[:1] == squash(b)[:1]:
                    best = max(best, 0.6)
            else:
                best = max(best, token_score(a, b))
    return best


def _suffix(toks):
    """Trailing JR/SR/II/III/IV off a token list (only after a real name)."""
    toks = list(toks)
    if len(toks) >= 2 and squash(toks[-1]) in SUFFIXES:
        return toks[:-1], squash(toks[-1])
    return toks, ""


def _split_sides(toks, viz_sur, viz_given, country):
    """Which CRM / secondary-ID tokens are the surname: the run of tokens
    that matches the passport surname (joined or apart: VAN BUREN, DE LUCA,
    MENDOZA MORALES), else whatever is left once the passport given names
    are accounted for, else the conventional end."""
    toks = list(toks)
    if not toks:
        return [], []
    forms = surname_forms(viz_sur)
    if forms:
        for n in (3, 2, 1):
            for i in range(len(toks) - n + 1):
                run = toks[i:i + n]
                joined = squash("".join(run))
                if joined in forms or d_stroke_forms(joined) & forms or (n == 1 and (run[0] in forms or squash(run[0]) in forms)):
                    return run, toks[:i] + toks[i + n:]
    if viz_given:
        given = [t for t in toks if any(given_match(t, v) >= 0.9 for v in viz_given)]
        rest = [t for t in toks if t not in given]
        if given and rest and len(rest) <= 2:
            return rest, given
    if country == "VN":
        return toks[:1], toks[1:]
    return toks[-1:], toks[:-1]


# ── the agent's work ─────────────────────────────────────────────────────────

def analyse(country, crm_name, viz, mrz, secondary_name, corporate=False, ubo=""):
    """The legal screening name and what the CRM record had wrong. `viz`
    and `mrz` are the parsed passport fields; the agent never sees the
    historical labels."""
    changes, flags, detected, aka = [], [], [], []

    def change(rule, frm, to, note=""):
        changes.append({"rule": rule, "from": frm, "to": to, "note": note})

    def detect(rule):
        lbl = label(rule, country)
        if lbl not in detected:
            detected.append(lbl)

    person = ubo if corporate and ubo else crm_name
    if corporate:
        flags.append("corporate_ubo")
    crm_display, repaired = repair_mojibake(person)
    if repaired is True:
        change("Encoding repair", person, crm_display, "UTF-8 text had been decoded as Windows-1252; repaired by round trip")
        detect("encoding")
    elif repaired is False:
        flags.append("encoding_unrepairable")
        change("Encoding repair", person, viz["given"] + " " + viz["surname"],
               "bytes were lost in the CRM copy; the name is taken from the passport")
        detect("encoding")
    crm_plain, extras = strip_parenthetical(crm_display)
    crm_tokens = tokens(crm_plain) if repaired is not False else []

    viz_sur = tokens(viz.get("surname", ""))
    viz_given, viz_suffix = _suffix(tokens(viz.get("given", "")))
    mononym = not viz_sur
    sec_plain, sec_extras = strip_parenthetical(secondary_name or "")
    sec_tokens, sec_suffix = _suffix(tokens(sec_plain))

    if has_marks(viz.get("surname", "") + viz.get("given", "")) and crm_tokens and not has_marks(crm_display) and repaired is None:
        flags.append("diacritics_stripped")
        detect("encoding")
    if any(ch in (crm_display or "") for ch in "ÐĐđ") or any(t.startswith(("DJ", "DD")) for t in crm_tokens) \
            or ("Đ" in (viz.get("surname", "") + viz.get("given", "")).upper() and crm_tokens):
        if any(t.startswith(("DJ", "DD")) for t in crm_tokens) or "Ð" in (crm_display or "") or "Đ" in (viz.get("surname", "") + viz.get("given", "")).upper():
            detect("dstroke")
            if any(t.startswith(("DJ", "DD")) for t in crm_tokens) or "Ð" in (crm_display or ""):
                flags.append("d_stroke_typing")

    # honorifics, abbreviations, fillers
    while len(crm_tokens) > 1 and squash(crm_tokens[0]) in HONORIFICS:
        change("Honorific removed", crm_tokens[0], "", "titles are not part of the legal name")
        detect("honorific")
        crm_tokens = crm_tokens[1:]
    for i, t in enumerate(crm_tokens):
        if squash(t) in ABBREVIATIONS and len(crm_tokens) > 1:
            change("Abbreviation expanded", t, ABBREVIATIONS[squash(t)], "dictionary expansion before screening")
            detect("honorific")
            crm_tokens[i] = ABBREVIATIONS[squash(t)]
    kept = []
    for t in crm_tokens:
        if squash(t) in FILLERS or not squash(t):
            change("Filler token removed", t, "", "placeholder surname (FNU/LNU) is not a name")
            detect("filler")
        elif kept and squash(t) == squash(kept[-1]):
            change("Duplicate token removed", t, "", "the given name had been repeated as a surname")
            detect("filler")
        else:
            kept.append(t)
    crm_tokens = kept
    crm_tokens, crm_suffix = _suffix(crm_tokens)
    if mononym and (len(crm_tokens) > 1 or any(c["rule"] in ("Filler token removed", "Duplicate token removed") for c in changes)
                    or any(is_initial(t) for t in sec_tokens) or "." in (crm_plain or "")):
        detect("filler")
        if any(is_initial(t) for t in crm_tokens):
            change("Filler token removed", next(t for t in crm_tokens if is_initial(t)), "",
                   "an initial had been added as a surname; the passport has none")
            crm_tokens = [t for t in crm_tokens if not is_initial(t)]

    # the canonical screening name: the passport
    canonical_tokens = (viz_sur + viz_given) if country == "VN" else (viz_given + viz_sur)
    canonical = " ".join(canonical_tokens)
    given_display = re.sub(r"\s+(JR|SR|II|III|IV)\.?$", "", (viz.get("given", "") or "").strip(), flags=re.I)
    canonical_display = (f"{viz.get('surname', '')} {given_display}" if country == "VN"
                         else f"{given_display} {viz.get('surname', '')}").strip()
    if viz_suffix:
        canonical_display += f" {viz_suffix}"
    suffix = viz_suffix or crm_suffix or sec_suffix
    if viz_suffix and not crm_suffix:
        change("Suffix restored", "", viz_suffix, "generational suffix on the passport, dropped by the CRM")
        detect("suffix")
    elif crm_suffix and not viz_suffix:
        detect("suffix")

    crm_sur, crm_given = _split_sides(crm_tokens, viz_sur, viz_given, country)
    sec_sur, sec_given = _split_sides(sec_tokens, viz_sur, viz_given, country)

    # initials and short forms against the passport given names
    mapped = {}            # crm given token → viz given token it stands for
    covered = set()        # viz given tokens accounted for by a glued or compressed CRM token
    unmatched = []
    pool = list(viz_given)
    for t in crm_given:
        hit = None
        # a run of initials glued together (VN, NSS, VL) stands for consecutive given names
        if 2 <= len(squash(t)) <= 3 and not any(t == v for v in viz_given) and len(viz_given) >= len(squash(t)):
            letters = squash(t)
            for i in range(len(viz_given) - len(letters) + 1):
                if all(viz_given[i + k].startswith(letters[k]) for k in range(len(letters))):
                    run = viz_given[i:i + len(letters)]
                    change("Initials expanded", t, " ".join(run), "from the passport given names")
                    detect("truncation" if country != "VN" else "initials")
                    hit = run[0]
                    covered.update(run)
                    break
        if hit:
            mapped[t] = hit
            continue
        if is_initial(t):
            for v in pool:
                if v.startswith(squash(t)) and v not in mapped.values():
                    hit = v
                    break
            if hit:
                change("Initial expanded", t, hit, "from the passport given names")
                detect("initials")
        else:
            best, score = None, 0.0
            for v in viz_given:
                s = given_match(t, v)
                if s > score:
                    best, score = v, s
            if score >= 0.9:
                hit = best
                if score < 1.0 and t != best:
                    if best in NICKNAMES.get(t, ()) or (len(t) >= 3 and best.startswith(t) and t not in viz_given):
                        change("Nickname replaced", t, best, "preferred name in the CRM; the passport name is screened")
                        detect("nickname")
                    elif same_class(t, best) or token_score(t, best) >= 0.9:
                        change("Spelling variant unified", t, best, "same name romanised differently; passport spelling adopted")
                        detect("translit")
            elif score >= 0.6:
                hit = best
        if hit:
            mapped[t] = hit
        else:
            unmatched.append(t)

    # concatenated given names (MINHKHANG = MINH + KHANG)
    for t in list(unmatched):
        for i in range(len(viz_given) - 1):
            if squash(t) == squash(viz_given[i] + viz_given[i + 1]):
                change("Compressed name split", t, f"{viz_given[i]} {viz_given[i + 1]}", "two given names had been merged")
                detect("initials")
                mapped[t] = viz_given[i]
                covered.update(viz_given[i:i + 2])
                unmatched.remove(t)
                break

    # initials for the surname (patronymic initial: BALAJI K → KRISHNAN)
    for t in list(crm_sur):
        if is_initial(t) and viz_sur and viz_sur[0].startswith(squash(t)):
            change("Patronymic initial expanded", t, viz_sur[0], "the father's initial stands for the passport surname")
            detect("patronymic")
            crm_sur = [viz_sur[0] if x == t else x for x in crm_sur]
    for t in list(crm_given):
        if is_initial(t) and viz_sur and viz_sur[0].startswith(squash(t)) and t in unmatched:
            change("Patronymic initial expanded", t, viz_sur[0], "the father's initial stands for the passport surname")
            detect("patronymic")
            unmatched.remove(t)

    # order: did the CRM hold the tokens in another order?
    crm_set = {squash(t) for t in crm_tokens}
    if crm_tokens and crm_set == {squash(t) for t in canonical_tokens} and [squash(t) for t in crm_tokens] != [squash(t) for t in canonical_tokens]:
        change("Name order aligned", " ".join(crm_tokens), canonical, "surname and given names keyed in the wrong fields; rebuilt from the MRZ primary identifier")
        detect("order")
    elif crm_sur and crm_tokens and squash(crm_tokens[0]) in {squash(x) for x in crm_sur} and country != "VN" and len(crm_tokens) > 1:
        change("Name order aligned", " ".join(crm_tokens), canonical, "surname keyed first; rebuilt from the MRZ primary identifier")
        detect("order")
    elif crm_sur and crm_tokens and squash(crm_tokens[-1]) in {squash(x) for x in crm_sur} and country == "VN" and len(crm_tokens) > 1:
        change("Name order aligned", " ".join(crm_tokens), canonical, "Western order on the form; family name restored first as the MRZ reads it")
        detect("order")

    # given names the CRM lacks or adds
    missing = [v for v in viz_given if v not in mapped.values() and v not in covered]
    if missing and crm_given and not unmatched:
        if len(viz_given) >= 3 and len(missing) >= 2 and country != "VN":
            change("Given names restored", " ".join(crm_given), " ".join(viz_given), "a long name had been shortened; the full passport name is screened")
            detect("truncation")
        elif len(viz_given) >= 2 and country == "IN" and (viz_given[0] in missing or viz_given[-1] in missing):
            change("Given name restored", " ".join(crm_given), " ".join(viz_given),
                   "the CRM kept the father's name as the given name" if viz_given[0] in missing else "the father's name (middle) was dropped")
            detect("middle")
        else:
            change("Middle name restored", " ".join(crm_given), " ".join(viz_given), "the passport carries the full given names")
            detect("middle")
    if unmatched and crm_given and not mapped:
        flags.append("given_name_differs")
        change("Given name differs", " ".join(crm_given), " ".join(viz_given), "the CRM holds an anglicised or preferred name; kept as an AKA")
        detect("anglicised")
    elif unmatched and crm_given:
        flags.append("extra_given_token")
        if country == "US":
            change("Given name differs", " ".join(unmatched), " ".join(viz_given), "a name the passport does not carry (anglicised or preferred); kept as an AKA")
            detect("anglicised")
    if country == "US" and any(c["rule"] == "Nickname replaced" and c["to"] in ANGLICISED.get(c["from"], ()) for c in changes):
        detect("anglicised")
    if country == "US" and viz_sur and squash("".join(viz_sur)) in VN_SURNAMES and crm_tokens and (missing or unmatched or
                                                                                                   any(c["rule"] == "Name order aligned" for c in changes)):
        detect("anglicised")
    if country == "IN" and len(viz_given) >= 2 and viz_given[-1].endswith(("BHAI", "LAL", "BEN")) and (missing or any(c["rule"] == "Name order aligned" for c in changes)):
        detect("middle")

    # surnames across documents
    screen_surnames = [viz_sur] if viz_sur else []
    other_surnames = []
    for src, toks in (("CRM", crm_sur), ("Secondary ID", sec_sur)):
        if toks and viz_sur and surname_score(viz_sur, toks) < 0.8 and not any(is_initial(t) for t in toks):
            if not any(squash("".join(toks)) == squash("".join(o)) for _, o in other_surnames):
                other_surnames.append((src, toks))
        elif toks and viz_sur and 0.9 <= surname_score(viz_sur, toks) < 1.0:
            change("Spelling variant unified", " ".join(toks), " ".join(viz_sur), f"{src} spells the surname differently; passport spelling adopted")
            detect("translit")
            if surname_forms(viz_sur) & surname_forms(toks) and len(viz_sur) + len(toks) > 2:
                detect("compound")
        elif toks and viz_sur and surname_score(viz_sur, toks) == 1.0 and (
                squash("".join(toks)) != squash("".join(viz_sur)) or len(toks) != len(viz_sur) or "-" in "".join(viz_sur) + "".join(toks)):
            # same surname typed differently: VAN BUREN / VANBUREN, DE LUCA / DELUCA, GOMEZ-AGUILAR / GOMEZ AGUILAR
            multi = toks if len(toks) > 1 else viz_sur
            joined = squash("".join(viz_sur))
            if any(t in PREFIXES for t in multi[:-1]) or any(joined.startswith(p) and len(joined) > len(p) + 2 for p in ("MC", "MAC", "DE", "DI", "DEL", "LE", "LA", "VAN", "VON", "ST", "O")) and len(toks) != len(viz_sur):
                detect("prefix")
                change("Surname prefix joined", " ".join(multi), joined, "prefix and surname compared joined and apart")
            elif "-" in "".join(viz_sur) + "".join(toks) or len(viz_sur) > 1 or len(toks) > 1:
                detect("compound")
            elif "'" in "".join(viz_sur) or "'" in "".join(toks):
                detect("prefix")
    for src, toks in (("CRM", crm_sur), ("Secondary ID", sec_sur)):
        if viz_sur and toks and surname_score(viz_sur, toks) == 0.8:
            if any(t in PREFIXES for t in viz_sur[:-1]):
                detect("prefix")
                change("Surname prefix restored", " ".join(toks), " ".join(viz_sur), f"the {src} dropped the surname prefix; both forms are screened")
            else:
                detect("compound")
                change("Compound surname restored", " ".join(toks), " ".join(viz_sur), f"the {src} carries one component; both components are screened")
    for src, toks in other_surnames:
        screen_surnames.append(toks)
        if viz_sur and sec_sur and src == "CRM" and surname_score(viz_sur, sec_sur) >= 0.9:
            note = "the CRM surname is on no document (a married name on the form?); both are screened"
        else:
            note = f"the {src} carries another surname (married or maiden name); both are screened"
        change("Second surname screened", " ".join(toks), " ".join(viz_sur), note)
        detect("surname")
        flags.append("surname_conflict")
    for e in extras + sec_extras:
        et = tokens(e)
        if et and et not in screen_surnames:
            screen_surnames.append(et)
            aka.append({"name": e, "source": "CRM (former name)", "why": "former surname noted in parentheses"})
    # prefix tokens in the passport surname (VAN BUREN) are screened joined and apart
    if viz_sur and any(t in PREFIXES for t in viz_sur[:-1]):
        detect("prefix")
    if viz_sur and any("'" in t for t in viz_sur):
        detect("prefix")
    if viz_sur and any("-" in t or len(viz_sur) > 1 for t in viz_sur) and not any(t in PREFIXES for t in viz_sur[:-1]):
        if len(viz_sur) > 1 or "-" in "".join(viz_sur):
            detect("compound")

    if viz_sur and squash("".join(viz_sur)) in COMMON_SURNAMES and country == "VN":
        flags.append("common_surname")
        detect("common")
    if mrz.get("truncated"):
        flags.append("mrz_truncated")
        detect("truncation")
    if crm_given and len(viz_given) >= 3 and sum(1 for t in crm_given if is_initial(t)) >= 2:
        detect("truncation")

    # AKAs: every other form the person has gone by
    crm_form = " ".join(crm_tokens)
    if crm_form and crm_form != canonical and repaired is not False:
        aka.append({"name": crm_plain, "source": "CRM", "why": "; ".join(c["rule"].lower() for c in changes[:3]) or "as keyed at onboarding"})
    sec_form = " ".join(sec_tokens)
    if sec_form and sec_form != canonical and sec_form != crm_form:
        aka.append({"name": sec_plain, "source": "Secondary ID", "why": "as printed on the secondary ID"})
    if corporate:
        aka.append({"name": crm_name, "source": "CRM (entity)", "why": "the customer is a company; the UBO is screened"})

    screen_given = sorted({t for t in viz_given} | {t for t in crm_given if not is_initial(t)} | {t for t in sec_given if not is_initial(t)})
    return {
        "canonical": canonical_display.upper(), "canonical_tokens": canonical_tokens, "surname": " ".join(viz_sur),
        "given": viz_given, "suffix": suffix, "mononym": mononym, "key": f"{' '.join(viz_sur)}, {' '.join(viz_given)}".strip(", "),
        "native": (f"{viz.get('surname', '')} {viz.get('given', '')}").strip(), "aka": aka,
        "screen_surnames": screen_surnames, "screen_given": screen_given,
        "sources": {"crm": crm_name, "crm_repaired": crm_display if repaired else "", "viz": viz.get("raw", ""),
                    "mrz": mrz.get("line1", ""), "secondary": secondary_name},
        "changes": changes, "flags": flags, "variance_detected": detected,
    }
