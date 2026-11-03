"""Folding, string similarity and phonetic keys for name matching.

Pure Python on purpose: the demo adds no dependency, and the whole book of
1,000 cases is matched in well under a second.
"""
import re
import unicodedata

# Letters that NFD cannot decompose into a base letter plus marks.
_MAP = {"Đ": "D", "đ": "d", "Ð": "D", "ð": "d", "Ł": "L", "ł": "l", "Ø": "O", "ø": "o", "ß": "ss", "Æ": "AE",
        "æ": "ae", "Œ": "OE", "œ": "oe", "Þ": "TH", "þ": "th", "ı": "i"}


def fold(text: str) -> str:
    """Upper-case ASCII for matching: Đ/Ð → D, diacritics stripped,
    punctuation other than apostrophes and hyphens turned into spaces."""
    s = "".join(_MAP.get(ch, ch) for ch in (text or ""))
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.upper().replace("’", "'")
    s = re.sub(r"[^A-Z0-9'\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str) -> list:
    return [t for t in fold(text).split() if t]


def squash(token: str) -> str:
    """A token without apostrophes or hyphens: O'BRIEN → OBRIEN."""
    return re.sub(r"[^A-Z0-9]", "", token or "")


def has_marks(text: str) -> bool:
    """Does the text carry diacritics or a D-stroke (information folding loses)?"""
    s = unicodedata.normalize("NFD", text or "")
    return any(unicodedata.category(ch) == "Mn" for ch in s) or any(ch in _MAP for ch in (text or ""))


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(la, lb) // 2 - 1
    if window < 0:
        window = 0
    ma, mb = [False] * la, [False] * lb
    matches = 0
    for i, ch in enumerate(a):
        lo, hi = max(0, i - window), min(lb, i + window + 1)
        for j in range(lo, hi):
            if not mb[j] and b[j] == ch:
                ma[i] = mb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    k = 0
    transpositions = 0
    for i in range(la):
        if ma[i]:
            while not mb[k]:
                k += 1
            if a[i] != b[k]:
                transpositions += 1
            k += 1
    transpositions //= 2
    return (matches / la + matches / lb + (matches - transpositions) / matches) / 3.0


def jaro_winkler(a: str, b: str, scale: float = 0.1, max_prefix: int = 4) -> float:
    a, b = squash(fold(a)), squash(fold(b))
    j = jaro(a, b)
    prefix = 0
    for x, y in zip(a, b):
        if x != y or prefix == max_prefix:
            break
        prefix += 1
    return j + prefix * scale * (1.0 - j)


_PHONETIC = (("PH", "F"), ("KH", "K"), ("GH", "G"), ("TH", "T"), ("DH", "D"), ("BH", "B"), ("SH", "S"), ("CH", "C"),
             ("CK", "K"), ("Q", "K"), ("W", "V"), ("Z", "S"), ("X", "KS"), ("EE", "I"), ("OO", "U"), ("OU", "U"),
             ("AU", "O"), ("EI", "I"), ("Y", "I"))


def phonetic_key(token: str) -> str:
    """A rough key under which Indic and Vietnamese romanisation variants
    collapse: CHAUDHARY / CHOUDHARY / CHOWDHURY → CDR, KURESHI / QURESHI → KRS."""
    t = squash(fold(token))
    for a, b in _PHONETIC:
        t = t.replace(a, b)
    t = re.sub(r"(.)\1+", r"\1", t)
    if t:
        t = t[0] + re.sub(r"[AEIOU]", "", t[1:])
    return t


def token_score(a: str, b: str) -> float:
    """How alike two folded name tokens are: 1 exact, 0.9 near, 0 apart."""
    if not a or not b:
        return 0.0
    if a == b or squash(a) == squash(b):
        return 1.0
    if phonetic_key(a) == phonetic_key(b) and jaro_winkler(a, b) >= 0.8:
        return 0.9
    jw = jaro_winkler(a, b)
    if jw >= 0.92:
        return 0.9
    if jw >= 0.85:
        return 0.7
    return 0.0
