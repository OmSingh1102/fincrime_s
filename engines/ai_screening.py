"""AI screening rules: the preset automation that clears a watchlist match
without an analyst, and the workflow label every KYC case carries as a
result.

This is a deterministic rule book, not a model. The client calls it "AI
automation rules" and the screens use that name; what runs is a short list
of checks a compliance team can read and audit. The rules run twice: at
intake, when the case is first screened (the seed does this while it builds
the case book), and again at the re-screen after Data Confirming, on the
name, date of birth and nationality the analyst confirmed.

Two workflows fall out of it (SOP: current automation and the client's
requirement of 2026-09-24):

    auto-verified        no match on any list, or every alert cleared by the
                         rules. The screening case closes itself, an export
                         is recorded as evidence, and (workflow.auto_verify)
                         an individual's case completes with no analyst step.
    human-in-the-loop    at least one alert the rules could not clear. The
                         analyst handles only what is left.

Rails that are not switchable: a rule only fires when both values are
present; a record that shares the client's exact date of birth always goes
to a person; a resolution an analyst recorded is never changed; the rules
only ever clear a record as a false positive.

Imports vocab, dates and the watchlist console's export helper only, so the
seed can run it while building cases and workflow.py can call it at the
re-screen without a cycle.
"""
from collections import Counter
from datetime import datetime

from fincrime.data import vocab
from fincrime.engines import worldcheck
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates

SYSTEM_USER = vocab.SYSTEM_USER
ACTION = "AI screening rules applied"
NO_HIT_TEXT = "No match on any list — nothing for an analyst."

RULES = [
    {"key": "dob_mismatch", "label": "DOB mismatch", "default": True,
     "checks": "Every date of birth the record lists conflicts with the client's confirmed date of birth. "
               "A bare year conflicts when the year differs; a full date when the date differs.",
     "wc_reason": "Date of Birth mismatch", "rationale": "DOB Mismatch"},
    {"key": "gender_mismatch", "label": "Gender mismatch", "default": True,
     "checks": "The record's gender differs from the client's. Never applied to a company, which has none.",
     "wc_reason": "Gender mismatch", "rationale": "Gender Mismatch"},
    {"key": "nationality_mismatch", "label": "Nationality mismatch", "default": False,
     "checks": "The citizenship the record lists does not include the client's nationality. "
               "Off by default: naturalised and dual nationals exist.",
     "wc_reason": "Nationality mismatch", "rationale": "Nationality Mismatch"},
]
GUARDS = [
    {"key": "san_review", "label": "Sanctions records always go to an analyst", "default": False,
     "checks": "When on, a record from a sanctions list (type SAN) is never auto-resolved, whatever the rules say."},
]
# Assistance that is switchable but resolves nothing by itself.
SWITCHES = [
    {"key": "model_review", "label": "Model assessment", "default": True,
     "checks": "The AI assessment button on a screening case: Claude, through Straive's LLM Foundry, reads the client "
               "and the watchlist record side by side and gives a verdict with its reasons. Advisory only: the analyst "
               "still records every outcome."},
]
RAILS = [
    "A rule only fires when both values are present. Missing data always goes to an analyst.",
    "A record that shares the client's exact date of birth always goes to an analyst, whatever else differs.",
    "A resolution an analyst recorded is never changed. The rules act only on unresolved records.",
    "The rules only ever clear a record as a false positive. A true or potential match is always a person's decision.",
]
RULE_BY_KEY = {r["key"]: r for r in RULES}
GUARD_BY_KEY = {g["key"]: g for g in GUARDS}
KEY_BY_LABEL = {r["label"]: r["key"] for r in RULES}
DEFAULT_ENABLED = {r["key"]: r["default"] for r in RULES}
DEFAULT_GUARDS = {g["key"]: g["default"] for g in GUARDS}
SWITCH_BY_KEY = {s["key"]: s for s in SWITCHES}
DEFAULT_SWITCHES = {s["key"]: s["default"] for s in SWITCHES}

_BOUND = {"settings": None}


