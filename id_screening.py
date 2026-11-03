"""Screening an uploaded ID: the person the ID names, searched on World-Check,
and each hit decided by a language model that follows the SOP. No screening
rule is written in code.

  1. Read. A vision model reads the ID (documents/id_scan).
  2. Search. The name, with the date of birth, sex and nationality, goes to
     World-Check One by its API once a key and secret are entered on the
     Settings page (adapters/world_check); until then to the stand-in
     watchlist, the World-Check records of the reconciliation log's 1,000
     cases (fincrime/watchlist). Either way the search only finds candidates.
  3. Decide. For each hit a model gets the SOP (the current version of the
     SOP page: the files' v1 to v3, then any version saved in the console),
     the client's variance reference, and the resolved cases of the log most
     like this one with what the analysts decided, and answers as the
     decision model of the Model evaluation page does: clear, true match, ask
     the customer, escalate; with the name and address discrepancy and its
     reasons. The resolved case behind the very record that was hit is never
     one of the examples (it would hand the model the analysts' answer).
  4. An analyst confirms each hit (true match, possible match, false
     positive). The case closes when every hit is confirmed, or, with no hit,
     when the analyst confirms there is none.

Saving a new SOP version sends every open case's unconfirmed hits to the
model again under it (app.py). The models, the number of examples, the name
score below which a name is not a candidate, and the World-Check account are
settings (kind "settings", id "screening"), not code.
"""
import hashlib
import time

from fincrime import benchmark, decision_model, watchlist
from fincrime.adapters import azure_docintel, didit, world_check
from fincrime.documents import id_scan
from fincrime.filebook import llm, loader, similarity
from fincrime.utils import dates

SETTINGS_ID = "screening"
SOP_KIND = "screen_sop"
RESOLUTIONS = ("True match", "Possible match", "False positive")
SUGGESTION = {"clear": "False positive", "true_match": "True match", "request_documents": "Possible match",
              "escalate": "Possible match"}
DECISION_MODELS = ("claude-opus-4-8", "claude-sonnet-5-5", "gpt-5", "gemini-2.5-pro", "gemini-2.5-flash", "claude-haiku-4-5",
                   "gpt-5-mini")


# ── settings ─────────────────────────────────────────────────────────────────

def default_settings():
    return {"id": SETTINGS_ID, "reader_model": id_scan.DEFAULT_MODEL, "decision_model": "claude-opus-4-8", "examples": 6,
            "name_threshold": 65, "max_hits": 5, "world_check": dict(world_check.DEFAULTS),
            # auto-verify (auto_verify below): which cases the AI closes without an analyst
            "auto_verify": True, "auto_no_hit": True, "auto_min_confidence": 90, "auto_dob_differs": True,
            "auto_person_lists": "Sanctions, PEP", "auto_qc_pct": 5,
            # the services connected by API (adapters/): reading the ID, verifying it; the client's system sending IDs
            "azure": dict(azure_docintel.DEFAULTS), "didit": dict(didit.DEFAULTS), "intake_keys": []}


def settings_from(stored):
    """The defaults with the stored record laid on top."""
    s = default_settings()
    stored = stored or {}
    s.update({k: v for k, v in stored.items() if k not in ("world_check", "azure", "didit")})
    for service in ("world_check", "azure", "didit"):
        s[service].update(stored.get(service) or {})
    return s


def decision_models():
    by_id = {m["id"]: m for m in benchmark.MODELS}
    return [by_id[i] for i in DECISION_MODELS if i in by_id]


def live(settings):
    return world_check.configured(settings["world_check"])


def source_label(settings):
    return ("World-Check One (live, by its API)" if live(settings)
            else "Stand-in World-Check: the World-Check records of the reconciliation log's 1,000 cases")


# ── the person on the ID ─────────────────────────────────────────────────────

