"""The KYC case state machine.

Every analyst action on a case is a function here. Each one validates the
state it needs, refuses with a WorkflowError it does not, mutates the record
in place, and appends an audit event — so nothing changes on a case without a
line in its history saying who did it and why. The routes stay thin: gather
the form, call one of these, save, redirect.

Two rules the earlier prototype got wrong are enforced here on purpose:
every outcome needs the analyst's own comment (nothing is ever written into
the audit trail on their behalf — the router and the AI screening rules sign
their own rows as themselves), and `reviewers_by_level` is append-only so
the segregation check still knows who touched a case at L1 after it has been
returned and picked up by somebody else.

The AI screening rules (engines/ai_screening) run again at the re-screen
after Data Confirming. An individual's case they leave nothing on is auto-verified
here without an analyst step; a business always keeps its human close.

Ownership changes also keep the work log (engines/worklog) and, on every
tier hand-off, ask the router (engines/routing) for the next analyst at
once — SOP 5.2.3, no bot lag.

    Submitted → IDVerification → DataConfirming ──OK──▶ DataScreening ──review: FP / no match──▶ Verified
                                      │  │                     │                (business: Checker 2 signs off first)
                                      │  └─Resubmit─▶ ResubmitID / ResubmitPOA
                                      └────Escalate─▶ DataEscalatingL2 ◀──review: TP / potential match──┘
                                                              └─ L2/EDD: Approve → Verified | Reject → Rejected | Escalate to EDD
"""
from fincrime.adapters import cms, skopenow
from fincrime.data import vocab
from fincrime.engines import ai_screening, routing, segregation, sop, worklog
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates

# Kept as an alias of the shipped list (vocab, the demo's stand-in for the
# internal library of sanctioned / prohibited jurisdictions); the checks read
# the SOP's current list (sop.value("sanctioned_countries")).
HIGH_RISK_COUNTRIES = vocab.SANCTIONED_OR_PROHIBITED


# ── helpers ──────────────────────────────────────────────────────────────────

def _audit(case, actor, action, detail=""):
    case.setdefault("audit", []).append({"at": dates.now_str(), "actor": actor, "action": action, "detail": detail})


def _change(case, actor, field, before, after):
    case.setdefault("change_log", []).append({"at": dates.now_str(), "by": actor, "field": field,
                                             "from": before, "to": after})


def _point(case, label, at=None):
    for p in case["verification_points"]:
        if p["label"] == label:
            p["at"] = at or dates.now_str()
            return


def _decision(case, check_point, **fields):
    for row in case["review_decisions"]:
        if row["check_point"] == check_point:
            row.update(fields)
            return


def _touch_level(case, level, actor):
    levels = case.setdefault("reviewers_by_level", {"L1": [], "L2": [], "EDD": []})
    if actor and actor not in levels.setdefault(level, []):
        levels[level].append(actor)


def _require(condition, message):
    if not condition:
        raise WorkflowError(message)


def _require_text(value, what):
    if not (value or "").strip():
        raise WorkflowError(f"{what} is required.")
    return value.strip()


def _require_open(case):
    _require(case["status"] not in vocab.CLOSED_STATUSES, "This case is closed.")


def _require_reserved(case, actor):
    _require(case["current_reviewer"] == actor,
             "Reserve the case first — only the current reviewer can act on it.")


def _hand_back(case, actor, outcome=""):
    """The reviewer's turn is over: they become the previous reviewer and
    their work-log entry closes with how the case left them."""
    case["previous_reviewer"] = actor
    case["current_reviewer"] = ""
    worklog.close_entry(case, outcome, email=actor)


def _open_investigation_file(case, actor, now):
    """SOP 4.1.3 §6.2: the investigation folder, named UID + (case id). Its
    contents are read live from the case (engines/investigation)."""
    if case.get("investigation_file"):
        return
    case["investigation_file"] = {"name": f"{case['client_id']} ({case['id']})", "created_at": now, "created_by": actor}
    _audit(case, actor, "Investigation file opened", f"Folder {case['investigation_file']['name']} — WC export, Skopenow report and attachments collect here.")


# ── read models ──────────────────────────────────────────────────────────────

def verification_points(case):
    """The six steps with a state each: done | current | todo."""
    status = case["status"]
    step = vocab.STATUS_STEP.get(status, 0)
    closed = status in vocab.CLOSED_STATUSES
    out = []
    for i, p in enumerate(case["verification_points"]):
        # A case that closed without ever being escalated shows five steps,
        # not six — the L2 step drops out, as it does on the real page.
        if closed and p["label"] == "Data Escalating L2" and not p["at"]:
            continue
        if closed:
            state = "done"
        elif i < step:
            state = "done"
        elif i == step:
            state = "current"
        else:
            state = "todo"
        out.append({"label": p["label"], "at": p["at"], "state": state, "n": i + 1})
    return out


