"""Mock open-source-intelligence report for the US-person path (SOP 4.1.3,
step 4.2.1). A request is recorded, and completing it produces a short
comparison the analyst reads against the watchlist record — name, relatives,
location history and public records, the four things the SOP says to compare.
Everything is invented from the case's own data; each section carries a
match flag so the analyst's review can point at the section that did not
line up."""
from fincrime.utils import dates


def request_report(case, actor):
    return {"status": "Requested", "requested_at": dates.now_str(), "requested_by": actor,
            "subject": case["client"]["legal_name"], "report": None, "outcome": "", "reviewed_by": "",
            "reviewed_at": "", "notes": ""}


def _city(address):
    """The city token of a comma-separated address: the part before the
    state/postcode part when there are three or more, else the last part,
    with any postcode digits dropped."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if not parts:
        return "the stated address"
    candidate = parts[-2] if len(parts) >= 3 else parts[-1]
    words = [w for w in candidate.split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words) or candidate


def complete_report(record, outcome, actor, notes, case=None):
    client = (case or {}).get("client") or {}
    name = client.get("legal_name") or record.get("subject") or "the subject"
    city = _city(client.get("address"))
    id_type = {"DrivingLicense": "driving licence", "Passport": "passport"}.get(client.get("id_type"), "identity document")
    cleared = outcome == "Cleared"
    sections = [
        {"title": "Name & aliases", "match": "Consistent",
         "text": f"{name.title()} appears under the legal name only; no aliases or alternate spellings in public records."},
        {"title": "Relatives & associates", "match": "Consistent",
         "text": "Two immediate family members identified from public records; none appears on any watchlist."},
        {"title": "Location history", "match": "Consistent" if cleared else "Gap",
         "text": (f"Continuous residence history around {city} for the last 9 years, consistent with the {id_type}."
                  if cleared else f"Residence history around {city} has an unexplained 3-year gap that overlaps the watchlist record's activity period.")},
        {"title": "Public records", "match": "Consistent" if cleared else "Possible link",
         "text": ("No criminal filings, liens or judgments. Two professional licence records, both current."
                  if cleared else "A civil filing names a party with the same name and year of birth; the record does not carry an identifier to rule the subject out.")},
        {"title": "Adverse media", "match": "Consistent" if cleared else "Inconclusive",
         "text": ("No press mentions linked to the subject's identifiers."
                  if cleared else "One press mention could refer to the subject; identifiers are insufficient to confirm or exclude.")},
    ]
    record = dict(record)
    record.update({
        "status": outcome, "outcome": outcome, "reviewed_by": actor, "reviewed_at": dates.now_str(), "notes": notes,
        "report": {"generated_at": dates.now_str(), "sections": sections},
    })
    return record