def person(read):
    f = (read or {}).get("fields") or {}
    name = f.get("full_name") or " ".join(x for x in (f.get("given_names"), f.get("surname")) if x)
    mrz = (read or {}).get("mrz") or []
    return {"name": name.strip(), "surname": f.get("surname", ""), "given_names": f.get("given_names", ""),
            "dob": f.get("date_of_birth", ""), "sex": f.get("sex", ""), "nationality": f.get("nationality", ""),
            "issuing_country": (read or {}).get("issuing_country", ""), "place_of_birth": f.get("place_of_birth", ""),
            "address": f.get("address", ""), "father_or_spouse_name": f.get("father_or_spouse_name", ""),
            "document_type": (read or {}).get("document_type", ""), "document_number": f.get("document_number", ""),
            "mrz1": mrz[0] if mrz else ""}


def search(who, settings):
    """(hits, source, error, case_system_id): the candidates for the person."""
    if live(settings):
        try:
            hits, csid = world_check.screen(settings["world_check"], who, int(settings["max_hits"]))
            return hits, "World-Check One", "", csid
        except world_check.WorldCheckError as exc:
            return [], "World-Check One", str(exc), ""
    return (watchlist.search(who, int(settings["name_threshold"]), int(settings["max_hits"])), "stand-in", "", "")


# ── the SOP ──────────────────────────────────────────────────────────────────

def sop_versions(stored):
    """Every SOP version, oldest first: the files', then those saved in the
    console (StateStore kind "screen_sop")."""
    out = [{"version": v, "text": decision_model.sop_text(v), "changes": decision_model.sop_changes(v), "by": "",
            "at": "", "source": "file"} for v in decision_model.sop_versions()]
    out += sorted((dict(r, source="console") for r in stored or []), key=lambda r: int(r["version"][1:]))
    return out


def sop_current(stored):
    return sop_versions(stored)[-1]


def sop_new(stored, text, note, actor):
    """A new version record, numbered after the last one."""
    text = (text or "").strip()
    if len(text) < 200:
        raise ValueError("The SOP is too short to decide a case from: keep the whole procedure.")
    current = sop_current(stored)
    if text == current["text"].strip():
        raise ValueError("Nothing changed: the text is the current version's.")
    number = max(int(v["version"][1:]) for v in sop_versions(stored)) + 1
    return {"id": f"v{number}", "version": f"v{number}", "text": text, "changes": [note.strip()] if note.strip() else [],
            "by": actor, "at": dates.now_str(), "from": current["version"]}


# ── what the model sees ──────────────────────────────────────────────────────

def facts(who, hit):
    """The case as the model starts it: the uploaded ID and the hit."""
    lines = [
        f"Customer, screened from an uploaded ID (no CRM record and no other document): {who['name']} (Individual)",
        f"  date of birth: {who['dob'] or 'not on the document'}",
        f"  sex: {who['sex'] or 'not printed'}; nationality: {who['nationality'] or 'not printed'}; "
        f"place of birth: {who['place_of_birth'] or 'not printed'}",
        f"  address on the ID: {who['address'] or 'none printed'}",
    ]
    if who.get("father_or_spouse_name"):
        lines.append(f"  father's / spouse's name: {who['father_or_spouse_name']}")
    lines += [
        f"{who['document_type'] or 'ID document'} ({who['issuing_country'] or 'country not read'}, no. {who['document_number'] or 'not read'})",
        f"  name as printed: surname {who['surname'] or '-'} | given names {who['given_names'] or '-'}",
        f"  MRZ line 1: {who['mrz1'] or 'none'}",
        "Secondary ID: not provided",
        "Proof of address: not provided",
        f"World-Check record {hit['uid']}: {hit['name']}" + (f" (AKA: {', '.join(hit['aka'])})" if hit.get("aka") else ""),
        f"  listed date of birth: {hit['dob_text']}",
        f"  listed places: {hit['places']}",
        f"  category: {hit['category']}; name match {hit['strength']} ({hit['score']}%)",
        f"  trigger: {hit['narrative']}",
    ]
    return "\n".join(lines)