def review_action(case, levels, actor=""):
    """Which modal the Review button opens for an analyst holding `levels`."""
    s = case["status"]
    if s == "DataScreening" and case.get("pending_reason") == "Pending Checker 2":
        checker1 = (case.get("checker1") or {}).get("by")
        return "checker2" if (levels & {"L1", "L2"} and actor != checker1) else ""
    if s == "DataConfirming" and "L1" in levels:
        return "dc"
    if s == "DataScreening" and levels & {"L1", "L2"}:
        return "edd"
    if s == "DataEscalatingL2" and levels & {"L2", "EDD"}:
        return "l2"
    return ""


def review_needs(case):
    """(stage, levels) the Review button wants at this stage — the text shown
    beside the greyed button when the signed-in analyst lacks the level, e.g.
    after a team lead moved an intake case by hand. ("", []) when the stage
    has no review step at all."""
    s = case["status"]
    if s == "DataScreening" and case.get("pending_reason") == "Pending Checker 2":
        return "Checker 2 (not the Checker 1 reviewer)", ["L1", "L2"]
    return {"DataConfirming": ("Data Confirming", ["L1"]),
            "DataScreening": ("Data Screening", ["L1", "L2"]),
            "DataEscalatingL2": ("The L2 / EDD review", ["L2", "EDD"])}.get(s, ("", []))


# ── ownership ────────────────────────────────────────────────────────────────

def reserve(case, actor):
    _require_open(case)
    _require(not case["current_reviewer"] or case["current_reviewer"] == actor,
             f"Already reserved by {case['current_reviewer']}. Ask them to release it or use Assign to Others.")
    now = dates.now_str()
    case["current_reviewer"] = actor
    worklog.open_entry(case, actor, routing.level_for(case) or "L1", now)
    routing.complete_handoff(case, actor, now)
    _audit(case, actor, "Reserved", "Case reserved for review.")


def release(case, actor):
    _require(case["current_reviewer"], "The case is not reserved.")
    _require(case["current_reviewer"] == actor, "Only the current reviewer can release the case.")
    case["current_reviewer"] = ""
    worklog.close_entry(case, "Released", email=actor)
    _audit(case, actor, "Released", "Case returned to the queue.")


def assign(case, actor, assignee):
    _require_open(case)
    assignee = _require_text(assignee, "An assignee")
    before = case["current_reviewer"]
    now = dates.now_str()
    if before and before != assignee:
        worklog.close_entry(case, "Reassigned", now, email=before)
    case["current_reviewer"] = assignee
    worklog.open_entry(case, assignee, routing.level_for(case) or "L1", now)
    routing.complete_handoff(case, assignee, now)
    _audit(case, actor, "Assigned", f"Assigned to {assignee}" + (f" (was {before})" if before else "") + ".")


# ── L1: data confirming ──────────────────────────────────────────────────────

CONFIRM_FIELDS = (("name", "legal_name", "Name", "text"), ("dob", "dob", "Birthday", "date"),
                  ("nationality", "nationality", "Nationality", "text"))
CONFIRM_FIELDS_KYB = (("name", "legal_name", "Legal name", "text"), ("id_number", "id_number", "Registration number", "text"),
                      ("nationality", "nationality", "Country of incorporation", "text"))


def confirm_fields(case):
    """(form field, client key, label, input type) × 3 — a person confirms
    name / birthday / nationality; a business confirms name / registration /
    country of incorporation."""
    return CONFIRM_FIELDS_KYB if case.get("agency") else CONFIRM_FIELDS


def poa_docs(case):
    return [d for d in case.get("documents", []) if d.get("category") == "ProofOfAddress"]