# ── settings ─────────────────────────────────────────────────────────────────

def default_settings():
    """The stored record: one row of kind "settings", id "ai_rules"."""
    return {"id": "ai_rules", "enabled": dict(DEFAULT_ENABLED), "guards": dict(DEFAULT_GUARDS),
            "switches": dict(DEFAULT_SWITCHES), "history": []}


def configure(record):
    """Bind the live settings dict (app.py rebuilds it in place on reset)."""
    _BOUND["settings"] = record


def settings():
    return _BOUND["settings"] if _BOUND["settings"] is not None else default_settings()


def rules_enabled():
    """The live switches (the keyword arguments of evaluate/apply override
    them, which is how the seed screens with the defaults)."""
    return settings().get("enabled", DEFAULT_ENABLED)


def guards_enabled():
    return settings().get("guards", DEFAULT_GUARDS)


def switches_enabled():
    return settings().get("switches", DEFAULT_SWITCHES)


def set_rule(record, actor, key, on):
    """Switch a rule or guard; returns (before, after). The change is logged
    on the record so the page can show who changed what."""
    on = str(on).strip().lower() in ("1", "true", "on", "yes")
    if key in RULE_BY_KEY:
        table, label = record.setdefault("enabled", dict(DEFAULT_ENABLED)), RULE_BY_KEY[key]["label"]
    elif key in GUARD_BY_KEY:
        table, label = record.setdefault("guards", dict(DEFAULT_GUARDS)), GUARD_BY_KEY[key]["label"]
    elif key in SWITCH_BY_KEY:
        table, label = record.setdefault("switches", dict(DEFAULT_SWITCHES)), SWITCH_BY_KEY[key]["label"]
    else:
        raise WorkflowError("Unknown rule.")
    before = bool(table.get(key))
    table[key] = on
    record.setdefault("history", []).append({"at": dates.now_str(), "by": actor, "key": key, "label": label,
                                             "from": before, "to": on})
    return before, on


# ── the rules ────────────────────────────────────────────────────────────────

def _client_dob(text):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _listed_dob(text):
    """(year, date | None) for one date the record lists, else None."""
    t = (text or "").strip()
    if len(t) == 4 and t.isdigit():
        return int(t), None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(t, fmt)
            return d.year, d.date()
        except ValueError:
            continue
    return None


def _swapped(full, cd):
    """The client's own date with the day and the month exchanged — a
    keying slip between dd/mm and mm/dd, not a different birthday."""
    return full.year == cd.year and full.month == cd.day and full.day == cd.month


def dob_state(client_dob, dobs):
    """"conflict" when every listed date of birth contradicts the client's,
    "same" when one is the client's exact date, else "unknown" (no data, an
    unreadable value, a bare year that matches the client's year, or the
    client's date with day and month transposed — the reconciliation log
    holds 22 of those and 14 were the client)."""
    cd = _client_dob(client_dob)
    if cd is None or not dobs:
        return "unknown"
    verdicts = []
    for raw in dobs:
        parts = _listed_dob(raw)
        if parts is None:
            return "unknown"
        year, full = parts
        if full is not None:
            if full == cd:
                return "same"
            verdicts.append("possible" if _swapped(full, cd) else "conflict")
        else:
            verdicts.append("conflict" if year != cd.year else "possible")
    return "conflict" if verdicts and all(v == "conflict" for v in verdicts) else "unknown"


def _client_gender(client):
    return {"M": "Male", "F": "Female"}.get((client.get("gender") or "").strip().upper(), "")


def _pretty(client_dob):
    d = _client_dob(client_dob)
    return d.strftime("%d-%b-%Y") if d else client_dob