_FILE_COUNTRY = {"INDIA": "India", "VIETNAM": "Vietnam", "UNITED STATES": "United States"}


def _pseudo_row(key, who, hit):
    """The scanned case in the log's columns, so the examples can be chosen
    the way the decision model chooses them (decision_model.signals)."""
    src = watchlist.row_of(hit["uid"]) if hit.get("source") == "stand-in" else None
    country = watchlist._country_key(who["issuing_country"]) or watchlist._country_key(who["nationality"])
    row = {k: "" for k in loader.read_rows()[0]}
    row.update({"kyc_case_id": key, "customer_name": who["name"], "customer_type": "Individual",
                "customer_provided_dob": who["dob"], "customer_registered_address": who["address"],
                "address_passport": who["address"], "passport_issuing_country": _FILE_COUNTRY.get(country, who["issuing_country"]),
                "match_score": f"{hit['score']}%"})
    if src:
        row.update({k: src[k] for k in src if k.startswith("world_check_") or k == "watchlist_category"})
    else:
        row.update({"world_check_uid": hit["uid"], "world_check_entity_subject_name": hit["name"],
                    "world_check_listed_dob": hit["dob_text"], "world_check_listed_address_jurisdiction": hit["places"],
                    "watchlist_category": hit["category"], "world_check_narrative_trigger_details": hit["narrative"]})
    return row


def examples(case_id, who, hit, k):
    """The k resolved cases of the log most like this one (same weighting as
    the decision model's, without its name rules), never the case whose
    World-Check record this hit is."""
    if k <= 0:
        return []
    me = decision_model.signals(_pseudo_row(f"{case_id}:{hit['uid']}", who, hit))
    scored = []
    for other in decision_model.rows().values():
        if other["world_check_uid"] == hit["uid"]:
            continue
        o = decision_model.signals(other)
        w = (3 * (o["date of birth"] == me["date of birth"]) + 3 * (o["country"] == me["country"])
             + (o["place"] == me["place"]) + (o["list"] == me["list"]) + (o["customer"] == me["customer"]))
        tie = hashlib.sha1((case_id + hit["uid"] + other["kyc_case_id"]).encode("utf-8")).hexdigest()
        scored.append((-w, tie, other))
    return [o for _, _, o in sorted(scored, key=lambda t: t[:2])[:k]]


def prompt(case_id, who, hit, k):
    rows = examples(case_id, who, hit, k)
    parts = []
    if rows:
        parts.append("Resolved cases like this one, with what the analysts decided:\n\n"
                     + "\n\n".join(f"### Example {i}\n{decision_model.example(o)}" for i, o in enumerate(rows, 1)))
    parts.append("### The case to decide\n" + facts(who, hit))
    return "\n\n".join(parts), [o["kyc_case_id"] for o in rows]


def decide(case_id, who, hit, sop, settings):
    """The model's answer on one hit under one SOP version (an "ai" record);
    status "failed" with the reason when there is no answer."""
    model = next((m for m in decision_models() if m["id"] == settings["decision_model"]), decision_models()[0])
    base = {"model": model["id"], "model_label": model["label"], "sop": sop["version"], "at": dates.now_str()}
    if not llm.KEY:
        return dict(base, status="failed", error="No model is configured (LLM_FOUNDRY_KEY): decide this hit yourself.")
    text, used = prompt(case_id, who, hit, int(settings["examples"]))
    started = time.monotonic()
    try:
        ans, t_in, t_out = benchmark.call(model, [{"type": "text", "text": text}], decision_model.instructions(sop["text"]),
                                          decision_model.schema())
    except Exception as exc:                        # the page offers "Ask the AI again"
        return dict(base, status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}",
                    seconds=round(time.monotonic() - started, 1))
    ref = decision_model.reference()
    outcome = decision_model._closest(ans.get("outcome"), decision_model.OUTCOME_KEYS)
    if outcome not in decision_model.OUTCOME_KEYS:
        return dict(base, status="failed", error=f"The model answered {outcome!r}, not one of the four outcomes.")
    price_in, price_out = model["price"]
    return dict(base, status="done", outcome=outcome, label=decision_model.OUTCOME_LABEL[outcome], suggested=SUGGESTION[outcome],
                confidence=ans.get("confidence"), reasons=[str(x)[:300] for x in (ans.get("reasons") or [])][:4],
                dob_finding=str(ans.get("dob_finding", ""))[:300], request=str(ans.get("request", ""))[:300],
                name_variance=decision_model._closest(ans.get("name_variance"), ref["name"]),
                address_variance=decision_model._closest(ans.get("address_variance"), ref["address"]),
                examples=used, tokens_in=t_in, tokens_out=t_out, cost=round((t_in * price_in + t_out * price_out) / 1e6, 4),
                seconds=round(time.monotonic() - started, 1))