def poa_checks(case, now=None):
    """SOP 4.1.2 step 1, computed from the uploaded proof of address: issued
    within six months, an accepted document type, not from a sanctioned or
    prohibited country — and the rule for a sanctioned-country ID (same
    country on the POA ⇒ escalate; a clean POA ⇒ process normally). Empty
    when the case needs no POA."""
    if not case.get("poa_required"):
        return []
    docs = poa_docs(case)
    if not docs:
        return [{"label": "Proof of address uploaded", "ok": False, "detail": "No proof of address on the case."}]
    d = docs[0]
    checks = []
    kind = d.get("kind", "")
    issued = dates.parse(d.get("issued_on") or "")
    age_label, limit, sanctioned = sop.poa_age_label(), sop.value("poa_max_age_days"), set(sop.value("sanctioned_countries"))
    if kind in sop.value("poa_no_age_limit"):
        checks.append({"key": "poa_age", "label": age_label, "ok": True,
                       "detail": f"not required: {vocab.ACCEPTED_POA_TYPES.get(kind, kind)} is a permanent card"})
    elif issued is None:
        checks.append({"key": "poa_age", "label": age_label, "ok": False, "detail": "The document shows no issue date."})
    else:
        age = ((now or dates.now()) - issued).days
        checks.append({"key": "poa_age", "label": age_label, "ok": age <= limit,
                       "detail": f"issued {d['issued_on']}, {age} days ago (limit {limit})"})
    checks.append({"key": "poa_type", "label": "Accepted document type (internal library)", "ok": kind in sop.value("accepted_poa_types"),
                   "detail": vocab.ACCEPTED_POA_TYPES.get(kind, kind.replace("_", " ") or "unknown type")})
    country = (d.get("country") or "").upper()
    cname = vocab.COUNTRY_NAMES.get(country, country or "unknown country")
    checks.append({"key": "poa_country", "label": "Not from a sanctioned or prohibited country", "ok": country not in sanctioned,
                   "detail": f"issued in {cname}"})
    id_country = (case["client"].get("id_issuing_place") or case["client"].get("nationality") or "").upper()
    if id_country in sanctioned:
        idname = vocab.COUNTRY_NAMES.get(id_country, id_country)
        same = country == id_country
        checks.append({"key": "sanctioned_id", "label": "Sanctioned-country ID rule (SOP 4.1.2 §2.3)", "ok": not same,
                       "detail": (f"ID and POA are both from {idname} — escalate" if same
                                  else f"ID from {idname}, POA from {cname} — process normally")})
    return checks


def poa_failures(case, now=None):
    return [c for c in poa_checks(case, now) if not c["ok"]]


def data_confirm(case, actor, form, alert_case=None, wc=None):
    """OK on the Data Confirm modal: all three fields confirmed (or edited and
    confirmed), the POA checks confirmed when a POA is required, optional
    summary and attachment. Moves the case to Data Screening, re-runs the
    name screen on the confirmed data and lets the AI screening rules act on
    it; an individual's case with nothing left for an analyst verifies itself."""
    _require(case["status"] == "DataConfirming", "Data confirming is only available while the case is in Data Confirming.")
    _require_reserved(case, actor)
    values = {}
    for field, client_key, label, _type in confirm_fields(case):
        _require(form.get(f"{field}_ok") == "1", f"Confirm {label} before pressing OK.")
        values[field] = _require_text(form.get(field, ""), label)
    if case.get("poa_required"):
        _require_poa(case)
        _require(form.get("poa_ok") == "1", "Confirm the proof of address checks before pressing OK.")
    confirm_data(case, actor, values, (form.get("summary") or "").strip()[:200], (form.get("attachment_name") or "").strip(),
                 alert_case, wc)


def _require_poa(case):
    failures = poa_failures(case)
    _require(not failures, "The proof of address fails a check: "
             + "; ".join(f"{f['label']} — {f['detail']}" for f in failures)
             + ". Use Resubmit POA, or Escalate when the sanctioned-country rule applies.")