def evaluate(match, client, *, enabled=None, guards=None):
    """(rule key, detail) for the first rule that clears this record, or
    ("", "") when it stays with an analyst."""
    if match.get("resolution", {}).get("status", "Unresolved") != "Unresolved":
        return "", ""
    en = rules_enabled() if enabled is None else enabled
    gd = guards_enabled() if guards is None else guards
    if gd.get("san_review") and "SAN" in (match.get("types") or []):
        return "", ""
    state = dob_state(client.get("dob", ""), match.get("dobs") or [])
    if state == "same":
        return "", ""
    if en.get("dob_mismatch") and state == "conflict":
        return "dob_mismatch", f"listed {', '.join(match['dobs'])}; client {_pretty(client.get('dob', ''))}"
    if en.get("gender_mismatch"):
        cg, mg = _client_gender(client), (match.get("gender") or "").strip().title()
        if cg and mg in ("Male", "Female") and cg != mg:
            return "gender_mismatch", f"record {mg}; client {cg}"
    if en.get("nationality_mismatch"):
        name = vocab.COUNTRY_NAMES.get((client.get("nationality") or "").upper(), "")
        cit = (match.get("citizenship") or "").strip()
        if name and cit and name.lower() not in cit.lower():
            return "nationality_mismatch", f"record {cit}; client {name}"
    return "", ""


def review_results(match, client, *, enabled=None, guards=None):
    """Each rule's verdict on one record, for the screening case's Smart
    Review Results: [{label, verdict, detail}], verdict one of "fires",
    "does not fire", "off" or "vetoed". The same tests as evaluate(), said
    out loud; it decides nothing."""
    en = rules_enabled() if enabled is None else enabled
    gd = guards_enabled() if guards is None else guards
    listed = ", ".join(match.get("dobs") or []) or "none"
    state = dob_state(client.get("dob", ""), match.get("dobs") or [])
    out = []
    if gd.get("san_review") and "SAN" in (match.get("types") or []):
        out.append({"label": GUARD_BY_KEY["san_review"]["label"], "verdict": "vetoed",
                    "detail": "A sanctions record: no rule clears it, an analyst decides."})
    if state == "same":
        dob = ("vetoed", f"The record lists the client's exact date of birth ({_pretty(client.get('dob', ''))}): "
                         "always a person's decision, whatever else differs.")
    elif state == "conflict":
        dob = ("fires", f"Every date the record lists conflicts with the client's: listed {listed}; client {_pretty(client.get('dob', ''))}.")
    else:
        dob = ("does not fire", f"The listed date of birth ({listed}) does not rule the client out "
                                f"(client {_pretty(client.get('dob', '')) or 'unknown'}): a year that matches, a range, "
                                "day and month swapped, or no date at all.")
    cg, mg = _client_gender(client), (match.get("gender") or "").strip().title()
    if not (cg and mg in ("Male", "Female")):
        gender = ("does not fire", f"Both genders are needed: record {mg or 'not listed'}; client {cg or 'not known'}.")
    else:
        gender = ("fires", f"Record {mg}; client {cg}.") if cg != mg else ("does not fire", f"The same gender: {cg}.")
    name = vocab.COUNTRY_NAMES.get((client.get("nationality") or "").upper(), "")
    cit = (match.get("citizenship") or "").strip()
    if not (name and cit):
        nat = ("does not fire", f"Both are needed: record {cit or 'not listed'}; client {name or 'not known'}.")
    elif name.lower() not in cit.lower():
        nat = ("fires", f"Record {cit}; client {name}.")
    else:
        nat = ("does not fire", f"The record's citizenship includes the client's nationality ({name}).")
    veto = bool(out) or state == "same"            # the sanctions guard, or the exact date of birth, stops every rule
    for key, (verdict, detail) in (("dob_mismatch", dob), ("gender_mismatch", gender), ("nationality_mismatch", nat)):
        if not en.get(key) and verdict != "vetoed":
            verdict, detail = "off", "Switched off on the AI Screening page. " + RULE_BY_KEY[key]["checks"]
        elif veto and verdict == "fires":
            verdict, detail = "vetoed", detail + " It would fire, but the record goes to an analyst all the same."
        out.append({"label": RULE_BY_KEY[key]["label"], "verdict": verdict, "detail": detail})
    return out


# ── the workflow label ───────────────────────────────────────────────────────