# ── the case ─────────────────────────────────────────────────────────────────

def open_hits(case):
    """The hits of an open case that no analyst has confirmed yet."""
    if case.get("status") == "closed":
        return []
    return [h for h in ((case.get("screening") or {}).get("hits") or []) if not h.get("resolution")]


def outcome(case):
    """The case's outcome from its confirmed hits."""
    hits = (case.get("screening") or {}).get("hits") or []
    if not hits:
        return "No match"
    states = {(h.get("resolution") or {}).get("status") for h in hits}
    return "True match" if "True match" in states else "Possible match" if "Possible match" in states else "Cleared"


def resolve(case, uid, status, note, actor, risk="", reason=""):
    """An analyst's answer on one hit (from the case page, or the World-Check
    console with its risk and reason); closes the case when it was the last."""
    if case.get("status") == "closed":
        raise ValueError("The case is closed.")
    if status not in RESOLUTIONS:
        raise ValueError("Choose true match, possible match or false positive.")
    hit = next((h for h in (case.get("screening") or {}).get("hits") or [] if h["uid"] == uid), None)
    if hit is None:
        raise ValueError("No such hit on this case.")
    ai = hit.get("ai") or {}
    hit["resolution"] = {"status": status, "note": (note or "").strip()[:2000], "by": actor, "at": dates.now_str(),
                         "risk": risk, "reason": reason,
                         "ai_suggested": ai.get("suggested", "") if ai.get("status") == "done" else "",
                         "agreed": ai.get("status") == "done" and ai.get("suggested") == status}
    if not open_hits(case):
        close(case, actor)
    return hit


def close(case, actor, note=""):
    if case.get("status") == "closed":
        raise ValueError("The case is closed.")
    if open_hits(case):
        raise ValueError("Confirm every hit first.")
    case["status"] = "closed"
    case["closed"] = {"outcome": outcome(case), "by": actor, "at": dates.now_str(), "note": (note or "").strip()[:2000]}


def is_id(read):
    return bool(read) and read.get("document_type") != "Not an identity document" and bool(person(read)["name"])


def progress(case):
    """The case's steps for the case page's stepper, each with the time the
    case reached it: uploaded, read, screened, the AI's review of every hit,
    completed (every hit confirmed, or no hit confirmed). done | current | todo."""
    sc = case.get("screening") or {}
    hits = sc.get("hits") or []
    ai = [h.get("ai") or {} for h in hits]
    reviewed = bool(sc) and not sc.get("error") and all(a.get("status") in ("done", "failed") for a in ai)
    closed = case.get("status") == "closed"
    steps = [("Submitted", case["at"], True),
             ("ID Verification", case["at"] if case.get("read") else "", bool(case.get("read"))),
             ("Data Screening", sc.get("at", "") if sc and not sc.get("error") else "", bool(sc) and not sc.get("error")),
             ("AI Review", max([a.get("at", "") for a in ai] or [sc.get("at", "")]) if reviewed else "", reviewed or closed),
             ("Completed", (case.get("closed") or {}).get("at", ""), closed)]
    out, current = [], False
    for n, (label, at, done) in enumerate(steps, 1):
        state = "done" if done else "todo" if current else "current"
        current = current or state == "current"
        out.append({"label": label, "at": at, "state": state, "n": n})
    return out