def confirm_data(case, actor, values, summary="", attachment="", alert_case=None, wc=None):
    """The part of Data Confirm an analyst and the AI Agent share: write the
    confirmed values, move to Data Screening, re-screen, and auto-verify
    when nothing is left. `values` maps each confirm field to its value."""
    _require(case["status"] == "DataConfirming", "Data confirming is only available while the case is in Data Confirming.")
    now = dates.now_str()
    edited = []
    for field, client_key, label, _type in confirm_fields(case):
        value = _require_text(values.get(field, ""), label)
        if field == "dob":
            _require(dates.parse(value) is not None, "Birthday must be a date (YYYY-MM-DD).")
        before = case["client"].get(client_key, "")
        changed = value != before
        if changed:
            _change(case, actor, label, before, value)
            case["client"][client_key] = value
            edited.append(label)
        case["confirmations"][field] = {"value": value, "confirmed_by": actor, "at": now, "edited": changed}
    if case.get("poa_required"):
        _require_poa(case)
        case["confirmations"]["poa"] = {"value": "checked", "confirmed_by": actor, "at": now, "edited": False}
    if attachment:
        case["attachments"].append({"name": attachment, "at": now, "by": actor, "ref": "data_confirm"})

    _point(case, "Data Confirming", now)
    _decision(case, "Data Confirming", decision="Pass", operated_at=now, by=actor,
              id_summary=summary, report=attachment)
    _touch_level(case, "L1", actor)
    case["status"] = "DataScreening"
    case["pending_reason"] = ""
    # The tool re-screens on the confirmed data; the second run appears as a
    # further pair of rows under Screening Result.
    case["screening"]["rows"].extend(cms.rescreen_rows(case, now))
    detail = "Name, date of birth and nationality confirmed against the document."
    if case.get("poa_required"):
        detail += " Proof of address checked (age, type, country)."
    if edited:
        detail += f" Edited: {', '.join(edited)}."
    if summary:
        detail += f" Summary: {summary}"
    _audit(case, actor, "Data confirmed", detail)
    ai_screening.apply(case, alert_case, wc, at=now, run="rescreen")
    if _auto_verify_allowed(case) and case["screening"].get("workflow") == "auto-verified":
        problems = id_problems(case)
        if problems:
            # The screening is clear but the ID is not: a person looks before
            # anything verifies (the rules never approve a bad ID).
            case["screening"]["workflow"] = "human-in-the-loop"
            _audit(case, vocab.SYSTEM_USER, "Auto-verify held", "; ".join(problems) + " — an analyst reviews the case.")
        else:
            auto_verify(case, actor, now)


def _auto_verify_allowed(case):
    """Individuals verify themselves when the rules leave nothing; businesses never
    do (SOP 4.2: Checker 1 recalculates the risk, Checker 2 signs off)."""
    return case["tenant"] == "ttlive"


def id_problems(case, now=None):
    """Why the ID on file cannot be approved without a person: the vendor
    declined it, or it has expired. Empty when neither."""
    out = []
    vc = case.get("vendor_check") or {}
    if vc.get("result") == "Declined":
        out.append("ID declined by the vendor" + (f": {vc['vendor_reason']}" if vc.get("vendor_reason") else ""))
    expiry = dates.parse((vc.get("extracted") or {}).get("expiry") or "")
    if expiry is not None and expiry < (now or dates.now()):
        out.append(f"ID expired on {expiry:%Y-%m-%d}")
    return out


def auto_verify(case, actor, now):
    """The auto-verified workflow's last step: Data Confirming is done and the
    AI screening rules left no alert for a person, so the case completes with
    no analyst step. System User signs the close; the analyst who confirmed
    the data stays on record as the previous reviewer and keeps the credit."""
    ai = case["screening"].get("ai") or {}
    export = ai.get("export", "")
    if case["screening"]["hit"]:
        _require(ai.get("auto_closed") and export, "The screening case is still open; an analyst has to close it.")
        reason = (f"All {ai['total']} alert(s) auto-resolved by the AI screening rules"
                  f"{' (' + ai['labels'] + ')' if ai.get('labels') else ''}.")
        decision = "False positive"
    else:
        reason, decision = "No screening hit — auto-verified by the AI screening rules.", "No match"
    if export:
        case["attachments"].append({"name": export, "at": now, "by": vocab.SYSTEM_USER, "ref": "auto_verify"})
    _decision(case, "Data Screening", decision=decision, operated_at=now, by=vocab.SYSTEM_USER,
              approve_reason=reason, id_summary=reason, report=export)
    case["status"] = "Verified"
    case["pending_reason"] = ""
    case["closed_at"] = now
    case["closed_by"] = vocab.SYSTEM_USER
    _point(case, "Completed", now)
    _hand_back(case, actor, "Auto-verified")
    _audit(case, vocab.SYSTEM_USER, "Case verified", f"Auto-verified: {reason} Data confirmed by {actor}.")


def resubmit(case, actor, decisions, id_reason, poa_reason, note):
    """Ask the user to upload again. The case leaves the queue; the intake
    SLA clock keeps running."""
    _require(case["status"] == "DataConfirming", "Resubmission can only be requested during Data Confirming.")
    _require_reserved(case, actor)
    request_resubmission(case, actor, decisions, id_reason, poa_reason, note)
    _touch_level(case, "L1", actor)
    _hand_back(case, actor, "Resubmission requested")


