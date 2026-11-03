"""The model benchmark behind the Architecture page: the AI Agent's own
document read (ai_agent.read_content: the four document images, with its
SYSTEM prompt and SCHEMA) sent to each model Straive's LLM Foundry gateway
serves, and scored against what the drawn documents actually print.

The reconciliation log makes this measurable: every document image is drawn
from the file's own data, so for every case the right read is known (truth,
the same oracle the test suite uses), and so are the photos spoiled on
purpose (blurred, glare, cropped, covered). Each model is scored on

  fields       ten printed values read exactly (passport name, given names,
               date of birth, nationality, number, expiry; the secondary
               ID's name and date of birth; the proof of address's address
               and date), on the photos that are not spoiled;
  defects      spoiled photos it flags as such, and clean ones it flags
               wrongly (false alarms);
  decision     whether the agent's fixed checks on its read decide the case
               as they do on the right read (pass, or go to a person);
  cost, speed  tokens measured on every call at the provider's list price,
               and seconds per case.

Claude models are called through the Anthropic SDK exactly as the agent
calls them (structured output, adaptive thinking, medium effort; Haiku 4.5
without thinking, which it does not take). OpenAI and Gemini models are
called through the gateway's OpenAI-compatible routes with the same prompt,
the same images and the schema in the instructions (JSON mode). The images
are the drawn specimens, fictional: no real document leaves the machine.

Run:  .venv\\Scripts\\python.exe -m fincrime.benchmark [--cases 16] [--models a,b] [--workers 3]
It writes fincrime/data/benchmark.json, which /architecture shows.
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:  # run on its own, the gateway key comes from .env, as for app.py
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from fincrime.data import recon_book  # noqa: E402
from fincrime.data.loader import load_seed
from fincrime.documents import svg_cards
from fincrime.documents.svg_cards import ISO3
from fincrime.engines import ai_agent, sop, workflow
from fincrime.filebook import addresses, llm, similarity
from fincrime.utils import dates

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "benchmark.json")

# USD per million tokens (input, output), list prices.
MODELS = (
    {"id": "claude-opus-4-8", "provider": "anthropic", "label": "Claude Opus 4.8", "price": (5.00, 25.00), "current": True},
    {"id": "claude-sonnet-5-5", "provider": "anthropic", "label": "Claude Sonnet 5.5", "price": (2.00, 10.00)},
    {"id": "claude-sonnet-4-6", "provider": "anthropic", "label": "Claude Sonnet 4.6", "price": (3.00, 15.00)},
    {"id": "claude-haiku-4-5", "provider": "anthropic", "label": "Claude Haiku 4.5", "price": (1.00, 5.00), "no_thinking": True},
    {"id": "gpt-5", "provider": "openai", "label": "GPT-5", "price": (1.25, 10.00)},
    {"id": "gpt-5-mini", "provider": "openai", "label": "GPT-5 mini", "price": (0.25, 2.00)},
    {"id": "gpt-4.1", "provider": "openai", "label": "GPT-4.1", "price": (2.00, 8.00)},
    {"id": "gpt-4.1-mini", "provider": "openai", "label": "GPT-4.1 mini", "price": (0.40, 1.60)},
    {"id": "gemini-2.5-pro", "provider": "gemini", "label": "Gemini 2.5 Pro", "price": (1.25, 10.00)},
    {"id": "gemini-2.5-flash", "provider": "gemini", "label": "Gemini 2.5 Flash", "price": (0.30, 2.50)},
)
PRICE_SOURCES = ("Anthropic list prices (Claude API reference, 2026-10-06); OpenAI and Google list prices as published "
                 "(checked 2026-10-28 on third-party price trackers; confirm on openai.com/api/pricing and ai.google.dev/pricing). "
                 "Gateway (LLM Foundry) charges may differ.")
FIELDS = (("id", "surname", "Passport surname"), ("id", "given_names", "Passport given names"), ("id", "dob", "Date of birth"),
          ("id", "nationality_code", "Nationality"), ("id", "document_number", "Passport number"), ("id", "expiry", "Expiry"),
          ("secondary", "name", "Secondary ID name"), ("secondary", "dob", "Secondary ID date of birth"),
          ("poa", "address", "Proof-of-address address"), ("poa", "issued_on", "Proof-of-address date"))


# ── the right read ───────────────────────────────────────────────────────────

def truth(case):
    """What the case's drawn documents print, as a flawless reader would
    return it (the test suite's oracle)."""
    v = recon_book.view(case["recon"]["id"])
    pp, s = v["documents"]["passport"], v["synth"]
    idr = {"readable": True, "problems": [], "surname": pp["viz"]["surname"], "given_names": pp["viz"]["given"],
           "dob": v["customer"]["dob"], "nationality_code": ISO3.get(case["client"]["nationality"], ""),
           "document_number": pp["number"], "expiry": s["passport_expiry"], "address": "", "issued_on": s["passport_issued"],
           "note": "", "edges_visible": True, "mrz_visible": True}
    holder = v["identity"].get("name", {}).get("canonical", v["customer"]["name"])
    poa = {"readable": True, "problems": [], "surname": holder, "given_names": "", "dob": "", "nationality_code": "",
           "document_number": "", "expiry": "", "address": v["documents"]["poa"]["address"], "issued_on": s["poa_issued_on"],
           "note": ""}
    back = {"readable": True, "endorsements": "NONE", "issuing_authority": "PASSPORT OFFICE", "note": ""}
    sec = v["documents"]["secondary"]
    secondary = {"readable": True, "problems": [], "document_type": sec["type"], "name": sec["name"] or holder,
                 "dob": v["customer"]["dob"], "document_number": s["id_number"], "expiry": "",
                 "address": sec["address"]["text"] if sec["address"]["printed"] else "", "note": ""}
    return {"id": idr, "back": back, "secondary": secondary, "poa": poa}


# The capture defects that spoil the photo itself. Not among them: an
# expired passport (a good photo of an expired document: the expiry check
# catches it), a flat scan (the vendor's finding) and a slight angle (good
# photos have one).
PHOTO_DEFECTS = ("partial", "blur", "glare", "obstructed", "damaged")


def spoiled(case):
    """Is the case's passport photo spoiled on purpose (filebook documents)?"""
    s = recon_book.view(case["recon"]["id"])["synth"]
    return s.get("capture", "none") in PHOTO_DEFECTS


def sample(cases, n):
    """A fixed sample: individuals of the Individuals line, about three in
    four with clean photos spread over the file (India, Vietnam, the US),
    the rest spoiled ones of different kinds."""
    people = [c for c in cases if c["tenant"] == "ttlive" and not c.get("agency") and "recon" in c]
    clean = [c for c in people if not spoiled(c)]
    bad = [c for c in people if spoiled(c)]
    want_bad = max(1, n // 4)
    step = max(1, len(clean) // (n - want_bad))
    picked = clean[::step][:n - want_bad]
    kinds, out_bad = set(), []
    for c in bad:
        kind = recon_book.view(c["recon"]["id"])["synth"]["capture"]
        if kind not in kinds:
            kinds.add(kind)
            out_bad.append(c)
        if len(out_bad) == want_bad:
            break
    return picked + out_bad


# ── calling a model ──────────────────────────────────────────────────────────

def _render(case, doc):
    return recon_book.render(case, doc) or svg_cards.render(case, doc)


def _call_anthropic(model, content, system=None, schema=None):
    import anthropic
    client = anthropic.Anthropic(base_url=llm.GATEWAY + "/anthropic", auth_token=llm.KEY, api_key=None,
                                 timeout=240.0, max_retries=3)
    kwargs = {"model": model["id"], "max_tokens": 8000, "system": [{"type": "text", "text": system or ai_agent.SYSTEM}],
              "messages": [{"role": "user", "content": content}],
              "output_config": {"format": {"type": "json_schema", "schema": schema or ai_agent.SCHEMA}}}
    if not model.get("no_thinking"):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"]["effort"] = "medium"
    r = client.messages.create(**kwargs)
    text = next((b.text for b in r.content if b.type == "text"), "")
    return _json(text), r.usage.input_tokens, r.usage.output_tokens


def _call_openai_like(model, content, system=None, schema=None):
    route = "/openai/v1/chat/completions" if model["provider"] == "openai" else "/gemini/v1beta/openai/chat/completions"
    parts = []
    for b in content:
        if b["type"] == "text":
            parts.append({"type": "text", "text": b["text"]})
        else:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"}})
    system = ((system or ai_agent.SYSTEM) + "\n\nAnswer with one JSON object only, no other text, that matches this JSON schema:\n"
              + json.dumps(schema or ai_agent.SCHEMA))
    body = {"model": model["id"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": parts}],
            "response_format": {"type": "json_object"}}
    body["max_completion_tokens" if model["provider"] == "openai" else "max_tokens"] = 16000
    req = urllib.request.Request(llm.GATEWAY + route, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {llm.KEY}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"{exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
    usage = data.get("usage") or {}
    return _json(data["choices"][0]["message"]["content"] or ""), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def call(model, content, system=None, schema=None):
    """One model's answer to `content` as (parsed JSON, input tokens, output
    tokens). By default the AI Agent's document read (its SYSTEM and SCHEMA);
    the decision model (fincrime/decision_model.py) passes its own."""
    return (_call_anthropic if model["provider"] == "anthropic" else _call_openai_like)(model, content, system, schema)


# ── scoring ──────────────────────────────────────────────────────────────────

def _same(field, want, got, country):
    want, got = (want or "").strip(), (got or "").strip() if isinstance(got, str) else ""
    if field == "address":
        # as the agent compares it: the drawn proof of address prints the passport's country after an
        # overseas address (ai_agent._poa_address drops that tail)
        got = ai_agent._poa_address(got, country)
        return addresses.compare(addresses.normalise(want, country), addresses.normalise(got, country)) == "same"
    if field in ("dob", "expiry", "issued_on"):
        return want[:10] == got[:10]
    if field in ("surname", "given_names", "name"):
        # the words, in any order: a US licence prints the surname line first, and
        # the console's name rules do not depend on the order
        return sorted(similarity.fold(want).upper().split()) == sorted(similarity.fold(got).upper().split())
    return similarity.squash(similarity.fold(want)).upper() == similarity.squash(similarity.fold(got)).upper()


def _flagged(read):
    idr = read.get("id") or {}
    named = [p for p in idr.get("problems", []) if p not in ai_agent.SOFT]
    return (not idr.get("readable", True)) or bool(named) or idr.get("edges_visible") is False or idr.get("mrz_visible") is False


def _passes(case, read):
    return all(c["ok"] for c in ai_agent.checks(case, read)[0])


def score(case, read):
    """One model's read of one case against the right one."""
    right, bad = truth(case), spoiled(case)
    out = {"case": case["id"], "spoiled": bad, "flagged": _flagged(read), "fields": {}}
    if not bad:
        country = case["client"]["nationality"]
        no_age_limit = (workflow.poa_docs(case) or [{}])[0].get("kind") in sop.value("poa_no_age_limit")
        for section, field, _label in FIELDS:
            if (section, field) == ("poa", "issued_on") and no_age_limit:
                continue                    # an Aadhaar card: its date is the card's, which the SOP does not use
            out["fields"][f"{section}.{field}"] = _same(field, right[section].get(field), (read.get(section) or {}).get(field), country)
    expected = (not bad) and _passes(case, right)
    try:
        decided = _passes(case, read)
    except Exception:                       # a read so malformed the checks cannot run: a person's
        decided = False
    out.update(expected_pass=expected, model_pass=decided, decision_agrees=expected == decided)
    return out


def summarise(model, rows):
    ok = [r for r in rows if not r.get("error")]
    fields = {f"{s}.{f}": [r["fields"][f"{s}.{f}"] for r in ok if not r["spoiled"] and f"{s}.{f}" in r["fields"]]
              for s, f, _ in FIELDS}
    all_fields = [x for v in fields.values() for x in v]
    bad = [r for r in ok if r["spoiled"]]
    clean = [r for r in ok if not r["spoiled"]]
    price_in, price_out = model["price"]
    costs = [(r["tokens_in"] * price_in + r["tokens_out"] * price_out) / 1e6 for r in ok]
    pct = lambda xs: round(100 * sum(xs) / len(xs), 1) if xs else None      # noqa: E731
    return {"id": model["id"], "label": model["label"], "provider": model["provider"], "price_in": price_in,
            "price_out": price_out, "current": bool(model.get("current")), "cases": len(rows), "errors": len(rows) - len(ok),
            "error_samples": [r["error"] for r in rows if r.get("error")][:3],
            "field_accuracy": pct(all_fields),
            "fields": {k: pct(v) for k, v in fields.items()},
            "defects_caught": f"{sum(r['flagged'] for r in bad)}/{len(bad)}",
            "false_alarms": f"{sum(r['flagged'] for r in clean)}/{len(clean)}",
            "decision_agreement": pct([r["decision_agrees"] for r in ok]),
            "seconds_median": round(statistics.median(r["seconds"] for r in ok), 1) if ok else None,
            "tokens_in_avg": round(statistics.mean(r["tokens_in"] for r in ok)) if ok else None,
            "tokens_out_avg": round(statistics.mean(r["tokens_out"] for r in ok)) if ok else None,
            "cost_per_case": round(statistics.mean(costs), 4) if costs else None,
            "cost_per_1000": round(1000 * statistics.mean(costs), 2) if costs else None}


# ── the run ──────────────────────────────────────────────────────────────────

def run(n_cases=16, model_ids=None, workers=3, log=print):
    if not llm.KEY:
        raise SystemExit("LLM_FOUNDRY_KEY is not set (.env): the benchmark calls the gateway.")
    _, kyc, _, _ = load_seed("file")
    cases = sample(kyc, n_cases)
    models = [m for m in MODELS if not model_ids or m["id"] in model_ids]
    contents = {c["id"]: ai_agent.read_content(c, _render) for c in cases}
    rows, lock = {m["id"]: [] for m in models}, threading.Lock()

    def one(model, case):
        started = time.monotonic()
        try:
            read, t_in, t_out = call(model, contents[case["id"]])
            row = dict(score(case, read), tokens_in=t_in, tokens_out=t_out, seconds=round(time.monotonic() - started, 1),
                       read=_compact(read))
        except Exception as exc:            # one failed call is counted, not fatal
            row = {"case": case["id"], "spoiled": spoiled(case), "error": f"{type(exc).__name__}: {exc}"[:300],
                   "seconds": round(time.monotonic() - started, 1)}
        with lock:
            rows[model["id"]].append(row)
            log(f"{model['id']:18} {case['id']}  {'ERROR ' + row['error'][:80] if row.get('error') else 'ok'}  {row['seconds']} s")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for case in cases:
            for model in models:
                pool.submit(one, model, case)
    result = {"run_at": dates.now_str(), "gateway": llm.GATEWAY, "price_sources": PRICE_SOURCES,
              "sample": {"cases": len(cases), "spoiled": sum(spoiled(c) for c in cases), "ids": [c["id"] for c in cases]},
              "fields": [{"key": f"{s}.{f}", "label": label} for s, f, label in FIELDS],
              "models": [summarise(m, rows[m["id"]]) for m in models],
              "per_case": rows}
    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    return result


_KEPT = {"id": ("readable", "problems", "surname", "given_names", "dob", "nationality_code", "document_number", "expiry",
                "edges_visible", "mrz_visible"),
         "back": ("readable", "endorsements", "issuing_authority"),
         "secondary": ("readable", "problems", "document_type", "name", "dob", "document_number", "address"),
         "poa": ("readable", "problems", "surname", "given_names", "address", "issued_on")}


def _compact(read):
    """The parts of a model's read the scoring and the checks use, kept with
    the run so it can be scored again without calling a model (rescore)."""
    return {sec: {k: (read.get(sec) or {})[k] for k in keys if k in (read.get(sec) or {})} for sec, keys in _KEPT.items()}


def rescore(result=None):
    """Score a stored run again (the scoring changed, not the reads); writes
    the results file and returns it."""
    result = result or load()
    _, kyc, _, _ = load_seed("file")
    by_id = {c["id"]: c for c in kyc}
    for model in MODELS:
        rows = result["per_case"].get(model["id"])
        if rows is None:
            continue
        for i, row in enumerate(rows):
            if row.get("read") is not None and not row.get("error"):
                rows[i] = dict(score(by_id[row["case"]], row["read"]), tokens_in=row["tokens_in"], tokens_out=row["tokens_out"],
                               seconds=row["seconds"], read=row["read"])
    result["models"] = [summarise(m, result["per_case"][m["id"]]) for m in MODELS if m["id"] in result["per_case"]]
    result["sample"]["spoiled"] = sum(spoiled(by_id[cid]) for cid in result["sample"]["ids"])
    result["fields"] = [{"key": f"{s}.{f}", "label": label} for s, f, label in FIELDS]
    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    return result


def _ratio(text):
    got, of = (int(x) for x in text.split("/"))
    return got, of


def verdict(result):
    """What a run's numbers say, in a few plain lines ([] without a run):
    the models that made no mistake, the cheapest of them against the model
    in use, and who missed what. A model counts as faultless only when every
    field, every decision and every photo check was right."""
    if not result:
        return []
    ms = [m for m in result["models"] if m.get("field_accuracy") is not None and not m["errors"]]
    if not ms:
        return []
    current = next((m for m in ms if m["current"]), None)
    faultless = [m for m in ms if m["field_accuracy"] == 100.0 and m["decision_agreement"] == 100.0
                 and _ratio(m["defects_caught"])[0] == _ratio(m["defects_caught"])[1] and _ratio(m["false_alarms"])[0] == 0]
    lines = []
    if faultless:
        cheapest = min(faultless, key=lambda m: m["cost_per_case"])
        lines.append(f"No mistake on this sample (every field, every decision, every photo check): "
                     f"{', '.join(m['label'] for m in sorted(faultless, key=lambda m: m['cost_per_case']))}.")
        if current and cheapest is not current and cheapest["cost_per_case"]:
            lines.append(f"The cheapest of them, {cheapest['label']}, costs ${cheapest['cost_per_case']} a case "
                         f"(${cheapest['cost_per_1000']} per 1,000 cases): {current['cost_per_case'] / cheapest['cost_per_case']:.1f}x "
                         f"less than {current['label']}, in use today at ${current['cost_per_case']}, and takes "
                         f"{cheapest['seconds_median']} s a case against {current['seconds_median']} s.")
    elif current:
        lines.append(f"In use today: {current['label']}, {current['field_accuracy']}% of fields at ${current['cost_per_case']} a case.")
    for what, test in (("Decided a case otherwise than on the right read (a clear document taken for unreadable, or a misread)",
                        lambda m: m["decision_agreement"] < 100),
                       ("Missed a spoiled photo", lambda m: _ratio(m["defects_caught"])[0] < _ratio(m["defects_caught"])[1]),
                       ("Flagged a clean photo", lambda m: _ratio(m["false_alarms"])[0] > 0),
                       ("Misread a printed field", lambda m: m["field_accuracy"] < 100)):
        names = [m["label"] for m in ms if test(m)]
        if names:
            lines.append(f"{what}: {', '.join(names)}.")
    failed = [f"{m['label']} ({m['errors']})" for m in result["models"] if m["errors"]]
    if failed:
        lines.append(f"Calls that failed (those models are not scored above): {', '.join(failed)}.")
    lines.append(f"A sample of {result['sample']['cases']} cases: a tie here is not a tie at scale. Rerun on more cases "
                 f"before changing the model.")
    return lines


def load():
    """The last run's results, or None."""
    try:
        with open(RESULTS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def confusion(result):
    """Each model's two confusion matrices on the run ([] without one): the
    photo check (spoiled or clean, against flagged or not) and the decision
    (pass or a person on the right read, against the same on its read).
    The shape of decision_model's matrices, so the page draws both alike."""
    if not result:
        return []
    out = []
    for m in result["models"]:
        rows = [r for r in result["per_case"].get(m["id"], []) if not r.get("error")]
        photo = {t: {c: 0 for c in ("Flagged", "Accepted")} for t in ("Spoiled", "Clean")}
        decision = {t: {c: 0 for c in ("Pass", "To a person")} for t in ("Pass", "To a person")}
        for r in rows:
            photo["Spoiled" if r["spoiled"] else "Clean"]["Flagged" if r["flagged"] else "Accepted"] += 1
            decision["Pass" if r["expected_pass"] else "To a person"]["Pass" if r["model_pass"] else "To a person"] += 1
        out.append({"label": m["label"], "current": m["current"], "cases": len(rows),
                    "photo": {"rows": ["Spoiled", "Clean"], "cols": ["Flagged", "Accepted"], "cells": photo},
                    "decision": {"rows": ["Pass", "To a person"], "cols": ["Pass", "To a person"], "cells": decision}})
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", type=int, default=16)
    ap.add_argument("--models", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--rescore", action="store_true", help="score the stored run again, with no model call")
    args = ap.parse_args()
    res = rescore() if args.rescore else run(args.cases, [m for m in args.models.split(",") if m], args.workers)
    for m in res["models"]:
        print(f"{m['label']:20} fields {m['field_accuracy']}%  defects {m['defects_caught']}  false alarms {m['false_alarms']}  "
              f"decision {m['decision_agreement']}%  {m['seconds_median']} s  ${m['cost_per_case']}/case  errors {m['errors']}")