# ── auto-verify: the AI closes the safe cases, an analyst gets the rest ──────
# What counts as safe is on the Settings page, not in code: whether a case
# with no World-Check hit closes by itself, the AI's confidence a "false
# positive" needs, whether the record's date of birth must differ, the lists
# (words of the record's category) that always go to a person, and the share
# of the cases the rule would close that still go to an analyst as a QC
# sample. Nothing is closed while an ID check warns or fails.

AUTO_ACTOR = "AI screening rule"


def _person_lists(settings):
    return [x.strip() for x in str(settings.get("auto_person_lists", "")).split(",") if x.strip()]


def _name_words(text):
    return {similarity.squash(p) for t in similarity.tokens(text or "") for p in t.split("-")} - {""}


def idv_differences(case):
    """[(what, the ID verification's, the ID's)]: where the ID verification read
    the document differently from the ID (a different document, or the
    sandbox's sample). A name with a word fewer (a middle name left out) is the
    same name; a value missing on either side is not compared."""
    idv, f = case.get("idv") or {}, (case.get("read") or {}).get("fields") or {}
    out = []
    theirs, ours = _name_words(idv.get("full_name")), _name_words(f.get("full_name"))
    if theirs and ours and not (theirs <= ours or ours <= theirs):
        out.append(("Name", idv["full_name"], f["full_name"]))
    for what, key in (("Date of birth", "date_of_birth"), ("Document number", "document_number")):
        a, b = (similarity.squash(str(x or "").upper()) for x in (idv.get(key), f.get(key)))
        if a and b and a != b:
            out.append((what, idv[key], f[key]))
    return out


def idv_approved(case):
    """(approved, why not): did the ID verification approve this very document?
    None set up (no result kept) counts as approved."""
    idv = case.get("idv") or {}
    if not idv:
        return True, ""
    who = idv.get("provider", "The ID verification")
    if idv.get("status") != "Approved":
        return False, f"{who} did not approve the document"
    if didit.is_sandbox(idv):
        return False, f"{who}'s sandbox answered with its sample, not a check of the document"
    diff = idv_differences(case)
    if diff:
        return False, f"{who} read a different " + ", ".join(d[0].lower() for d in diff) + " from the ID's"
    return True, ""


def hit_is_safe(case, hit, settings):
    """(safe, why): may the rule clear this hit as a false positive?"""
    ai = hit.get("ai") or {}
    bar, conf = int(settings["auto_min_confidence"]), ai.get("confidence")
    listed = next((x for x in _person_lists(settings) if x.lower() in (hit.get("category") or "").lower()), None)
    if case.get("verdict") != "Pass":
        return False, "an ID check did not pass"
    idv = case.get("idv") or {}
    approved, why = idv_approved(case)
    if not approved:
        return False, why
    if ai.get("status") != "done" or ai.get("outcome") != "clear":
        return False, "the AI did not say false positive"
    if not isinstance(conf, (int, float)) or conf < bar:
        return False, f"the AI was {conf}% sure, under the {bar}% bar"
    if settings.get("auto_dob_differs") and hit.get("dob_result") != "Not matched":
        return False, "the record's date of birth does not differ from the ID's"
    if listed:
        return False, f"a {listed} record always goes to an analyst"
    return True, (f"the AI said false positive at {conf}% (bar {bar}%)"
                  + (f"; {idv['provider']} approved the document" if idv else "")
                  + ("; the record's date of birth differs from the ID's" if settings.get("auto_dob_differs") else "")
                  + (f"; not a {' or '.join(_person_lists(settings))} record" if _person_lists(settings) else "")
                  + "; every ID check passed")


