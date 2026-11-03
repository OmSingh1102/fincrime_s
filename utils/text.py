"""Text helpers: transliteration for MRZ lines and PDF fallbacks, initials,
escaping for SVG/XML."""
from xml.sax.saxutils import escape as _xml_escape

# Cyrillic → Latin, ICAO 9303-style (the scheme passports use in the MRZ).
_CYR = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "ZH", "З": "Z",
    "И": "I", "Й": "I", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R",
    "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "KH", "Ц": "TS", "Ч": "CH", "Ш": "SH",
    "Щ": "SHCH", "Ъ": "IE", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "IU", "Я": "IA",
}
for _k, _v in list(_CYR.items()):
    _CYR[_k.lower()] = _v.lower() if len(_v) == 1 else _v.capitalize()


def translit(text: str) -> str:
    """Latin rendering of a string that may carry Cyrillic; other text passes through."""
    return "".join(_CYR.get(ch, ch) for ch in (text or ""))


def is_ascii(text: str) -> bool:
    try:
        (text or "").encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def initials(name: str, n: int = 2) -> str:
    parts = [p for p in (name or "").replace("_", " ").replace(".", " ").split() if p]
    return "".join(p[0] for p in parts[:n]).upper() or "?"


def esc(text) -> str:
    """Escape for SVG / XML text nodes and attributes."""
    return _xml_escape(str(text if text is not None else ""), {'"': "&quot;"})


def mrz_field(text: str) -> str:
    """Uppercase Latin with everything that is not A-Z or 0-9 turned into '<'."""
    out = []
    for ch in translit(text or "").upper():
        out.append(ch if ch.isalnum() and ch.isascii() else "<")
    return "".join(out)


def mrz_name(surname: str, given: str) -> str:
    return f"{mrz_field(surname)}<<{mrz_field(given).replace(' ', '<')}"


def split_name(full: str):
    """(surname, given names) — the last token is the surname."""
    parts = (full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def display_name(email: str) -> str:
    """'amara.iyer@straive.com' → 'Amara Iyer'."""
    local = (email or "").split("@")[0]
    return " ".join(p.capitalize() for p in local.replace("_", ".").split(".") if p)


def script_of(text: str) -> str:
    """Which writing system a name uses: cyrillic | arabic | latin. Drives the
    document-skill match in routing; anything outside the two non-Latin
    blocks counts as latin."""
    for ch in text or "":
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            return "cyrillic"
        if 0x0600 <= o <= 0x06FF:
            return "arabic"
    return "latin"
