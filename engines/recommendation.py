"""The "Recommended" tag on Change Case Outcome, and the prefill on Change
Alert Outcome. Deterministic rules over what has already been decided, by the
analyst or by the AI screening rules — no model, and this module never
decides anything itself: a Pending alert means no recommendation at all."""
from fincrime.data import vocab

_LABEL = vocab.WC_STATUS_LABEL
_RATIONALE = vocab.WC_REASON_RATIONALE


def recommend_case_outcome(alert, wc=None):
    outcomes = [a["outcome"] for a in alert["alerts"]]
    if not outcomes:
        return {"value": "Approve - False Positive", "why": "No watchlist matches were returned."}
    if "Pending" in outcomes:
        return None
    if "True Positive" in outcomes:
        pep = False
        if wc:
            tp_ids = {a["match_id"] for a in alert["alerts"] if a["outcome"] == "True Positive"}
            pep = any("PEP" in m["types"] or "RCA" in m["types"] for m in wc["matches"] if m["id"] in tp_ids)
        if pep:
            return {"value": "Escalate - True Positive", "why": "A confirmed PEP or close associate needs enhanced due diligence, not a rejection."}
        return {"value": "Reject - True Positive", "why": "A sanctions, law-enforcement or regulatory record was confirmed as this person."}
    if "Potential Match" in outcomes:
        return {"value": "Escalate - Potential Match", "why": "A match could not be cleared at L1."}
    return {"value": "Approve - False Positive", "why": "Every alert was resolved as a false positive."}


def recommend_alert_outcome(alert_row, match):
    if not match:
        return {"result_label": "", "rationale": "", "risk_level": ""}
    res = match["resolution"]
    return {"result_label": _LABEL.get(res["status"], ""), "rationale": _RATIONALE.get(res["reason"], ""),
            "risk_level": res["risk"] or ""}