def classify(kyc, alert):
    """auto-verified: no hit, or a screening case the rules closed on their
    own. Anything a person touched, or still has to, is human-in-the-loop."""
    if not kyc["screening"]["hit"]:
        return "auto-verified"
    if alert is None:
        return kyc["screening"].get("workflow") or "human-in-the-loop"
    outcome = alert.get("case_outcome") or {}
    if alert["status"] == "Closed" and outcome.get("by") == SYSTEM_USER:
        return "auto-verified"
    return "human-in-the-loop"


WAITING = "waiting-on-customer"
WAITING_REASONS = ("RFI Sent", "Pending Resubmission")


def shown_workflow(kyc):
    """The workflow label the console shows and counts. Auto-verified means
    the case is closed and nothing more is asked of the customer (the
    client's definition, 2026-10-25): a case the rules cleared that is still
    open, waiting on the customer's reply to an RFI or a resubmission, shows
    as waiting on the customer instead. The stored label (classify) is the
    screening's, and it is what the engines read."""
    workflow = kyc["screening"].get("workflow", "")
    if workflow == "auto-verified" and kyc["status"] not in vocab.CLOSED_STATUSES and (
            kyc.get("pending_reason") in WAITING_REASONS or kyc["status"].startswith("Resubmit")):
        return WAITING
    return workflow


def _counts_text(counter):
    return ", ".join(f"{label} ×{n}" for label, n in counter.most_common()) or "no records"


def stats(kyc, alert, *, run="", at="", resolved=0):
    """What the rules have done on this case, computed from the records as
    they stand (so it never reads "0 of 1" after a re-screen that found
    nothing new)."""
    rows = list(alert["alerts"]) if alert is not None else []
    auto = [r for r in rows if r.get("resolved_by") == SYSTEM_USER]
    pending = [r for r in rows if r.get("outcome") == "Pending"]
    labels = Counter(r.get("smart_strategy") or "rule" for r in auto)
    outcome = (alert or {}).get("case_outcome") or {}
    auto_closed = bool(alert is not None and alert["status"] == "Closed" and outcome.get("by") == SYSTEM_USER)
    return {
        "total": len(rows), "auto_resolved": len(auto), "for_review": len(pending),
        "rules": {KEY_BY_LABEL.get(label, label): n for label, n in labels.items()},
        "labels": _counts_text(labels) if auto else "",
        "auto_closed": auto_closed, "export": outcome.get("attachment", "") if auto_closed else "",
        "last_run": {"run": run, "at": at, "resolved": resolved},
    }


def no_hit_stats(at):
    return {"total": 0, "auto_resolved": 0, "for_review": 0, "rules": {}, "labels": "", "auto_closed": False,
            "export": "", "last_run": {"run": "intake", "at": at, "resolved": 0}}


def summary(kyc):
    """One line for the case page and the audit trail."""
    if not kyc["screening"]["hit"]:
        return NO_HIT_TEXT
    ai = kyc["screening"].get("ai") or {}
    if not ai.get("total"):
        return "Screening hit; the AI screening rules have not run on this case."
    text = (f"{ai['auto_resolved']} of {ai['total']} match(es) auto-resolved by the AI screening rules"
            f"{' (' + ai['labels'] + ')' if ai.get('labels') else ''}; {ai['for_review']} left for review.")
    if ai.get("auto_closed"):
        text += " The screening case closed itself."
    return text


# ── running the rules ────────────────────────────────────────────────────────

def _audit(record, at, action, detail):
    record.setdefault("audit", []).append({"at": at, "actor": SYSTEM_USER, "action": action, "detail": detail})


