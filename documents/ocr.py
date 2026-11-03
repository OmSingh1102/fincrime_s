"""OCR on uploaded document images: Tesseract, run on this machine (no
image leaves it for this step), as a cross-check beside Claude's read.

text(image) is the image's printed text. mrz(image) finds the passport's
machine-readable zone (ICAO 9303 TD3: two lines of 44 characters) and
parses it: document number, nationality, date of birth, sex, expiry,
surname and given names, with every check digit and the composite one
tested. The lower part of the page is straightened first (a photo is
rarely level, and a tilt of half a degree is enough to spoil OCR of a
44-character line), then read a few ways, and the lines pooled: each
check-digit field is taken from a reading its own check digit confirms,
everything else by vote. Check digits that add up mean OCR read the zone
right; the agent then holds Claude's read to it (ai_agent checks,
"Passport MRZ read by OCR"). Letters and digits OCR confuses (O and 0,
I and 1 ...) are put right only where the field can hold one of them, never
guessed until a check digit happens to agree.

id_read(image) is OCR as Scan ID's reader (the upload page's "Read with
OCR", beside the AI): the fields come from the passport's zone, since OCR
alone does not know which printed line is the name or which date is the
birth date; the page's text is kept beside them.

Tesseract is found from TESSERACT_CMD, the PATH, or its usual Windows
folders. Without it available() is False and nothing here runs: the
agent's checks say OCR is not available and block nothing."""
import io
import os
import shutil
import time

from PIL import Image, ImageFilter, ImageOps

from fincrime.utils import dates

try:
    import pytesseract
except ImportError:                     # pragma: no cover - requirements.txt lists it
    pytesseract = None

