"""The name-screening case: handle it, disposition each alert, record the
case outcome. Mirrors the KYC workflow's rules — every outcome needs the
analyst's own comment, and everything lands in the case's audit trail."""
from fincrime.data import vocab
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates


def _audit(record, actor, action, detail=""):
    record.setdefault("audit", []).append({"at": dates.now_str(), "actor": actor, "action": action, "detail": detail})


def _require(cond, msg):
    if not cond:
        raise WorkflowError(msg)


def _require_text(value, what):
    if not (value or "").strip():
        raise WorkflowError(f"{what} is required.")
    return value.strip()


def tab_counts(alerts):
    counts = {"All": len(alerts)}
    for o in vocab.ALERT_OUTCOMES:
        counts[o] = sum(1 for a in alerts if a["outcome"] == o)
    return counts


def handle(alert, actor):
    _require(alert["status"] != "Closed", "This screening case is closed.")
    _require(not alert["assigned_to"] or alert["assigned_to"] == actor,
             f"Already being handled by {alert['assigned_to']}.")
    alert["status"] = "Handling"
    alert["assigned_to"] = actor
    alert["latest_updated"] = dates.now_str()
    _audit(alert, actor, "Case handled", "Analyst took the case.")


def _require_handler(alert, actor):
    _require(alert["status"] != "Closed", "This screening case is closed.")
    _require(alert["assigned_to"] == actor, "Handle the case first — only its handler can change outcomes.")


def alert_outcome(alert, actor, form):
    """One outcome written to every ticked alert: the form carries one
    alert_id per ticked row (a single id still works). Everything is checked
    before anything is written, so a refusal leaves no alert half-done."""
    _require_handler(alert, actor)
    ids = [i for i in (form.getlist("alert_id") if hasattr(form, "getlist") else [form.get("alert_id", "")]) if i]
    _require(ids, "Tick at least one alert first.")
    by_id = {a["id"]: a for a in alert["alerts"]}
    picked = [by_id.get(i) for i in ids]
    _require(all(r is not None for r in picked), "One of the ticked alerts is not on this case.")
    review_mode = form.get("review_mode", "Manual Review")
    _require(review_mode in vocab.REVIEW_MODES, "Pick a review mode.")
    label = form.get("result_label", "")
    _require(label in vocab.RESULT_LABELS, "Pick a result label.")
    rationale = form.get("rationale", "")
    _require(rationale in vocab.RATIONALES, "Pick a rationale.")
    comment = _require_text(form.get("comment", ""), "A comment")
    risk = form.get("risk_level", "")
    _require(risk in vocab.RISK_LEVELS, "Pick a risk level.")
    now = dates.now_str()
    for row in picked:
        row.update({"outcome": label, "review_mode": review_mode, "result_label": label, "rationale": rationale,
                    "comment": comment, "risk_level": risk, "attachment": (form.get("attachment") or "").strip(),
                    "resolved_by": actor, "resolved_at": now})
        _audit(alert, actor, f"Alert outcome: {label}", f"{row['name']} (WL {row['watchlist_id']}) — {rationale}, {risk} risk. {comment}")
    alert["latest_updated"] = now
    return picked


def case_outcome(alert, actor, form, kyc=None):
    _require_handler(alert, actor)
    value = form.get("outcome", "")
    _require(value in vocab.CASE_OUTCOMES, "Pick a case outcome.")
    pending = [a for a in alert["alerts"] if a["outcome"] == "Pending"]
    _require(not pending, f"{len(pending)} alert(s) are still pending. Resolve every alert before the case outcome.")
    rationale = form.get("rationale", "")
    comment = _require_text(form.get("comment", ""), "A comment")
    now = dates.now_str()
    alert["case_outcome"] = {"value": value, "rationale": rationale, "comment": comment, "by": actor, "at": now,
                             "attachment": (form.get("attachment") or "").strip()}
    alert["status"] = "Closed"
    alert["latest_updated"] = now
    _audit(alert, actor, "Case outcome", f"{value}. {comment}")
    if kyc is not None:
        kyc.setdefault("audit", []).append({"at": now, "actor": actor, "action": "Screening case outcome",
                                            "detail": f"{value}. {comment}"})