def request_resubmission(case, actor, decisions, id_reason, poa_reason, note):
    """The part of Resubmit an analyst and the AI Agent share: record what
    the user must upload again and why, and move the case to Resubmit ID or
    Resubmit POA."""
    _require(case["status"] == "DataConfirming", "Resubmission can only be requested during Data Confirming.")
    decisions = [d for d in (decisions or []) if d in ("Resubmit ID", "Resubmit POA")]
    _require(decisions, "Choose what the user must resubmit.")
    if "Resubmit ID" in decisions:
        _require(id_reason in vocab.RESUBMIT_ID_REASONS, "Pick a Resubmit ID reason.")
    if "Resubmit POA" in decisions:
        _require(poa_reason in sop.resubmit_poa_reasons(), "Pick a Resubmit POA reason.")
    now = dates.now_str()
    prior = (case.get("resubmit") or {}).get("count", 0)
    case["resubmit"] = {"decisions": decisions, "id_reason": id_reason if "Resubmit ID" in decisions else "",
                        "poa_reason": poa_reason if "Resubmit POA" in decisions else "", "note": (note or "").strip(),
                        "at": now, "by": actor, "count": prior + 1}
    case["status"] = "ResubmitID" if "Resubmit ID" in decisions else "ResubmitPOA"
    case["pending_reason"] = "Pending Resubmission"
    _decision(case, "Data Confirming", decision="Resubmit", operated_at=now, by=actor,
              fail_reason=case["resubmit"]["id_reason"], poa_fail_reason=case["resubmit"]["poa_reason"])
    reasons = "; ".join(r for r in (case["resubmit"]["id_reason"], case["resubmit"]["poa_reason"]) if r)
    _audit(case, actor, "Resubmission requested",
           f"{', '.join(decisions)} (#{prior + 1}). {reasons}" + (f" Note: {note.strip()}" if (note or '').strip() else ""))


def escalate(case, actor, reason, comment, fraud_typology="", alert_case=None):
    """Hand the case to L2. A fraud escalation carries its typology (SOP
    4.1.3 §2.1.1: the fraud tracker needs it); the investigation folder opens
    (§6.2); the linked screening case moves to L2; the router gives the case
    to an L2 analyst at once."""
    _require(case["status"] in ("DataConfirming", "DataScreening"),
             "Only a case in Data Confirming or Data Screening can be escalated from L1.")
    _require_reserved(case, actor)
    _require(reason in vocab.ESCALATE_REASONS, "Pick an escalation reason.")
    comment = _require_text(comment, "A comment")
    fraud_typology = (fraud_typology or "").strip()
    if reason.startswith("Fraud"):
        _require(fraud_typology in vocab.FRAUD_TYPOLOGIES,
                 "Pick the fraud typology — it is what the fraud tracker records (SOP 4.1.3 §2.1.1).")
    else:
        _require(not fraud_typology or fraud_typology in vocab.FRAUD_TYPOLOGIES, "Unknown fraud typology.")
    now = dates.now_str()
    case["status"] = "DataEscalatingL2"
    case["pending_reason"] = "Pending L2"
    case["case_review_level"] = "L2"
    case["escalated_at"] = now
    case["edd_due_at"] = dates.add_hours(now, sop.value("sla_days_edd") * 24)
    case["escalate_reason"] = reason
    case["fraud_typology"] = fraud_typology
    _point(case, "Data Escalating L2", now)
    _decision(case, "Data Escalating L2", decision="Escalated", operated_at=now, by=actor, rejection_reason=reason)
    _touch_level(case, "L1", actor)
    _hand_back(case, actor, "Escalated to L2")
    _audit(case, actor, "Escalated to L2", f"{reason}. {comment}" + (f" Fraud typology: {fraud_typology}." if fraud_typology else ""))
    _open_investigation_file(case, actor, now)
    if alert_case is not None and alert_case.get("case_level") != "L2":
        alert_case["case_level"] = "L2"
        alert_case.setdefault("audit", []).append({"at": now, "actor": actor, "action": "Moved to L2",
                                                   "detail": "KYC case escalated; the screening case now sits in the L2 queue."})
    routing.handoff(case, "L1", "L2", exclude=(actor,))


def rfi(case, actor, message):
    _require_open(case)
    _require_reserved(case, actor)
    send_rfi(case, actor, message)


def send_rfi(case, actor, message):
    """The part of RFI an analyst and the AI Agent share. An RFI nobody holds
    (the agent's) waits on the user: out of the queue (routing.level_for)."""
    _require_open(case)
    message = _require_text(message, "The RFI message")
    case["pending_reason"] = "RFI Sent"
    case.setdefault("rfi", []).append({"at": dates.now_str(), "by": actor, "message": message, "status": "Sent"})
    _audit(case, actor, "RFI sent", message)


