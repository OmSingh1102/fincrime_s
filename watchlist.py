"""The stand-in World-Check: the World-Check side of the reconciliation log's
1,000 cases, searched for the person on an uploaded ID while there is no
World-Check licence. Every row of the log carries the record its customer
was matched to (the UID, the listed name and aliases, the listed date of
birth, the places, the list, why the person is listed); here the 1,000 rows
are one watchlist of 1,000 profiles. The customer side of the rows is not
used, and the note the analysts wrote beside the listed date of birth
("Exact Match", "21 yrs diff") is dropped: it is their answer, not the
record.

The search finds candidates the way World-Check's own name search does: a
name score from the names alone, word order, diacritics, hyphens and a
missing middle name set aside, with the date of birth and the country
reported beside each candidate (matched, not matched, unknown) and never
used to drop one. Whether a candidate is the person on the ID is not
decided here: that is the screening model's job, under the SOP
(fincrime/id_screening.py). The score below which a name is not a candidate
and the number of candidates kept are settings, on the Settings page.
"""
import re
from functools import lru_cache

from fincrime.filebook import loader, parse, similarity

STRENGTHS = ((99, "Exact"), (90, "Strong"), (80, "Medium"), (0, "Weak"))
_cache = {}


def _dob_text(dob):
    """The listed date of birth as the record gives it, without the note."""
    if dob["kind"] == "full":
        return dob["date"]
    if dob["kind"] == "year":
        return f"{dob['year']} (year only)"
    if dob["kind"] == "range":
        return f"{dob['year']}–{dob['year_to']} (range)"
    return "Not listed"


def _profile(row):
    n = parse.wc_name(row["world_check_entity_subject_name"])
    primary = f"{n['surname']}, {n['given']}" if n["given"] else n["surname"]
    names = [f"{n['given']} {n['surname']}".strip()] + list(n["aka"])
    dob = parse.wc_dob(row["world_check_listed_dob"])
    return {"uid": row["world_check_uid"], "name": primary, "aka": list(n["aka"]), "names": names,
            "dob": dob, "dob_text": _dob_text(dob), "places": row["world_check_listed_address_jurisdiction"],
            "countries": sorted({p["country"] for p in parse.wc_location(row["world_check_listed_address_jurisdiction"])["places"]
                                 if p["country"]}),
            "category": row["watchlist_category"], "narrative": row["world_check_narrative_trigger_details"],
            "row": row["kyc_case_id"]}


def profiles():
    """Every record of the stand-in watchlist (read once)."""
    if "profiles" not in _cache:
        _cache["profiles"] = [_profile(r) for r in loader.read_rows()]
    return _cache["profiles"]


def profile(uid):
    return next((p for p in profiles() if p["uid"] == uid), None)


def row_of(uid):
    """The log row a stand-in record comes from (its World-Check columns)."""
    p = profile(uid)
    return next((r for r in loader.read_rows() if r["kyc_case_id"] == p["row"]), None) if p else None


# ── the name search ──────────────────────────────────────────────────────────

@lru_cache(maxsize=8192)
def _words(text):
    out = []
    for t in similarity.tokens(text):
        out += [similarity.squash(p) for p in t.split("-") if similarity.squash(p)]
    return tuple(out)


def _joined(words, other):
    """Two neighbouring words written as one where the other name has them
    as one (O CONNOR / OCONNOR, VAN DYKE / VANDYKE)."""
    out, i, others = [], 0, set(other)
    while i < len(words):
        if i + 1 < len(words) and words[i] + words[i + 1] in others:
            out.append(words[i] + words[i + 1])
            i += 2
        else:
            out.append(words[i])
            i += 1
    return out


@lru_cache(maxsize=262144)
def _word_score(a, b):
    if a == b:
        return 1.0
    if len(a) == 1 or len(b) == 1:                     # an initial
        return 0.8 if a[0] == b[0] else 0.0
    return similarity.token_score(a, b)


def name_score(a, b):
    """0-100: how alike two names are, word order aside. Each word of the
    shorter name takes its best partner in the longer one; a word the
    longer name has beyond them (a middle name) costs a quarter of a word."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0
    wa, wb = _joined(wa, wb), _joined(wb, wa)
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    free, total = list(long_), 0.0
    for w in short:
        best = max(free, key=lambda o: _word_score(w, o))
        s = _word_score(w, best)
        if s:
            free.remove(best)
            total += s
    return round(100 * total / (len(short) + 0.25 * (len(long_) - len(short))))


def strength(score):
    return next(label for floor, label in STRENGTHS if score >= floor)


def _dob_result(dob_iso, dob):
    if not dob_iso or dob["kind"] == "none":
        return "Unknown"
    year = int(dob_iso[:4]) if re.match(r"\d{4}-\d{2}-\d{2}$", dob_iso) else None
    if dob["kind"] == "full":
        return "Matched" if dob["date"] == dob_iso else "Not matched"
    if year is None:
        return "Unknown"
    if dob["kind"] == "year":
        return "Matched" if dob["year"] == year else "Not matched"
    return "Matched" if dob["year"] <= year <= dob["year_to"] else "Not matched"


_COUNTRY_SAME = {"UNITED STATES": {"USA", "US", "UNITED STATES OF AMERICA"}, "VIETNAM": {"VIET NAM", "VNM", "VN"},
                 "INDIA": {"IND", "IN"}}


def _country_key(text):
    t = similarity.fold(re.sub(r"\(.*?\)", "", text or ""))
    for key, also in _COUNTRY_SAME.items():
        if t == key or t in also:
            return key
    return t


def _country_result(countries, person):
    mine = {_country_key(person.get(k)) for k in ("issuing_country", "nationality")} - {""}
    if not mine or not countries:
        return "Unknown"
    return "Matched" if mine & {_country_key(c) for c in countries} else "Not matched"


def search(person, threshold=65, limit=5):
    """The records whose name scores `threshold` or more against the name on
    the ID, best first, at most `limit`: each with its name score and
    strength, and the date of birth and country reported beside it. Among
    names that score the same, a record whose date of birth, then country,
    matches comes first (they order the hits; they never drop one).

    On the 1,000 rows, searched with the passport's name, the date of birth
    and the country, the floor of 65 finds the customer's own record for all
    but a few dozen (two-word Vietnamese names crowded out of the top 5, a
    nickname such as Jimmy for James), and every true match."""
    name = (person.get("name") or "").strip()
    if not name:
        return []
    found = []
    for p in profiles():
        score = max(name_score(name, n) for n in p["names"] if n)
        if score >= threshold:
            found.append((score, _dob_result(person.get("dob", ""), p["dob"]), _country_result(p["countries"], person), p))
    rank = {"Matched": 0, "Unknown": 1, "Not matched": 2}
    found.sort(key=lambda t: (-t[0], rank[t[1]], rank[t[2]], t[3]["uid"]))
    return [{"uid": p["uid"], "name": p["name"], "aka": p["aka"], "dob_text": p["dob_text"], "places": p["places"],
             "category": p["category"], "narrative": p["narrative"], "score": score, "strength": strength(score),
             "dob_result": dob, "country_result": country, "source": "stand-in", "row": p["row"]}
            for score, dob, country, p in found[:limit]]
