"""Who held a case, at which level, from when to when, and how it left their
hands.

Every ownership change — Reserve, Assign, Release, an automatic route, a
hand-off, a close — opens or closes an entry here. The Productivity / Time
Tracker is a flat view of these entries, and the shift-progress metrics
(handle time, work in progress) are computed from them. Nothing else in the
app reads them, so an entry that is slightly off never changes a decision.
"""
from fincrime.utils import dates


def open_entry(case, email, level, at=None):
    """Start an entry for `email`; closes any other analyst's open entry
    first. Idempotent: an analyst already holding the case keeps their entry."""
    now = at or dates.now_str()
    entries = case.setdefault("work", [])
    for w in entries:
        if w.get("to"):
            continue
        if w["analyst"] == email:
            return w
        w["to"], w["outcome"] = now, w.get("outcome") or "Reassigned"
    entry = {"level": level, "analyst": email, "from": now, "to": "", "outcome": ""}
    entries.append(entry)
    case["assigned_at"] = now
    return entry


def close_entry(case, outcome, at=None, email=None):
    """Close the newest open entry (optionally only `email`'s) with `outcome`."""
    now = at or dates.now_str()
    for w in reversed(case.get("work") or []):
        if w.get("to") or (email and w["analyst"] != email):
            continue
        w["to"], w["outcome"] = now, outcome or ""
        return w
    return None


def hours(entry, now=None):
    """Handle time of one entry in hours; an open entry counts up to now."""
    start = dates.parse(entry.get("from"))
    end = dates.parse(entry.get("to")) or (now or dates.now())
    return round((end - start).total_seconds() / 3600, 2) if start else 0.0


def open_for(case, email):
    return next((w for w in reversed(case.get("work") or []) if not w.get("to") and w["analyst"] == email), None)