# ── L1: screening review ─────────────────────────────────────────────────────

def edd_review(case, actor, decision, summary, attachment, alert_case=None, level="L1"):
    """OK on the EDD Review modal, after the screening case is dealt with.
    `level` is the tier the actor is acting at (L1, or L2 for a senior
    analyst working the screening queue) — it is where the close is recorded."""
    _require(case["status"] == "DataScreening", "The screening review is only available while the case is in Data Screening.")
    _require(case.get("pending_reason") != "Pending Checker 2",
             "This business is waiting for Checker 2 — the second reviewer signs it off from the L2 queue.")
    _require_reserved(case, actor)
    summary = _require_text(summary, "A summary")
    hit = case["screening"]["hit"]
    options = list(vocab.EDD_DECISIONS) + (["No match - Approve"] if not hit else [])
    _require(decision in options, "Pick a decision.")
    now = dates.now_str()

    if hit and alert_case is not None and decision in ("False positive", "No match - Approve"):
        pending = [a for a in alert_case["alerts"] if a["outcome"] == "Pending"]
        _require(not pending, f"{len(pending)} screening alert(s) are still pending. Resolve them in the screening case first.")
        _require(alert_case.get("case_outcome"), "Record the screening case outcome before closing the KYC case.")
        _require(attachment, "Attach the World-Check export to the review.")

    if decision in ("False positive", "No match - Approve", "Approve after Skopenow"):
        if decision == "Approve after Skopenow":
            _require((case.get("skopenow") or {}).get("status") == "Cleared",
                     "The Skopenow report has not cleared this person yet.")
        _close(case, actor, "Verified", now, decision, summary, attachment, level=level)
        return
    if decision == "Request Skopenow report":
        _require(case["client"]["us_person"], "Skopenow reports are only requested for US persons (SOP 4.1.3). Escalate instead.")
        case["pending_reason"] = "Pending Skopenow"
        case["skopenow"] = skopenow.request_report(case, actor)
        _touch_level(case, level, actor)
        _audit(case, actor, "Skopenow report requested", summary)
        return
    # True positive / potential match: hand to L2.
    reason = "Unresolved medium or high risk screening hit" if decision.startswith("Potential") else "Other"
    if attachment:
        case["attachments"].append({"name": attachment, "at": now, "by": actor, "ref": "edd_review"})
    _decision(case, "Data Screening", decision=decision, operated_at=now, by=actor, id_summary=summary, report=attachment)
    escalate(case, actor, reason, summary, alert_case=alert_case)


def _close(case, actor, status, now, decision, summary, attachment="", level="L1"):
    """Close the case — or, for a business being approved, park it for
    Checker 2 (SOP 4.2: a second reviewer signs off every business approval,
    after the risk rating has been recalculated)."""
    checker2_gate = status == "Verified" and case["tenant"] == "ttshop" and not case.get("checker2")
    if checker2_gate:
        _require(case.get("risk_recalculated_at"), "Recalculate the risk rating before approving a business (SOP 4.2.2 §5.1.1).")
    if attachment:
        case["attachments"].append({"name": attachment, "at": now, "by": actor, "ref": "edd_review"})
    _decision(case, "Data Screening", decision=decision, operated_at=now, by=actor,
              approve_reason=summary if status == "Verified" else "", id_summary=summary, report=attachment)
    _touch_level(case, level, actor)
    if checker2_gate:
        case["checker1"] = {"by": actor, "at": now, "decision": decision, "summary": summary, "attachment": attachment}
        case["pending_reason"] = "Pending Checker 2"
        _hand_back(case, actor, "Checker 1 approved")
        _audit(case, actor, "Checker 1 approved", f"{decision}. {summary} Awaiting Checker 2 sign-off.")
        routing.handoff(case, "L1", "L2", exclude=(actor,))
        return
    case["status"] = status
    case["pending_reason"] = ""
    case["closed_at"] = now
    case["closed_by"] = actor
    _point(case, "Completed", now)
    _hand_back(case, actor, f"Closed — {status}")
    _audit(case, actor, f"Case {status.lower()}", f"{decision}. {summary}")


