"""The two clocks from the SOP: 48 hours for confirming and screening, 14 days
for an EDD escalation, both as the SOP rules page sets them (engines/sop):
each deadline is counted from when the case was ingested, or escalated, at
the current setting, so a change moves every open case's deadline at once.
The intake clock starts at ingestion and is never reset by a resubmit or an
L2 return — a case bounced back and forth still shows how long the customer
has been waiting."""
from fincrime.data import vocab
from fincrime.engines import sop
from fincrime.utils import dates


def active_due(case):
    """Which deadline applies right now, and its label."""
    if case["status"] in vocab.CLOSED_STATUSES:
        return "", "closed"
    if case["status"] == "DataEscalatingL2" and case.get("edd_due_at"):
        start = case.get("escalated_at")
        return (dates.add_hours(start, sop.value("sla_days_edd") * 24) if start else case["edd_due_at"]), "EDD"
    start = case.get("created_at")
    return (dates.add_hours(start, sop.value("sla_hours_l1")) if start else case.get("sla_due_at", "")), "L1"


def remaining_hours(case, now=None):
    due, _ = active_due(case)
    return dates.hours_until(due, now) if due else 0.0


def breached(case, now=None):
    if case["status"] in vocab.CLOSED_STATUSES:
        return False
    return remaining_hours(case, now) < 0


def _human(hours):
    negative = hours < 0
    h = abs(hours)
    if h >= 48:
        text = f"{int(h // 24)}d {int(h % 24)}h"
    elif h >= 1:
        text = f"{int(h)}h {int(round((h - int(h)) * 60)):02d}m"
    else:
        text = f"{int(round(h * 60))}m"
    return ("-" if negative else "") + text


def waiting_on_user(case):
    """An RFI sent that nobody holds (the AI Agent's): the case waits for the
    user's reply, not for an analyst (routing.level_for)."""
    return case.get("pending_reason") == "RFI Sent" and not case.get("current_reviewer") \
        and case["status"] in ("DataConfirming", "DataScreening")


def chip(case, now=None):
    """{text, tone, title} for the list and the case header."""
    if case["status"] in vocab.CLOSED_STATUSES:
        return {"text": "Closed", "tone": "done", "title": f"Closed {case.get('closed_at', '')}"}
    if case["status"].startswith("Resubmit"):
        return {"text": "Waiting on user", "tone": "done", "title": "Resubmission requested; the intake clock keeps running"}
    if waiting_on_user(case):
        return {"text": "Waiting on user", "tone": "done", "title": "Information requested from the user; the intake clock keeps running"}
    due, label = active_due(case)
    hours = dates.hours_until(due, now)
    tone = "late" if hours < 0 else ("warn" if hours <= 12 else "ok")
    return {"text": ("Breached " if hours < 0 else "") + _human(hours) + (" left" if hours >= 0 else ""),
            "tone": tone, "title": f"{label} SLA due {due}"}