def qc_sampled(case_id, settings):
    """Is the case one of the QC sample (a fixed share, by its ID)?"""
    pct = int(settings.get("auto_qc_pct") or 0)
    return pct > 0 and int(hashlib.sha1(("qc:" + case_id).encode("utf-8")).hexdigest(), 16) % 100 < pct


def auto_verify(case, settings):
    """Once every open hit has the AI's answer: clear the hits the rule calls
    safe, and close the case when that is all of them, or when there is no
    hit and every ID check passed; the rest stay for an analyst. A case the
    rule would close entirely may instead be a QC sample, left for an
    analyst. Returns what it did: "closed", "cleared", "qc sample" or ""."""
    sc = case.get("screening") or {}
    if not settings.get("auto_verify") or case.get("status") == "closed" or not sc or sc.get("error"):
        return ""
    pending = open_hits(case)
    if any((h.get("ai") or {}).get("status") not in ("done", "failed") for h in pending):
        return ""                                   # the AI has not answered on every hit yet
    idv = case.get("idv") or {}
    if idv.get("status") == "pending":
        return ""                                   # nor the ID verification
    hits = sc.get("hits") or []
    if not hits:
        if not settings.get("auto_no_hit") or case.get("verdict") != "Pass" or not idv_approved(case)[0]:
            return ""
        whole, safe = True, []
    else:
        judged = [(h, *hit_is_safe(case, h, settings)) for h in pending]
        safe = [(h, why) for h, ok, why in judged if ok]
        if not safe:
            return ""
        whole = len(safe) == len(pending)
    if whole and qc_sampled(case["id"], settings):
        if not case.get("qc_sample"):
            case["qc_sample"] = {"at": dates.now_str(), "pct": int(settings["auto_qc_pct"])}
        return "qc sample"
    now = dates.now_str()
    for h, why in safe:
        h["resolution"] = {"status": "False positive", "note": "Cleared by the AI screening rule: " + why + ".", "by": AUTO_ACTOR,
                           "at": now, "risk": "", "reason": "Date of Birth mismatch" if settings.get("auto_dob_differs") else "",
                           "ai_suggested": "False positive", "agreed": True, "auto": True}
    if open_hits(case):
        return "cleared"
    close(case, AUTO_ACTOR, "No World-Check record matched the name, and every ID check passed." if not hits
          else f"Every hit cleared by the AI screening rule ({len(safe)}).")
    case["closed"]["auto"] = True
    return "closed"


# The console's alert outcomes (the name-screening page's words) for a hit's confirmation.
ALERT_OUTCOME = {"True match": "True Positive", "Possible match": "Potential Match", "False positive": "False Positive"}
FROM_ALERT_OUTCOME = {v: k for k, v in ALERT_OUTCOME.items()}


# ── the World-Check console view ─────────────────────────────────────────────
# A screening case shown in the simulated World-Check console (wc_case.html,
# wc_match.html), as the 1,000-case book shows its watchlist cases: the case
# is the person on the ID, its matches are the hits. A stand-in hit is the
# file's record built the way that book builds it (recon_book._match: key
# data, places, biography, aliases, dates); a World-Check One hit from what
# the API returned. Built from the case each time, never stored: the hit's
# confirmation is the match's resolution, in the console's words.

WC_STATUS = {"True match": "True positive", "Possible match": "Potential match", "False positive": "False positive"}
FROM_WC_STATUS = {v: k for k, v in WC_STATUS.items()}


def _wc_resolution(hit):
    res = hit.get("resolution")
    if not res:
        return {"status": "Unresolved", "risk": "", "reason": "", "remark": "", "comment": "", "by": "", "at": ""}
    return {"status": WC_STATUS[res["status"]], "risk": res.get("risk", ""), "reason": res.get("reason", ""),
            "remark": res.get("reason", ""), "comment": res.get("note", ""), "by": res["by"], "at": res["at"]}