def checker2_signoff(case, actor, decision, comment):
    """Business Checker 2: a different analyst approves, returns, or escalates
    what Checker 1 approved."""
    _require(case["status"] == "DataScreening" and case.get("pending_reason") == "Pending Checker 2",
             "This case is not waiting for Checker 2.")
    _require_reserved(case, actor)
    checker1 = case.get("checker1") or {}
    _require(actor != checker1.get("by"), "Checker 2 must be a different analyst from Checker 1 (four-eyes rule).")
    _require(decision in vocab.CHECKER2_DECISIONS, "Pick a decision.")
    comment = _require_text(comment, "A comment")
    now = dates.now_str()
    if decision == "Approve":
        case["checker2"] = {"by": actor, "at": now, "decision": decision, "comment": comment}
        _audit(case, actor, "Checker 2 approved", comment)
        _close(case, actor, "Verified", now, checker1.get("decision") or "False positive",
               checker1.get("summary") or comment, checker1.get("attachment", ""), level="L2")
        return
    _touch_level(case, "L2", actor)
    case["pending_reason"] = ""
    if decision == "Return to Checker 1":
        case["checker1"] = None
        _hand_back(case, actor, "Returned to Checker 1")
        _audit(case, actor, "Returned to Checker 1", comment)
        routing.handoff(case, "L2", "L1", exclude=(actor,))
        return
    escalate(case, actor, "Other", comment)


def skopenow_clear(case, actor, outcome, notes):
    _require((case.get("skopenow") or {}).get("status") == "Requested", "No Skopenow report is outstanding.")
    _require_reserved(case, actor)
    notes = _require_text(notes, "Your review of the report")
    _require(outcome in ("Cleared", "Inconclusive"), "Pick an outcome.")
    case["skopenow"] = skopenow.complete_report(case["skopenow"], outcome, actor, notes, case=case)
    case["pending_reason"] = "" if outcome == "Cleared" else "Pending L2"
    _audit(case, actor, f"Skopenow report {outcome.lower()}", notes)
    if outcome == "Inconclusive":
        escalate(case, actor, "Inconclusive Skopenow report", notes)


# ── L2 / EDD ─────────────────────────────────────────────────────────────────

def l2_decision(case, actor, level, decision, comment, levels=None):
    """Approve / Reject / Escalate to EDD at L2, or Approve / Reject at EDD.
    `levels` (the actor's roles) closes the gap where an L2-only analyst
    could decide at EDD from the case page."""
    _require(case["status"] == "DataEscalatingL2", "Only an escalated case can be decided here.")
    _require(level in ("L2", "EDD"), "Unknown level.")
    if levels is not None:
        _require(level in levels, f"{vocab.LEVEL_LABELS[level]} decisions need the {level} role.")
    # Segregation before reservation: an analyst who touched the case at L1
    # gets that refusal even when the router has since given the case to
    # somebody else.
    segregation.assert_may_decide(case, actor, level)
    _require_reserved(case, actor)
    comment = _require_text(comment, "A comment")
    _require(decision in vocab.L2_DECISIONS, "Pick a decision.")
    if level == "EDD":
        _require(case["pending_reason"] == "Pending EDD", "This case has not been escalated to EDD.")
        _require(decision != "Escalate to EDD", "The case is already at EDD.")
    now = dates.now_str()
    _touch_level(case, level, actor)
    if decision == "Escalate to EDD":
        case["pending_reason"] = "Pending EDD"
        case["case_review_level"] = "L3"
        _decision(case, "Data Escalating L2", decision="Escalated to EDD", operated_at=now, by=actor, rejection_reason=comment)
        _hand_back(case, actor, "Escalated to EDD")
        _audit(case, actor, "Escalated to EDD", comment)
        routing.handoff(case, "L2", "EDD", exclude=(actor,))
        return
    status = "Verified" if decision == "Approve" else "Rejected"
    case["status"] = status
    case["pending_reason"] = ""
    case["closed_at"] = now
    case["closed_by"] = actor
    _point(case, "Completed", now)
    _decision(case, "Data Escalating L2", decision="Approved" if status == "Verified" else "Rejected",
              operated_at=now, by=actor,
              approve_reason=comment if status == "Verified" else "", rejection_reason=comment if status == "Rejected" else "")
    _hand_back(case, actor, f"Closed — {status}")
    _audit(case, actor, f"{level} decision: {decision}", comment)


# ── Businesses: Checker 1 ──────────────────────────────────────────────────────