MRZ_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
_CANDIDATES = (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
               os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"))
TIMEOUT = 30                            # seconds per Tesseract run
_MRZ_CONFIG = f"--psm 6 -c tessedit_char_whitelist={MRZ_CHARS} -c load_system_dawg=0 -c load_freq_dawg=0"
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "T": "7", "Z": "2", "S": "5",
                           "B": "8", "G": "6", "A": "4"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A"})
_WIDTH = 2300                           # the zone is read at about this width: letters some 40 pixels high


class OcrError(Exception):
    """Tesseract failed on an image (not: found nothing on it)."""


def command():
    """The Tesseract executable, or "" when this machine has none."""
    for path in (os.environ.get("TESSERACT_CMD", ""), shutil.which("tesseract") or "", *_CANDIDATES):
        if path and os.path.isfile(path):
            return path
    return ""


def available():
    return pytesseract is not None and bool(command())


MODEL = {"model": "tesseract-ocr", "label": "OCR (Tesseract)"}
_UNSURE = (("document_number", "number"), ("date_of_birth", "dob"), ("date_of_expiry", "expiry"))


def id_read(data, field_keys):
    """(read, used): an uploaded ID read by OCR alone, in Scan ID's shape.
    With a passport zone found, its fields (a field whose check digit does
    not add up marked unsure); with none, no field: the case says so and
    goes to an analyst. OCR does not judge the photo (photo_judged False).
    OcrError when Tesseract fails."""
    started = time.monotonic()
    page_text, zone = text(data), mrz(data)
    fields = {k: "" for k in field_keys}
    read = {"document_type": "ID document", "issuing_country": "", "readable": bool(page_text), "problems": [],
            "fields": fields, "mrz": [], "uncertain": [], "note": "", "ocr_text": page_text[:4000],
            "photo_judged": False, "mrz_by": "OCR's reading", "fields_from_mrz": True}
    if zone:
        z = zone["fields"]
        found = {"surname": z["surname"], "given_names": z["given_names"],
                 "full_name": " ".join(x for x in (z["given_names"], z["surname"]) if x), "date_of_birth": z["dob"],
                 "sex": z["sex"], "nationality": z["nationality"], "document_number": z["number"], "date_of_expiry": z["expiry"]}
        fields.update({k: v for k, v in found.items() if k in fields})
        read.update(document_type="Passport", issuing_country=z["issuer"], mrz=list(zone["lines"]),
                    uncertain=[k for k, c in _UNSURE if not zone["checks"][c]],
                    note="Fields from the passport's machine-readable zone; OCR does not tell which printed line is which field.")
    else:
        read["note"] = ("OCR found no passport machine-readable zone, and on its own it cannot tell which printed line is the "
                        "name or which date is the birth date: no field is filled. The text it read is below.")
    return read, dict(MODEL, cost=0.0, seconds=round(time.monotonic() - started, 1))


def _run(image, config):
    pytesseract.pytesseract.tesseract_cmd = command()
    try:
        return pytesseract.image_to_string(image, lang="eng", config=config, timeout=TIMEOUT)
    except (pytesseract.TesseractError, RuntimeError, OSError) as exc:
        raise OcrError(f"Tesseract could not read the image: {exc}") from None


def _open(data):
    with Image.open(io.BytesIO(data)) as im:
        return ImageOps.grayscale(im)


def text(data):
    """The image's printed text, as Tesseract reads it (page layout found
    automatically)."""
    return _run(_open(data), "--psm 3").strip()


# ── the machine-readable zone ────────────────────────────────────────────────

def check_digit(field):
    """ICAO 9303: weights 7, 3, 1 over the field; digits as themselves,
    letters A=10 .. Z=35, the filler < as 0."""
    total = 0
    for i, ch in enumerate(field):
        value = int(ch) if ch.isdigit() else (ord(ch) - 55 if "A" <= ch <= "Z" else 0)
        total += value * (7, 3, 1)[i % 3]
    return str(total % 10)


def _lines(raw):
    """The MRZ-shaped lines of an OCR result: only MRZ characters, at least
    30 long (a line cut short is padded with fillers)."""
    out = []
    for line in raw.splitlines():
        line = "".join(ch for ch in line.upper().replace(" ", "").replace("«", "<<") if ch in MRZ_CHARS)
        if len(line) >= 30:
            out.append((line + "<" * 44)[:44])
    return out


def _date(yymmdd, future):
    d = dates.parse(f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}")
    if d is None:
        return ""
    if not future and d.year > dates.now().year:          # a birth date: last century
        d = d.replace(year=d.year - 100)
    return d.strftime("%Y-%m-%d")


def parse(line1, line2):
    """A TD3 (passport) MRZ's fields and check-digit results; None when the
    lines are not one."""
    if len(line1) != 44 or len(line2) != 44 or line1[0] != "P":
        return None
    digit = lambda s: s.translate(_TO_DIGIT)          # noqa: E731
    letter = lambda s: s.translate(_TO_LETTER)        # noqa: E731
    number, cd_num = line2[0:9], digit(line2[9])
    dob, cd_dob, exp, cd_exp = digit(line2[13:19]), digit(line2[19]), digit(line2[21:27]), digit(line2[27])
    optional, cd_opt, composite = line2[28:42], digit(line2[42]), digit(line2[43])
    checks = {"number": check_digit(number) == cd_num, "dob": check_digit(dob) == cd_dob,
              "expiry": check_digit(exp) == cd_exp,
              "optional": cd_opt in ("<", check_digit(optional)) if optional.strip("<") else cd_opt in ("<", "0"),
              "composite": check_digit(number + cd_num + dob + cd_dob + exp + cd_exp + optional + cd_opt) == composite}
    names = letter(line1[5:]).split("<<", 1)
    sex = line2[20] if line2[20] in "MF<" else {"H": "M", "E": "F"}.get(line2[20], "<")
    return {"valid": all(checks.values()), "checks": checks, "lines": [line1, line2],
            "fields": {"document_type": letter(line1[0:2]).rstrip("<"), "issuer": letter(line1[2:5]).rstrip("<"),
                       "surname": names[0].replace("<", " ").strip(),
                       "given_names": (names[1] if len(names) > 1 else "").replace("<", " ").strip(),
                       "number": number.rstrip("<"), "nationality": letter(line2[10:13]).rstrip("<"),
                       "dob": _date(dob, future=False), "sex": "" if sex == "<" else sex, "expiry": _date(exp, future=True)}}


def _vote(lines):
    """Each position's most frequent character across readings (the first
    reading's on a tie)."""
    out = []
    for chars in zip(*lines):
        counts = {}
        for ch in chars:
            counts[ch] = counts.get(ch, 0) + 1
        out.append(max(counts, key=counts.get))
    return "".join(out)


_BLANK = "P<" + "<" * 42


def _is_line2(line):
    """Line 2 holds the dates: digits where the date of birth and the expiry go."""
    return sum(ch.isdigit() for ch in line[13:28]) >= 10 and sum(ch.isdigit() for ch in line[13:19]) >= 5


def _score(line2):
    c = parse(_BLANK, line2)["checks"]
    return c["number"] + c["dob"] + c["expiry"] + c["composite"]


_FIELDS = (("number", 0, 10), ("dob", 13, 20), ("expiry", 21, 28))


def assemble(lines):
    """One MRZ from the lines several readings gave. A line 2 whose every
    check digit adds up wins as read (the one read most often); otherwise
    the best readings vote, each check-digit field (document number, date
    of birth, expiry) taking the value most readings agree on among those
    its own check digit confirms. Line 1 (the names; no check digit) is the
    readings' vote. None without a line 2."""
    twos = [x for x in lines if _is_line2(x)]
    if not twos:
        return None
    ones = [x for x in lines if x[0] == "P" and not _is_line2(x)]
    line1 = _vote(ones) if ones else _BLANK
    whole = [x for x in twos if parse(_BLANK, x)["valid"]]
    if whole:
        return parse(line1, max(whole, key=whole.count))
    top = max(map(_score, twos))
    line2 = list(_vote([x for x in twos if _score(x) >= top - 1]))
    for key, a, b in _FIELDS:
        confirmed = [cand[a:b] for cand in twos if parse(_BLANK, cand)["checks"][key]]
        if confirmed:
            line2[a:b] = max(confirmed, key=confirmed.count)
    if sum(ch == "<" for ch in line2[28:42]) >= 9:          # the optional field is mostly fillers: all of it is
        line2[28:43] = "<" * 15
    return parse(line1, "".join(line2))


def find(raw):
    """The MRZ in one OCR result, or None."""
    return assemble(_lines(raw))


def _skew(gray):
    """The small rotation (degrees, -3 to 3) that lines the text rows up
    best: the one whose dark-pixel row profile is sharpest."""
    small = gray.resize((max(1, gray.width // 2), max(1, gray.height // 2)))
    ink = small.point(lambda p: 1 if p < 128 else 0)
    best, sharpest = 0.0, -1.0
    for tenth in range(-30, 31, 5):
        turned = ink.rotate(tenth / 10, fillcolor=0)
        data, w = turned.tobytes(), turned.width
        rows = [sum(data[y * w:(y + 1) * w]) for y in range(turned.height)]
        mean = sum(rows) / len(rows)
        sharp = sum((r - mean) ** 2 for r in rows)
        if sharp > sharpest:
            best, sharpest = tenth / 10, sharp
    return best


def _readings(region):
    """The MRZ-shaped lines of readings of one region of the page: level at
    the estimated tilt, then half a degree and a degree either side of it,
    then with the strokes thickened, until a reading's check digits all add
    up."""
    guess = _skew(region)
    angles = []
    for step in (0, -0.5, 0.5, -1.0, 1.0):
        angle = max(-3.0, min(3.0, guess + step))
        if angle not in angles:
            angles.append(angle)
    out = []
    for angle, thicken in [(x, False) for x in angles] + [(angles[0], True)]:
        level = region.rotate(angle, resample=Image.BICUBIC, fillcolor=255) if angle else region
        factor = _WIDTH / level.width
        img = level.resize((max(1, int(level.width * factor)), max(1, int(level.height * factor))), Image.LANCZOS)
        if thicken:
            img = img.filter(ImageFilter.MinFilter(3))
        out += _lines(_run(ImageOps.expand(img, border=40, fill=255), _MRZ_CONFIG))
        got = assemble(out)
        if got and got["valid"]:
            break
    return out


def mrz(data):
    """The passport data page's MRZ, read by OCR: the lower part of the page
    (where the zone is), then the whole picture (a passport small in a
    photo); None when no MRZ was found."""
    page = _open(data)
    pool, best = [], None
    for top in (0.70, 0.55, 0.0):
        pool += _readings(page.crop((0, int(page.height * top), page.width, page.height)))
        best = assemble(pool)
        if best and best["valid"]:
            break
    return best