def _wc_match(hit, key, matched_at):
    from fincrime.data import recon_book, seed      # lazy: the data builders are heavy and import the engines
    row = watchlist.row_of(hit["uid"]) if hit.get("source") == "stand-in" else None
    if row:
        m = recon_book._match(loader.build_case(row, 0), key)
    else:
        listed = hit["dob_text"].split(" (")[0]
        m = seed.wc_match(key=key, wc_id=hit["uid"], name=hit["name"], types=("SIC",), gender="",
                          dobs=[listed] if listed and listed != "Not listed" else [],
                          citizenship=hit["places"].split(";")[0].split(" (")[0], country=hit["places"],
                          category=(hit["category"] or "").upper(), bio=hit["narrative"], aliases=hit.get("aka") or [])
    ai = hit.get("ai") or {}
    m.update(id=hit["uid"], strength=hit["score"] / 100, matched=matched_at, resolution=_wc_resolution(hit),
             ai=dict(ai, status=ai.get("status") or "none", suggested_wc=WC_STATUS.get(ai.get("suggested"), "")))
    return m


def wc_view(case):
    """The case as the World-Check console's case and match pages read it."""
    sc = case.get("screening") or {}
    who = sc.get("person") or person(case.get("read"))
    at = sc.get("at") or case["at"]
    hits = sc.get("hits") or []
    audit = [{"at": at, "actor": "screening", "action": "Case screened",
              "detail": f"{len(hits)} record(s) matched on {sc.get('source_label') or 'World-Check'}."}]
    for h in hits:
        res = h.get("resolution")
        if res:
            how = ", ".join(x for x in (f"{res['risk']} risk" if res.get("risk") else "", res.get("reason", "")) if x)
            audit.append({"at": res["at"], "actor": res["by"], "action": f"Match resolved: {WC_STATUS[res['status']]}",
                          "detail": f"{h['name']} (WC {h['uid']}){' — ' + how if how else ''}. {res.get('note', '')}".strip()})
    for e in case.get("wc_exports") or []:
        audit.append({"at": e["at"], "actor": e["by"], "action": "Case exported", "detail": e["filename"]})
    audit.sort(key=lambda e: e["at"])
    dob = who.get("dob", "") if len(who.get("dob", "")) == 10 and who["dob"][4] == "-" else ""
    return {"id": case["id"], "key": case["id"], "alert_case_id": "", "kyc_case_id": "",
            "primary_name": who.get("name", ""), "gender": {"M": "Male", "F": "Female"}.get((who.get("sex") or "").upper()[:1], ""),
            "dob": dob, "group": "ID screening", "assignee": "Case Unassigned", "rating": "Not Rated", "ongoing_screening": "Off",
            "last_updated": max([at] + [h["resolution"]["at"] for h in hits if h.get("resolution")]),
            "case_details": {"Source": "World-Check One" if sc.get("source") == "World-Check One" else "Stand-in (reconciliation log)",
                             "DocumentType": who.get("document_type", ""), "DocumentNo": who.get("document_number", ""),
                             "Nationality": who.get("nationality", ""), "IssuingCountry": who.get("issuing_country", ""),
                             "UploadedBy": case.get("by", ""), "CaseStatus": "Closed" if case.get("status") == "closed" else "Open"},
            "matches": [_wc_match(h, f"{case['id']}-{h['uid']}", at) for h in hits],
            "exports": case.get("wc_exports") or [], "audit": audit}


def wc_export(case, actor):
    """A World-Check export of the case, kept with it (the console's Export)."""
    now = dates.now_str()
    name = "".join(ch for ch in (person(case.get("read"))["name"] or case["id"]).title() if ch.isalnum())
    exports = case.setdefault("wc_exports", [])
    export = {"id": f"exp-{len(exports) + 1}", "filename": f"WorldCheck_{name}_{now.replace('-', '').replace(':', '').replace(' ', '_')}.pdf",
              "at": now, "by": actor}
    exports.append(export)
    return export
