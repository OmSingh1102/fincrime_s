"""Model assessment of a watchlist match: Claude, reached through Straive's
LLM Foundry gateway, reads the client record and the watchlist record side
by side and says whether they could be the same person.

Advisory, on demand. The analyst presses "AI assessment" on the CMS
screening case for the ticked alerts (or every pending one); the verdict,
its confidence, the reasons and what is missing are written on the alert
row and on the World-Check match, with an audit row on the screening case,
the watchlist case and the KYC case. Nothing is resolved by it: the analyst
still records the outcome, and the deterministic rules (ai_screening.py)
remain the only thing that clears a record without a person. It never runs
at intake.

Configuration, read once at import (documented in .env.example):
  LLM_FOUNDRY_KEY   the gateway token; unset = the feature is off
  LLM_FOUNDRY_URL   the gateway (default https://llmfoundry.straivedemo.com)
  LLM_MODEL         the Claude model id (default claude-opus-5)
The gateway serves the Anthropic Messages API under /anthropic, so the
official SDK talks to it with that base URL and the token as a bearer token.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from fincrime.data import vocab
from fincrime.engines import ai_screening, worldcheck
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates

GATEWAY = os.environ.get("LLM_FOUNDRY_URL", "https://llmfoundry.straivedemo.com").rstrip("/")
KEY = os.environ.get("LLM_FOUNDRY_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "claude-opus-4-8")   # the strongest Claude this gateway enables (Opus 5 is not)
ACTION = "AI assessment recorded"
VERDICTS = ["different person", "same person", "unclear"]
VERDICT_TONE = {"different person": "emerald", "same person": "rose", "unclear": "amber"}
# What the verdict suggests on the analyst's own labels (the analyst decides).
SUGGESTED = {"different person": "False Positive", "same person": "True Positive", "unclear": "Potential Match"}
PARALLEL = 4            # alerts assessed at once when several are ticked
TIMEOUT = 90.0          # seconds per call

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "integer", "description": "0-100, how sure you are of the verdict"},
        "summary": {"type": "string", "description": "one or two sentences an auditor can read"},
        "reasons": {"type": "array", "items": {"type": "string"}, "description": "short, one fact each"},
        "missing_data": {"type": "array", "items": {"type": "string"},
                         "description": "decisive fields absent on either side"},
        "suggested_outcome": {"type": "string", "enum": vocab.RESULT_LABELS},
    },
    "required": ["verdict", "confidence", "summary", "reasons", "missing_data", "suggested_outcome"],
    "additionalProperties": False,
}

SYSTEM = """You assist a name-screening analyst in a KYC operation. You receive one client record from an onboarding case and one watchlist record that a screening vendor returned as a possible match, with the field-by-field comparison the system already made. Decide whether the watchlist record could be the same person, or the same company, as the client.

Judge only from the data given and never invent facts. A matching or similar name is the reason the record came back and is not, by itself, evidence of identity. A conflict on a hard identifier points to a different person: a full date of birth that differs, a different gender for an individual, a different nationality with nothing suggesting dual nationality. A matching hard identifier, or a matching place and profession on top of the name, points to the same person. When a decisive field is missing on either side, list it under missing_data and lean to "unclear": the analyst must not clear a record on your word when the data is not there. Confidence is a percentage for the verdict you give. suggested_outcome is the analyst's label that follows from the verdict: "different person" is False Positive, "same person" is True Positive, "unclear" is Potential Match. Keep the summary to one or two sentences an auditor can read; each reason is one fact."""

_state = {"client": None, "calls": 0, "last_error": ""}


def configured():
    return bool(KEY)


def switched_on():
    return bool(ai_screening.switches_enabled().get("model_review", True))


def why_not():
    """Why the button is off, or "" when an assessment can run."""
    if not configured():
        return "Model assessment is not configured: set LLM_FOUNDRY_KEY (see .env.example)."
    if not switched_on():
        return "Model assessment is switched off on the AI Screening page."
    return ""


def ready():
    return not why_not()


def status():
    """What the AI Screening page shows."""
    return {"configured": configured(), "on": switched_on(), "model": MODEL, "gateway": urlsplit(GATEWAY).netloc,
            "calls": _state["calls"], "last_error": _state["last_error"]}


def _require(cond, msg):
    if not cond:
        raise WorkflowError(msg)


def _client():
    if _state["client"] is None:
        import anthropic
        _state["client"] = anthropic.Anthropic(base_url=GATEWAY + "/anthropic", auth_token=KEY, api_key=None,
                                               timeout=TIMEOUT, max_retries=1)
    return _state["client"]