def _maybe_close(alert, wc, at):
    """Close a screening case nobody needs to touch: every alert cleared by
    the rules, none pending, no analyst ever handled it. Records the export
    first so the evidence file exists before the outcome names it."""
    rows = alert["alerts"]
    if alert["status"] == "Closed" or alert.get("assigned_to"):
        return None
    if any(r["outcome"] == "Pending" for r in rows) or any(r.get("resolved_by") != SYSTEM_USER for r in rows):
        return None
    export = worldcheck.record_export(wc, SYSTEM_USER, at=at)
    labels = Counter(r.get("smart_strategy") or "rule" for r in rows)
    alert["case_outcome"] = {"value": "Approve - False Positive", "rationale": "All alerts resolved as false positive",
                             "comment": f"Every alert was auto-resolved by the AI screening rules ({_counts_text(labels)}).",
                             "by": SYSTEM_USER, "at": at, "attachment": export["filename"]}
    alert["status"] = "Closed"
    alert["latest_updated"] = at
    _audit(alert, at, "Case outcome", "Approve - False Positive")
    return export


def apply(kyc, alert, wc, *, at, run="intake", enabled=None, guards=None):
    """Run the rules over the unresolved records, sync the screening case's
    alert rows, close the screening case when nothing is left for a person,
    and stamp the KYC case with its workflow. Returns the stats dict."""
    fired = Counter()
    if alert is not None and wc is not None and kyc["screening"]["hit"]:
        client = kyc["client"]
        by_match = {a["match_id"]: a for a in alert["alerts"]}
        for m in wc["matches"]:
            key, detail = evaluate(m, client, enabled=enabled, guards=guards)
            if not key:
                continue
            rule = RULE_BY_KEY[key]
            comment = f"Auto-resolved by AI screening rule: {rule['label']} ({detail})."
            m["resolution"] = {"status": "False positive", "risk": "Low", "reason": rule["wc_reason"],
                               "remark": rule["wc_reason"], "comment": comment, "by": SYSTEM_USER, "at": at}
            row = by_match.get(m["id"])
            if row is not None:
                row.update(outcome="False Positive", review_mode="Smart Review", result_label="False Positive",
                           rationale=rule["rationale"], comment=comment, risk_level="Low",
                           resolved_by=SYSTEM_USER, resolved_at=at, smart_strategy=rule["label"])
            fired[rule["label"]] += 1
        if fired:
            wc["last_updated"] = at
            alert["latest_updated"] = at
        export = _maybe_close(alert, wc, at) if (fired or not alert["alerts"]) else None
        if fired or export:
            total, pending = len(alert["alerts"]), sum(1 for r in alert["alerts"] if r["outcome"] == "Pending")
            when = "at intake" if run == "intake" else "at the re-screen after Data Confirming"
            detail = f"{sum(fired.values())} of {total} match(es) auto-resolved {when} ({_counts_text(fired)}); {pending} left for review."
            if export:
                detail += f" Screening case closed: Approve - False Positive; export {export['filename']} recorded."
            for record in (wc, alert, kyc):
                _audit(record, at, ACTION, detail)
    result = stats(kyc, alert, run=run, at=at, resolved=sum(fired.values()))
    kyc["screening"]["ai"] = result
    kyc["screening"]["workflow"] = classify(kyc, alert)
    if run == "intake":
        _system_review(kyc, at, result)
    return result


def _system_review(kyc, at, result):
    """The "System Review" check point, empty in the real tool, records the
    rules' verdict at intake."""
    row = next((r for r in kyc["review_decisions"] if r["check_point"] == "System Review"), None)
    if row is None:
        return
    if not kyc["screening"]["hit"]:
        text = NO_HIT_TEXT
    else:
        referred = result["total"] - result["auto_resolved"]
        text = (f"{result['auto_resolved']} of {result['total']} match(es) cleared by the AI screening rules"
                f"{' (' + result['labels'] + ')' if result.get('labels') else ''}; {referred} referred to an analyst.")
    row.update(decision="AI screening rules", approve_reason=text, operated_at=at, by=SYSTEM_USER)


def label_no_hit(kyc, at):
    """A case with no match on any list: auto-verified from the start."""
    kyc["screening"]["workflow"] = "auto-verified"
    kyc["screening"]["ai"] = no_hit_stats(at)
    _system_review(kyc, at, kyc["screening"]["ai"])