def recalculate_risk(case, actor):
    """SOP 4.2.2: recalculate the business's risk rating before approving."""
    _require(case["tenant"] == "ttshop", "Risk rating recalculation applies to businesses.")
    score = 0
    reasons = []
    agency = case.get("agency") or {}
    high_risk = set(sop.value("sanctioned_countries"))
    for ubo in agency.get("ubos", []):
        if ubo.get("nationality") in high_risk:
            score += 2
            reasons.append(f"UBO {ubo['name']} ({ubo['nationality']}) is in a high-risk jurisdiction")
        if ubo.get("pct", 0) >= 50 and ubo.get("nationality") in high_risk:
            score += 1
            reasons.append("majority owner in a high-risk jurisdiction")
    if case["client"]["nationality"] in high_risk:
        score += 2
        reasons.append("incorporated in a high-risk jurisdiction")
    if case["screening"]["hit"]:
        score += 1
        reasons.append("open screening hit")
    if case["status"] == "DataEscalatingL2":
        score += 1
        reasons.append("escalated")
    rating = "High" if score >= 3 else ("Medium" if score >= 1 else "Low")
    before = case["risk_rating"]
    case["risk_rating"] = rating
    case["risk_recalculated_at"] = dates.now_str()
    _change(case, actor, "Risk rating", before, rating)
    _audit(case, actor, "Risk rating recalculated", f"{before or 'unrated'} → {rating}. " + ("; ".join(reasons) if reasons else "No risk factors."))
    return rating


def set_pending(case, actor, reason):
    _require(reason in vocab.PENDING_REASONS, "Unknown pending reason.")
    before = case["pending_reason"]
    case["pending_reason"] = reason
    _audit(case, actor, "Pending reason set", f"{before or '-'} → {reason or '-'}")


# ── QC (SOP 2.1) ─────────────────────────────────────────────────────────────

def qc_review(case, actor, result, reason, comment):
    """A QC reviewer samples a closed case: Pass or Fail with a reason. Fail
    parks the feedback on the closing analyst's My Work page."""
    _require(case["status"] in vocab.CLOSED_STATUSES, "QC samples closed cases only.")
    _require(result in ("Pass", "Fail"), "Pick a QC result.")
    _require(reason in vocab.QC_REASONS, "Pick a QC reason.")
    comment = _require_text(comment, "A QC comment")
    touched = {e for lst in (case.get("reviewers_by_level") or {}).values() for e in lst}
    _require(actor not in touched and actor != case.get("closed_by"),
             "You worked this case — another QC reviewer must sample it.")
    now = dates.now_str()
    case.update(qc_reviewer=actor, qc_result=result, qc_reason=reason, qc_at=now, qc_comment=comment, qc_ack=False)
    if result == "Fail":
        case["pending_reason"] = "Pending QC"
    elif case.get("pending_reason") == "Pending QC":
        case["pending_reason"] = ""
    _audit(case, actor, f"QC review: {result}", f"{reason}. {comment}")


def qc_ack(case, actor):
    _require(case.get("qc_result") == "Fail" and not case.get("qc_ack"), "There is no QC feedback to acknowledge on this case.")
    _require(actor == case.get("closed_by"), "Only the analyst who closed the case acknowledges its QC feedback.")
    case["qc_ack"] = True
    if case.get("pending_reason") == "Pending QC":
        case["pending_reason"] = ""
    _audit(case, actor, "QC feedback acknowledged", f"{case.get('qc_reason', '')}.")


# ── small edits ──────────────────────────────────────────────────────────────

def add_tag(case, actor, tag):
    _require(tag in vocab.TAGS, "Unknown tag.")
    if tag not in case["tags"]:
        case["tags"].append(tag)
        _audit(case, actor, "Tag added", tag)


def modify_user_info(case, actor, form):
    """Modify User Info: correct a client field outside the confirm modal."""
    changed = []
    for field, client_key, label, _type in confirm_fields(case):
        value = (form.get(field) or "").strip()
        if value and value != case["client"].get(client_key, ""):
            if field == "dob":
                _require(dates.parse(value) is not None, "Birthday must be a date (YYYY-MM-DD).")
            _change(case, actor, label, case["client"].get(client_key, ""), value)
            case["client"][client_key] = value
            changed.append(label)
    _require(changed, "Nothing changed.")
    _audit(case, actor, "User info modified", ", ".join(changed))


def change_status(case, actor, status, comment):
    _require(status in vocab.STATUSES, "Unknown status.")
    comment = _require_text(comment, "A reason")
    before = case["status"]
    _require(status != before, "The case is already in that status.")
    case["status"] = status
    if status in vocab.CLOSED_STATUSES:
        case["closed_at"] = dates.now_str()
        case["closed_by"] = actor
        _point(case, "Completed", case["closed_at"])
    _change(case, actor, "Status", before, status)
    _audit(case, actor, "Status changed", f"{before} → {status}. {comment}")