def brief(kyc, wc, match):
    """The user turn: both records and the system's own comparison, as JSON."""
    c = kyc["client"]
    client = {k: c.get(k, "") for k in ("legal_name", "native_name", "dob", "gender", "nationality", "country",
                                        "id_type", "address")}
    client["kind"] = "individual" if c.get("gender") in ("M", "F") else "company"   # entities carry no gender
    client["tenant"] = vocab.TENANTS.get(kyc.get("tenant", ""), {}).get("label", kyc.get("tenant", ""))
    key_data, further = match.get("key_data", {}), match.get("further_info", {})
    record = {
        "name": match.get("name"), "matched_alias": match.get("matched_alias"), "aliases": match.get("aliases", []),
        "gender": match.get("gender"), "dates_of_birth": match.get("dobs", []), "place_of_birth": match.get("pob"),
        "citizenship": match.get("citizenship"), "country": match.get("country"), "list_types": match.get("types", []),
        "category": key_data.get("category"), "sub_category": key_data.get("sub_category"),
        "pep_status": key_data.get("pep_status"), "special_interest": key_data.get("special_interest", []),
        "position": key_data.get("position"), "location": key_data.get("locations") or key_data.get("location"),
        "biography": further.get("biography"), "identification": further.get("identification"),
        "reports": further.get("reports", []), "keywords": match.get("keywords", []),
        "name_match_strength": match.get("strength"),
    }
    comparison = [{"field": r["field"], "client": r["submitted"], "record": r["matched"], "system_says_match": r["match"]}
                  for r in worldcheck.comparison_rows(wc, match, c)] if wc else []
    body = {"client": client, "watchlist_record": record, "system_comparison": comparison}
    return json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True, default=str)


def _call_model(text):
    """One request to the gateway; returns (parsed JSON object, usage). Kept
    apart so the tests can stand in for the model."""
    import anthropic
    try:
        response = _client().messages.create(
            model=MODEL, max_tokens=4000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text}],
            thinking={"type": "adaptive"},
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.AuthenticationError:
        raise WorkflowError("The model gateway rejected the key (LLM_FOUNDRY_KEY).")
    except anthropic.APIStatusError as exc:
        raise WorkflowError(f"The model gateway answered {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        raise WorkflowError("Could not reach the model gateway.")
    if response.stop_reason == "refusal":
        raise WorkflowError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise WorkflowError("The model's answer was cut short.")
    answer = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(answer)
    except ValueError:
        raise WorkflowError("The model's answer was not the JSON expected.")
    u = response.usage
    usage = {"input": u.input_tokens, "output": u.output_tokens,
             "cached": getattr(u, "cache_read_input_tokens", 0) or 0}
    return data, usage


def assess(kyc, wc, match, actor):
    """One assessment, validated into the record written on the row."""
    _require(configured(), why_not())
    started = time.monotonic()
    try:
        data, usage = _call_model(brief(kyc, wc, match))
    except WorkflowError as exc:
        _state["last_error"] = str(exc)
        raise
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else "unclear"
    try:
        confidence = max(0, min(100, int(data.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0
    suggested = data.get("suggested_outcome")
    _state["calls"] += 1
    _state["last_error"] = ""
    return {
        "verdict": verdict, "confidence": confidence,
        "summary": str(data.get("summary") or "").strip(),
        "reasons": [str(r) for r in (data.get("reasons") or [])][:8],
        "missing_data": [str(r) for r in (data.get("missing_data") or [])][:8],
        "suggested_outcome": suggested if suggested in vocab.RESULT_LABELS else SUGGESTED[verdict],
        "model": MODEL, "at": dates.now_str(), "by": actor,
        "seconds": round(time.monotonic() - started, 1), "tokens": usage,
    }


def assess_alerts(kyc, alert, wc, actor, ids):
    """Assess the alert rows with these ids, or every pending row when none
    are given, a few at a time. Returns (done, error): the rows assessed and
    the message of the first failure. What was assessed stays."""
    _require(not why_not(), why_not())
    rows = alert["alerts"]
    if ids:
        wanted = set(ids)
        picked = [r for r in rows if r["id"] in wanted]
        _require(len(picked) == len(wanted), "One of the ticked alerts is not on this case.")
    else:
        picked = [r for r in rows if r["outcome"] == "Pending"]
    _require(picked, "Nothing to assess: tick an alert, or leave every alert unticked to assess the pending ones.")
    matches = {m["id"]: m for m in (wc or {}).get("matches", [])}
    missing = [r for r in picked if r["match_id"] not in matches]
    _require(not missing, f"No watchlist record for alert {missing[0]['id']}." if missing else "")

    def one(row):
        try:
            return row, assess(kyc, wc, matches[row["match_id"]], actor), ""
        except WorkflowError as exc:
            return row, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(PARALLEL, len(picked))) as pool:
        results = list(pool.map(one, picked))

    done, error = [], ""
    for row, out, err in results:
        if out is None:
            error = error or err
            continue
        row["ai_assessment"] = out
        matches[row["match_id"]]["ai_assessment"] = out
        detail = (f"{row['name']} (WL {row['watchlist_id']}): {out['verdict']}, {out['confidence']}% — "
                  f"{out['summary']} [{out['model']}, asked by {actor}]")
        for record in (alert, wc, kyc):
            if record is not None:
                record.setdefault("audit", []).append({"at": out["at"], "actor": actor, "action": ACTION, "detail": detail})
        done.append(row)
    if done:
        alert["latest_updated"] = done[-1]["ai_assessment"]["at"]
        if wc is not None:
            wc["last_updated"] = alert["latest_updated"]
    return done, error
