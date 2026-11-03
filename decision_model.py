"""The decision model behind the Model evaluation page: a model that decides
a screened KYC case from the facts an L1 analyst starts with, built from the
client's 1,000 sample cases and scored against what the analysts decided.

Every row of the reconciliation log holds the case's facts (the CRM record,
the passport's data page and MRZ, the secondary ID, the proof of address, the
World-Check record) and the analysts' answers (the disposition, the name and
address variance types, their notes). The model sees the facts only and
gives three answers in one call, each scored against the analysts':

  outcome          clear (false positive), true match, ask the customer for
                   documents, escalate to L2: the disposition, in four;
  name variance    the main name discrepancy across the documents, from the
                   client's variance reference (22 types in the file);
  address variance the main address discrepancy (21 types).

How the model is built from the cases. The file is split once, by case ID:
200 cases (a fifth of each outcome) are held out and only ever scored; the
other 800 are what the model learns from:
  - the SOP (fincrime/data/decision_sop/v*.md) is written and revised on a
    tuning slice of the 800, never on the held-out cases, with the client's
    variance reference (fincrime/filebook/data/variance_reference.csv)
    appended as the playbook;
  - for each case, the most similar of the 800 resolved cases go in the
    prompt with the analysts' outcome and notes (examples; 0 = the SOP
    alone).
Weight fine-tuning is not tried: the gateway serves model calls only.

Beside the language models, two baselines need no call:
  rules   the console's own rules: the date-of-birth rule clears a hit
          (ai_screening.dob_state); everything else goes to a person. For the
          name variance, the console's hand-written name rules
          (names.analyse), their first finding.
  tree    a decision tree learned from the 800 cases on signals computed
          from the facts (the date-of-birth relation, the places, an alias
          that is the customer's name, the list, the customer type).

What the model never sees (the analysts' answers, or set from them in the
file): the disposition, the compliance notes, the variance types, the pain
point, the resolution, the screening impact, the score after normalisation,
the handling time, the analyst and review date, the discrepancy analysis,
the matched attributes (except how many World-Check profiles the name
returned), the note beside the listed date of birth ("Exact Match", "12 yrs
diff"), and the risk rating (every true match is High in the file: set
after the review).

Run:  .venv\\Scripts\\python.exe -m fincrime.decision_model --models gemini-2.5-flash,claude-sonnet-5-5 [--sop v3] [--examples 6] [--set test|tune]
It writes fincrime/data/decision_eval.json, which /model-evaluation shows.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

try:  # run on its own, the gateway key comes from .env, as for app.py
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from fincrime import benchmark  # noqa: E402
from fincrime.engines import ai_screening
from fincrime.filebook import addresses, llm, loader, names, parse, similarity
from fincrime.utils import dates

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "data", "decision_eval.json")
SOP_DIR = os.path.join(HERE, "data", "decision_sop")
REFERENCE = os.path.join(HERE, "filebook", "data", "variance_reference.csv")

OUTCOMES = (("clear", "Clear (false positive)"), ("true_match", "True match"),
            ("request_documents", "Ask the customer"), ("escalate", "Escalate to L2"))
OUTCOME_KEYS = tuple(k for k, _ in OUTCOMES)
OUTCOME_LABEL = dict(OUTCOMES)
PERSON = "person"            # the rules' answer when they cannot decide
FAILED = "failed"            # a call that failed
NONE = "None – consistent across documents"
TEST_SHARE = 0.2
TUNE_SIZE = 60
DEFAULT_EXAMPLES = 6
DEFAULT_MODELS = ("claude-opus-4-8", "claude-sonnet-5-5", "claude-haiku-4-5", "gpt-5", "gpt-5-mini",
                  "gemini-2.5-pro", "gemini-2.5-flash")


# ── the file ─────────────────────────────────────────────────────────────────

_cache = {}


def rows():
    """The reconciliation log's rows, by case ID (read once)."""
    if "rows" not in _cache:
        _cache["rows"] = {r["kyc_case_id"]: r for r in loader.read_rows()}
    return _cache["rows"]


def outcome(row):
    """The analysts' disposition, in four."""
    d = row["disposition_status"]
    for prefix, key in (("False Positive", "clear"), ("True Positive", "true_match"), ("Pending", "request_documents"),
                        ("Escalated", "escalate")):
        if d.startswith(prefix):
            return key
    raise ValueError(f"{row['kyc_case_id']}: unknown disposition {d!r}")


def reference():
    """The client's variance reference: the name and the address types in
    its order, and its rows (country, field, type, what happens, control)."""
    if "reference" not in _cache:
        with open(REFERENCE, encoding="utf-8", newline="") as fh:
            ref = list(csv.DictReader(fh))
        kinds = {"Name": [], "Address": []}
        for r in ref:
            if r["Variance Type"] not in kinds[r["Field"]]:
                kinds[r["Field"]].append(r["Variance Type"])
        _cache["reference"] = {"name": [NONE] + kinds["Name"], "address": [NONE] + kinds["Address"], "rows": ref}
    return _cache["reference"]


def _h(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def split():
    """The fixed split: test = a fifth of each outcome, chosen by a hash of
    the case ID; train = the rest; tune = TUNE_SIZE of train, again by
    outcome, for revising the SOP."""
    by = {}
    for cid, r in rows().items():
        by.setdefault(outcome(r), []).append(cid)
    test, train = [], []
    for ids in by.values():
        ids = sorted(ids, key=_h)
        n = round(len(ids) * TEST_SHARE)
        test += ids[:n]
        train += ids[n:]
    tune = []
    for key, ids in by.items():
        pool = sorted((i for i in ids if i in set(train)), key=lambda i: _h("tune" + i))
        tune += pool[:max(2, round(TUNE_SIZE * len(ids) / len(rows())))]
    return {"test": sorted(test), "train": sorted(train), "tune": sorted(tune)}


# ── what the model sees ──────────────────────────────────────────────────────

def _listed_dob(row):
    w = parse.wc_dob(row["world_check_listed_dob"])
    return {"full": w["date"], "year": str(w["year"]), "range": f"{w['year']}–{w['year_to']}"}.get(w["kind"], "not listed")


def facts(row):
    """The case as the analyst starts it: no answer of theirs in it."""
    profiles = re.search(r"\bof (\d+) WC profiles", row["matched_attributes"])
    lines = [
        f"Customer: {row['customer_name']} ({row['customer_type']})",
        f"  date of birth: {row['customer_provided_dob']}",
        f"  registered address (CRM): {row['customer_registered_address']}",
        f"Passport ({row['passport_issuing_country']}, no. {row['passport_no']})",
        f"  data page: {row['name_passport_data_page_viz']}",
        f"  MRZ line 1: {row['name_passport_mrz_line_1']}",
        f"  address: {row['address_passport']}",
        f"Secondary ID ({row['secondary_id_type']})",
        f"  name: {row['name_secondary_id']}",
        f"  address: {row['address_secondary_id']}",
        f"Proof of address ({row['proof_of_address_type']}): {row['address_proof_of_address']}",
        f"World-Check record {row['world_check_uid']}: {row['world_check_entity_subject_name']}",
        f"  listed date of birth: {_listed_dob(row)}",
        f"  listed places: {row['world_check_listed_address_jurisdiction']}",
        f"  category: {row['watchlist_category']}; match score {row['match_score']}"
        + (f"; the name returned {profiles.group(1)} World-Check profiles" if profiles else ""),
        f"  trigger: {row['world_check_narrative_trigger_details']}",
    ]
    return "\n".join(lines)


def example(row):
    """A resolved case from the training cases: its facts and what the
    analysts decided and wrote."""
    return (facts(row) + f"\nAnalysts' outcome: {outcome(row)} ({row['disposition_status']})"
            f"\nName variance: {row['name_variance_type']}\nAddress variance: {row['address_variance_type']}"
            f"\nTheir note: {row['compliance_action_regulatory_notes']}")


# ── signals computed from the facts (the tree, the rules, the examples) ─────

def _dob_relation(row):
    cd = ai_screening._client_dob(parse.customer_dob(row["customer_provided_dob"]))
    w = parse.wc_dob(row["world_check_listed_dob"])
    if cd is None or w["kind"] == "none":
        return "none"
    if w["kind"] == "full":
        listed = ai_screening._client_dob(w["date"])
        if listed == cd:
            return "same date"
        if listed and ai_screening._swapped(listed, cd):
            return "day/month swapped"
        return "other date"
    if w["kind"] == "year":
        return "same year" if w["year"] == cd.year else "other year"
    return "year in range" if w["year"] <= cd.year <= w["year_to"] else "year out of range"


_COUNTRY_WORDS = {"United States": ("UNITED STATES", "USA"), "Vietnam": ("VIETNAM", "VIET NAM"), "India": ("INDIA",)}


def _place_relation(row):
    """The closest a listed place comes to an address on the customer's
    documents: city, region, country (the passport's, or one an address
    names), or abroad."""
    texts = [similarity.fold(row[k]) for k in ("customer_registered_address", "address_proof_of_address",
                                               "address_secondary_id", "address_passport")]
    blob = " | ".join(texts)

    def has(word):
        return bool(word) and re.search(r"(?<![A-Z])" + re.escape(word) + r"(?![A-Z])", blob) is not None

    best = "abroad"
    for p in parse.wc_location(row["world_check_listed_address_jurisdiction"])["places"]:
        city = re.sub(r"\s+CITY$", "", similarity.fold(p["city"]))
        cities = {city, similarity.fold(p["former"]), addresses.IN_LEGACY_CITY.get(city, "")}
        cities |= {k for k, v in addresses.IN_LEGACY_CITY.items() if v == city}
        if any(has(c) for c in cities):
            return "city"
        if p["region"] and has(similarity.fold(p["region"])):
            best = "region"
        elif best == "abroad" and (p["country"] == row["passport_issuing_country"]
                                   or any(has(w) for w in _COUNTRY_WORDS.get(p["country"], (similarity.fold(p["country"]),)))):
            best = "country"
    return best


def _alias_is_customer(row):
    m = re.search(r"\(AKA:\s*(.*?)\)", row["world_check_entity_subject_name"])
    if not m:
        return "no"
    ct = parse.customer_type(row["customer_type"])
    own = {similarity.fold(row["customer_name"]), similarity.fold(ct["ubo"])} - {""}
    return "yes" if similarity.fold(m.group(1)) in own else "no"


def signals(row):
    """The handful of facts the tree learns from and the examples are
    chosen by."""
    if "signals" not in _cache:
        _cache["signals"] = {}
    cid = row["kyc_case_id"]
    if cid not in _cache["signals"]:
        ct = parse.customer_type(row["customer_type"])
        score = parse.percent(row["match_score"])
        _cache["signals"][cid] = {
            "date of birth": _dob_relation(row), "place": _place_relation(row), "alias is the customer": _alias_is_customer(row),
            "list": parse.wc_group(row["watchlist_category"]),
            "customer": "company" if ct["corporate"] else "NRI" if ct["nri"] else "individual",
            "country": row["passport_issuing_country"], "match score": "90+" if score >= 90 else "80-89" if score >= 80 else "under 80"}
    return _cache["signals"][cid]


def name_rules(row):
    """The console's hand-written name rules on the case: the variance
    types they detect, in their order."""
    if "names" not in _cache:
        _cache["names"] = {}
    cid = row["kyc_case_id"]
    if cid not in _cache["names"]:
        ct = parse.customer_type(row["customer_type"])
        a = names.analyse(parse.country_code(row["passport_issuing_country"]), row["customer_name"],
                          parse.viz(row["name_passport_data_page_viz"]), parse.mrz1(row["name_passport_mrz_line_1"]),
                          row["name_secondary_id"], ct["corporate"], ct["ubo"])
        _cache["names"][cid] = list(a.get("variance_detected") or [])
    return _cache["names"][cid]


# ── the baselines ────────────────────────────────────────────────────────────

def rules_decide(row):
    """The console's screening rule on the case: the date-of-birth rule
    clears a record that is plainly someone else; anything else, a person.
    The name rules' first finding stands for the name variance."""
    dob = parse.wc_dob(row["world_check_listed_dob"])
    listed = [dob["date"] or (str(dob["year"]) if dob["kind"] == "year" else "")] if dob["kind"] in ("full", "year") else []
    state = ai_screening.dob_state(parse.customer_dob(row["customer_provided_dob"]), [x for x in listed if x])
    found = name_rules(row)
    return {"outcome": "clear" if state == "conflict" else PERSON, "name_variance": found[0] if found else NONE,
            "address_variance": None}


def _gini(labels):
    n = len(labels)
    return 1.0 - sum((c / n) ** 2 for c in Counter(labels).values()) if n else 0.0


def train_tree(train_rows, max_depth=4, min_leaf=5):
    """A small classification tree (CART, Gini) on the signals, learned
    from the training cases. Each split asks whether one signal has one
    value."""
    data = [(signals(r), outcome(r)) for r in train_rows]

    def grow(items, depth):
        labels = [y for _, y in items]
        node = {"predict": Counter(labels).most_common(1)[0][0], "counts": dict(Counter(labels)), "n": len(items)}
        if depth >= max_depth or len(set(labels)) == 1:
            return node
        best, base = None, _gini(labels)
        for feat in items[0][0]:
            for val in sorted({x[feat] for x, _ in items}):
                yes = [(x, y) for x, y in items if x[feat] == val]
                no = [(x, y) for x, y in items if x[feat] != val]
                if len(yes) < min_leaf or len(no) < min_leaf:
                    continue
                gain = base - (len(yes) * _gini([y for _, y in yes]) + len(no) * _gini([y for _, y in no])) / len(items)
                if gain > 1e-9 and (best is None or gain > best[0]):
                    best = (gain, feat, val, yes, no)
        if best is None:
            return node
        _, feat, val, yes, no = best
        node.update(feature=feat, value=val, yes=grow(yes, depth + 1), no=grow(no, depth + 1))
        return node

    return grow(data, 0)


def tree_predict(node, sig):
    while "feature" in node:
        node = node["yes"] if sig[node["feature"]] == node["value"] else node["no"]
    return node["predict"]


def tree_lines(node, indent=""):
    """The tree as indented if/else lines a reviewer can read."""
    if "feature" not in node:
        counts = ", ".join(f"{OUTCOME_LABEL[k]} {v}" for k, v in sorted(node["counts"].items(), key=lambda kv: -kv[1]))
        return [f"{indent}→ {OUTCOME_LABEL[node['predict']]}  ({node['n']} training cases: {counts})"]
    return ([f"{indent}if {node['feature']} is {node['value']}:"] + tree_lines(node["yes"], indent + "    ")
            + [f"{indent}else:"] + tree_lines(node["no"], indent + "    "))


# ── the language model ───────────────────────────────────────────────────────

def sop_versions():
    try:
        return sorted((f[:-3] for f in os.listdir(SOP_DIR) if re.fullmatch(r"v\d+\.md", f)), key=lambda v: int(v[1:]))
    except OSError:
        return []


_CHANGES = re.compile(r"<!--(.*?)-->", re.S)


def _sop_file(version):
    with open(os.path.join(SOP_DIR, f"{version}.md"), encoding="utf-8") as fh:
        return fh.read()


def sop_text(version):
    """The SOP the model is given: the file without its change note."""
    return _CHANGES.sub("", _sop_file(version)).strip()


def sop_changes(version):
    """The change note at the top of a revised SOP (an HTML comment, kept
    out of the prompt): what changed from the version before, and why."""
    m = _CHANGES.search(_sop_file(version))
    return [line.strip(" -") for line in m.group(1).strip().splitlines() if line.strip(" -")] if m else []


def system(version):
    """The model's instructions under one SOP version of the files."""
    return instructions(sop_text(version))


def instructions(sop_body):
    """The model's instructions: the SOP (a version's text, or one edited in
    the console), the client's variance reference as its playbook, and how
    to answer."""
    ref = reference()
    playbook = "\n".join(f"- {r['Passport Country']} · {r['Field']} · {r['Variance Type']}: {r['What happens']} "
                         f"Control: {r['Recommended control']}" for r in ref["rows"])
    return (sop_body.strip() + "\n\n## Variance reference (the client's playbook)\n" + playbook
            + "\n\n## Answer\nname_variance and address_variance: one type each, exactly as the reference names it, or "
              f"\"{NONE}\". dob_finding: one short sentence. outcome: clear, true_match, request_documents or escalate. "
              "confidence: 0-100 for the outcome. reasons: at most three short facts. request: the document to ask the "
              "customer for when the outcome is request_documents, else empty.")


def schema():
    ref = reference()
    return {"type": "object", "additionalProperties": False,
            "required": ["name_variance", "address_variance", "dob_finding", "outcome", "confidence", "reasons", "request"],
            "properties": {"name_variance": {"type": "string", "enum": ref["name"]},
                           "address_variance": {"type": "string", "enum": ref["address"]},
                           "dob_finding": {"type": "string"},
                           "outcome": {"type": "string", "enum": list(OUTCOME_KEYS)},
                           "confidence": {"type": "integer"},
                           "reasons": {"type": "array", "items": {"type": "string"}},
                           "request": {"type": "string"}}}


def neighbours(row, library, k):
    """The k training cases most like this one: the same date-of-birth
    relation and passport country count most, then the name changes the
    console's rules find on both, the place, the customer type, the list.
    Never the case itself."""
    s, mine = signals(row), set(name_rules(row))
    scored = []
    for other in library:
        if other["kyc_case_id"] == row["kyc_case_id"]:
            continue
        o = signals(other)
        w = (3 * (o["date of birth"] == s["date of birth"]) + 3 * (o["country"] == s["country"])
             + 2 * len(mine & set(name_rules(other))) + (o["place"] == s["place"]) + (o["customer"] == s["customer"])
             + (o["list"] == s["list"]))
        scored.append((-w, _h(row["kyc_case_id"] + other["kyc_case_id"]), other))
    return [o for _, _, o in sorted(scored, key=lambda t: t[:2])[:k]]


def prompt(row, library, k):
    parts = []
    if k:
        parts.append("Resolved cases like this one, with what the analysts decided:\n\n"
                     + "\n\n".join(f"### Example {i}\n{example(o)}" for i, o in enumerate(neighbours(row, library, k), 1)))
    parts.append("### The case to decide\n" + facts(row))
    return "\n\n".join(parts)


def _closest(value, allowed):
    """A model's answer held to the allowed values: exact, or the same once
    case, dashes and spacing are set aside; else the answer as given."""
    if value in allowed:
        return value
    key = re.sub(r"[\s\-–—]+", " ", str(value or "")).strip().lower()
    for a in allowed:
        if re.sub(r"[\s\-–—]+", " ", a).strip().lower() == key:
            return a
    return str(value or "")


# ── a run ────────────────────────────────────────────────────────────────────

def run_key(subset, model_id, sop, k):
    return f"{subset}:{model_id}:{sop}:{k}"


def _model(model_id):
    m = next((m for m in benchmark.MODELS if m["id"] == model_id), None)
    if m is None:
        raise SystemExit(f"Unknown model {model_id}: one of {', '.join(m['id'] for m in benchmark.MODELS)}")
    return m


def run(model_ids=DEFAULT_MODELS, sop=None, k=DEFAULT_EXAMPLES, subset="test", workers=4, retry_failed=False, limit=None,
        log=print):
    """Each model on the subset's cases; the runs are kept in the results
    file under run_key, saved as they go. retry_failed calls again only the
    cases whose call failed in the stored run. sop: the latest version
    unless named."""
    sop = sop or sop_versions()[-1]
    if not llm.KEY:
        raise SystemExit("LLM_FOUNDRY_KEY is not set (.env): the decision model calls the gateway.")
    result = load() or {}
    sp, by_id = split(), rows()
    library = [by_id[i] for i in sp["train"]]
    instructions, answer_schema = system(sop), schema()
    ref, lock = reference(), threading.Lock()
    for model_id in model_ids:
        model = _model(model_id)
        key = run_key(subset, model_id, sop, k)
        stored = (result.get("runs") or {}).get(key)
        done = {r["case"]: r for r in (stored or {}).get("per_case", []) if not (retry_failed and r.get("error"))}
        todo = [cid for cid in sp[subset] if cid not in done][:limit]
        log(f"{key}: {len(todo)} to call, {len(done)} kept")

        def one(cid):
            row, started = by_id[cid], time.monotonic()
            try:
                ans, t_in, t_out = benchmark.call(model, [{"type": "text", "text": prompt(row, library, k)}], instructions, answer_schema)
                rec = {"case": cid, "truth": outcome(row), "outcome": _closest(ans.get("outcome"), OUTCOME_KEYS),
                       "name_truth": row["name_variance_type"], "name_variance": _closest(ans.get("name_variance"), ref["name"]),
                       "address_truth": row["address_variance_type"],
                       "address_variance": _closest(ans.get("address_variance"), ref["address"]),
                       "confidence": ans.get("confidence"), "dob_finding": str(ans.get("dob_finding", ""))[:300],
                       "reasons": [str(x)[:300] for x in (ans.get("reasons") or [])][:4], "request": str(ans.get("request", ""))[:300],
                       "tokens_in": t_in, "tokens_out": t_out}
            except Exception as exc:          # one failed call is counted, not fatal
                rec = {"case": cid, "truth": outcome(row), "name_truth": row["name_variance_type"],
                       "address_truth": row["address_variance_type"], "error": f"{type(exc).__name__}: {exc}"[:300]}
            rec["seconds"] = round(time.monotonic() - started, 1)
            with lock:
                done[cid] = rec
                log(f"{model_id:18} {cid} {'ERROR ' + rec['error'][:70] if rec.get('error') else rec['outcome'] + ' / ' + rec['truth']}"
                    f"  {rec['seconds']} s  [{len(done)}/{len(sp[subset])}]")
                if len(done) % 20 == 0:
                    _store(result, key, model, sop, k, subset, done)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
        _store(result, key, model, sop, k, subset, done)
    return result


def _store(result, key, model, sop, k, subset, done):
    per_case = [done[c] for c in sorted(done)]
    result.setdefault("runs", {})[key] = {
        "key": key, "kind": "llm", "model": model["id"], "label": model["label"], "provider": model["provider"],
        "price": list(model["price"]), "current": bool(model.get("current")), "sop": sop, "examples": k, "set": subset,
        "run_at": dates.now_str(), "per_case": per_case, "summary": summarise(per_case, model["price"])}
    save(result)


def baselines(result=None, write=True):
    """The rules and the tree on the test and tuning cases (no call),
    added to `result` (the results file by default) and saved."""
    result = result if result is not None else (load() or {})
    sp, by_id = split(), rows()
    tree = train_tree([by_id[i] for i in sp["train"]])
    for subset in ("test", "tune"):
        for kind, label, decide in (("rules", "Console rules today", rules_decide),
                                    ("tree", "Decision tree learned from the 800 cases",
                                     lambda r: {"outcome": tree_predict(tree, signals(r)), "name_variance": None,
                                                "address_variance": None})):
            per_case = []
            for cid in sp[subset]:
                row, d = by_id[cid], decide(by_id[cid])
                per_case.append({"case": cid, "truth": outcome(row), "outcome": d["outcome"],
                                 "name_truth": row["name_variance_type"], "name_variance": d["name_variance"],
                                 "address_truth": row["address_variance_type"], "address_variance": d["address_variance"]})
            key = f"{subset}:{kind}"
            result.setdefault("runs", {})[key] = {"key": key, "kind": kind, "label": label, "set": subset, "run_at": dates.now_str(),
                                                  "per_case": per_case, "summary": summarise(per_case)}
    counts = {s: dict(Counter(outcome(by_id[i]) for i in ids)) for s, ids in sp.items()}
    result["split"] = {"sizes": {s: len(ids) for s, ids in sp.items()}, "outcomes": counts, "test_ids": sp["test"]}
    result["tree"] = tree_lines(tree)
    result["reference"] = {"name": reference()["name"], "address": reference()["address"]}
    if write:
        save(result)
    return result


# ── scoring ──────────────────────────────────────────────────────────────────

def _matrix(per_case, truth_key, pred_key, labels, extra=()):
    cols = list(labels) + list(extra)
    m = {t: {c: 0 for c in cols} for t in labels}
    for r in per_case:
        t, p = r.get(truth_key), (FAILED if r.get("error") else r.get(pred_key))
        if t not in m:
            continue
        if p not in m[t]:
            m[t][p] = 0
            cols.append(p) if p not in cols else None
        m[t][p] += 1
    return {"rows": list(labels), "cols": cols, "cells": m}


def summarise(per_case, price=None):
    ok = [r for r in per_case if not r.get("error")]
    n = len(per_case)
    pct = lambda a, b: round(100 * a / b, 1) if b else None        # noqa: E731
    right = sum(r["outcome"] == r["truth"] for r in ok)
    decided = [r for r in ok if r["outcome"] in OUTCOME_KEYS]
    open_ = lambda o: "open" if o in ("request_documents", "escalate", PERSON) else o   # noqa: E731
    per_class = {}
    f1s = []
    for c in OUTCOME_KEYS:
        tp = sum(r["outcome"] == c and r["truth"] == c for r in ok)
        said = sum(r["outcome"] == c for r in ok)
        real = sum(r["truth"] == c for r in ok)
        prec, rec = (tp / said if said else None), (tp / real if real else None)
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
        f1s.append(f1)
        per_class[c] = {"precision": pct(tp, said), "recall": pct(tp, real), "support": real}
    out = {"cases": n, "errors": n - len(ok), "error_samples": [r["error"] for r in per_case if r.get("error")][:3],
           "accuracy": pct(right, len(ok)), "automated": pct(len(decided), len(ok)),
           "right_when_decided": pct(sum(r["outcome"] == r["truth"] for r in decided), len(decided)),
           "settled_accuracy": pct(sum(open_(r["outcome"]) == open_(r["truth"]) for r in ok), len(ok)),
           "missed_true_matches": sum(r["truth"] == "true_match" and r["outcome"] == "clear" for r in ok),
           "wrongly_cleared": sum(r["truth"] != "clear" and r["outcome"] == "clear" for r in ok),
           "macro_f1": round(100 * sum(f1s) / len(f1s), 1), "per_class": per_class,
           "outcome_matrix": _matrix(per_case, "truth", "outcome", OUTCOME_KEYS, (PERSON,) if any(r.get("outcome") == PERSON for r in ok) else ())}
    ref = reference()
    for field, labels in (("name", ref["name"]), ("address", ref["address"])):
        scored = [r for r in ok if r.get(f"{field}_variance") is not None]
        if scored:
            seen = [l for l in labels if any(r[f"{field}_truth"] == l for r in scored)]
            out[f"{field}_accuracy"] = pct(sum(r[f"{field}_variance"] == r[f"{field}_truth"] for r in scored), len(scored))
            out[f"{field}_matrix"] = _matrix(scored, f"{field}_truth", f"{field}_variance", seen)
            pairs = Counter((r[f"{field}_truth"], r[f"{field}_variance"]) for r in scored if r[f"{field}_variance"] != r[f"{field}_truth"])
            out[f"{field}_confusions"] = [{"truth": t, "said": s, "n": c} for (t, s), c in pairs.most_common(8)]
        else:
            out[f"{field}_accuracy"] = None
    confident = [r for r in ok if r["outcome"] == "clear" and isinstance(r.get("confidence"), (int, float)) and r["confidence"] >= 90]
    out["confident_clears"] = {"n": len(confident), "wrong": sum(r["truth"] != "clear" for r in confident)}
    if price and ok:
        costs = [(r["tokens_in"] * price[0] + r["tokens_out"] * price[1]) / 1e6 for r in ok]
        out.update(cost_per_case=round(statistics.mean(costs), 4), cost_per_1000=round(1000 * statistics.mean(costs), 2),
                   seconds_median=round(statistics.median(r["seconds"] for r in ok), 1),
                   tokens_in_avg=round(statistics.mean(r["tokens_in"] for r in ok)),
                   tokens_out_avg=round(statistics.mean(r["tokens_out"] for r in ok)))
    return out


# ── the page ─────────────────────────────────────────────────────────────────

SHORT = {"clear": "Clear", "true_match": "True match", "request_documents": "Ask customer", "escalate": "Escalate",
         PERSON: "To a person", FAILED: "Call failed"}


def matrix_view(m, short=None, danger=None, right=None):
    """A confusion matrix for the page: each cell's count, its share of the
    row (the shade) and its kind: right (the same label, unless `right`
    says otherwise), a dangerous miss (a hit cleared that the analysts did
    not clear), another miss, or empty."""
    short = short or (lambda x: x)
    right = right or (lambda t, c: t == c)
    out = {"cols": [short(c) for c in m["cols"]], "rows": []}
    for t in m["rows"]:
        total = sum(m["cells"][t].values())
        cells = []
        for c in m["cols"]:
            n = m["cells"][t].get(c, 0)
            kind = "zero" if not n else "hit" if right(t, c) else "danger" if danger and danger(t, c) else "miss"
            cells.append({"n": n, "kind": kind, "alpha": round(0.12 + 0.6 * n / total, 2) if n and total else 0})
        out["rows"].append({"label": short(t), "total": total, "cells": cells})
    return out


def _cleared_wrongly(truth, said):
    return truth != "clear" and said == "clear"


def _undecidable():
    """How the file's hits whose date of birth cannot settle them (a bare
    year that matches, a range, none) were decided."""
    c = Counter(outcome(r) for r in rows().values() if signals(r)["date of birth"] in ("same year", "year in range", "none"))
    return c


def overall(s):
    """The three answers together: the mean of the outcome, name and
    address scores (None when one is missing, as for the baselines)."""
    xs = [s.get("accuracy"), s.get("name_accuracy"), s.get("address_accuracy")]
    return round(sum(xs) / 3, 1) if all(x is not None for x in xs) else None


def _ablations(llms):
    """Plain lines on the runs that differ in one thing only: the same
    model with and without similar cases, or with two SOP versions."""
    out = []

    def said(r):
        s = r["summary"]
        return (f"{s['accuracy']}% of outcomes, the name {s['name_accuracy']}%, the address {s['address_accuracy']}%"
                + (f", {s['wrongly_cleared']} hit{'s' if s['wrongly_cleared'] != 1 else ''} cleared wrongly" if s["wrongly_cleared"] else ""))

    full = [r for r in llms if r["summary"]["errors"] <= 0.02 * r["summary"]["cases"]]     # a failed call or two is scored on the rest
    for a in full:
        for b in full:
            if a["model"] != b["model"] or a is b:
                continue
            if a["sop"] == b["sop"] and a["examples"] > b["examples"] == 0:
                out.append(f"Similar cases in the prompt ({a['label']}, SOP {a['sop']}): with {a['examples']}, {said(a)}; "
                           f"with none, {said(b)}.")
            if a["examples"] == b["examples"] and int(a["sop"][1:]) > int(b["sop"][1:]):
                out.append(f"The SOP tuning, on the held-out cases ({a['label']}): SOP {b['sop']}, {said(b)}; "
                           f"SOP {a['sop']}, {said(a)}.")
    return out


def view(result):
    """Everything the Model evaluation page shows, or None before a run."""
    if not result or not result.get("runs"):
        return None
    runs = result["runs"]
    test = [r for r in runs.values() if r["set"] == "test"]
    base = [r for r in test if r["kind"] != "llm"]
    for r in test:
        r["overall"] = overall(r["summary"])
    llms = sorted((r for r in test if r["kind"] == "llm" and r["per_case"]),
                  key=lambda r: (-(r["overall"] or 0), r["summary"].get("cost_per_case") or 0))
    table = sorted(base, key=lambda r: r["kind"] != "rules") + llms
    for r in table:
        s = r["summary"]
        r["matrix"] = matrix_view(s["outcome_matrix"], SHORT.get, _cleared_wrongly)
        r["title"] = r["label"] + (f" · SOP {r['sop']}, {r['examples']} similar cases" if r["kind"] == "llm" and r["examples"]
                                   else f" · SOP {r['sop']} alone" if r["kind"] == "llm" else "")
    complete = [r for r in llms if not r["summary"]["errors"] and len(r["per_case"]) == result["split"]["sizes"]["test"]]
    best = next((r for r in complete if r["summary"]["wrongly_cleared"] == 0), None)
    lines = []
    rules = next((r for r in base if r["kind"] == "rules"), None)
    tree = next((r for r in base if r["kind"] == "tree"), None)
    if rules and tree:
        lines.append(f"In this file the analysts' outcome follows the date of birth: the console's rules settle "
                     f"{rules['summary']['automated']}% of the {rules['summary']['cases']} held-out cases with no mistake and send "
                     f"the rest to a person; a decision tree learned from the 800 other cases gets {tree['summary']['accuracy']}% "
                     f"of the outcomes right. Where the models add to that is naming the discrepancies and writing the reasons.")
    if best:
        s = best["summary"]
        lines.append(f"Best language model (the three answers together, and no hit cleared wrongly): {best['title']}: "
                     f"{s['accuracy']}% of outcomes, the name discrepancy {s['name_accuracy']}% (the console's hand-written name rules: "
                     f"{rules['summary']['name_accuracy'] if rules else '—'}%), the address discrepancy {s['address_accuracy']}%, "
                     f"${s['cost_per_case']} a case (${s['cost_per_1000']} per 1,000).")
        near = [r for r in complete if r is not best and r["summary"]["wrongly_cleared"] == 0
                and (r["overall"] or 0) >= (best["overall"] or 0) - 2
                and (r["summary"].get("cost_per_case") or 1) < (s.get("cost_per_case") or 0)]
        if near:
            cheap = min(near, key=lambda r: r["summary"]["cost_per_case"])
            cs = cheap["summary"]
            lines.append(f"Within two points of it for less: {cheap['title']}: {cs['accuracy']}% of outcomes, the name "
                         f"{cs['name_accuracy']}%, the address {cs['address_accuracy']}%, at ${cs['cost_per_case']} a case "
                         f"(${cs['cost_per_1000']} per 1,000), {s['cost_per_case'] / cs['cost_per_case']:.1f}x less.")
        safe = [r for r in complete if r["summary"]["wrongly_cleared"] == 0 and r is not best and r["overall"] is not None]
        if safe:
            cheap = min(safe, key=lambda r: r["summary"]["cost_per_case"])
            cs = cheap["summary"]
            if cheap not in near and cs["cost_per_case"] < s["cost_per_case"]:
                lines.append(f"The cheapest with no hit cleared wrongly: {cheap['title']}: {cheap['overall']}% on the three "
                             f"together against {best['overall']}%, at ${cs['cost_per_case']} a case (${cs['cost_per_1000']} per "
                             f"1,000), {s['cost_per_case'] / cs['cost_per_case']:.0f}x less.")
    lines += _ablations(llms)
    cleared = [r["title"] for r in table if r["summary"]["wrongly_cleared"]]
    lines.append("No approach cleared a hit the analysts did not clear." if not cleared else
                 f"Cleared a hit the analysts did not clear (the dangerous mistake): {', '.join(cleared)}.")
    und = _undecidable()
    if und:
        lines.append(f"Ask the customer or escalate: of the {sum(und.values())} hits whose date of birth cannot settle them (a "
                     f"matching bare year, a range, none), the analysts asked the customer in {und.get('request_documents', 0)} "
                     f"and escalated {und.get('escalate', 0)}, with nothing in the file that tells the two apart, so every "
                     f"approach mixes them up. The SOP has to say which; from version 2 it asks the customer first.")
    tune = sorted((r for r in runs.values() if r["set"] == "tune" and r["kind"] == "llm"),
                  key=lambda r: (r["label"], r["sop"], r["examples"]))
    for r in tune:
        r["title"] = f"{r['label']} · SOP {r['sop']}" + (f", {r['examples']} similar cases" if r["examples"] else " alone")
    sops = [{"version": v, "text": sop_text(v), "changes": sop_changes(v)} for v in sop_versions()]
    errors = []
    if best:
        by_id = rows()
        for c in best["per_case"]:
            if c.get("error") or c["outcome"] == c["truth"]:
                continue
            row = by_id[c["case"]]
            errors.append({"case": c["case"], "name": row["customer_name"], "truth": SHORT[c["truth"]], "said": SHORT.get(c["outcome"], c["outcome"]),
                           "danger": _cleared_wrongly(c["truth"], c["outcome"]), "disposition": row["disposition_status"],
                           "note": row["compliance_action_regulatory_notes"], "reasons": c.get("reasons") or [],
                           "dob": c.get("dob_finding", "")})
    variance = {}
    for field in ("name", "address"):
        r = best if best else None
        if r and r["summary"].get(f"{field}_matrix"):
            idx = {lab: i for i, lab in enumerate(r["summary"][f"{field}_matrix"]["rows"], 1)}
            mv = matrix_view(r["summary"][f"{field}_matrix"], lambda x, idx=idx: str(idx[x]) if x in idx else x)
            variance[field] = {"accuracy": r["summary"][f"{field}_accuracy"], "confusions": r["summary"][f"{field}_confusions"],
                               "matrix": mv, "labels": list(idx)}
    if rules and rules["summary"].get("name_confusions"):
        variance["rules_name"] = {"accuracy": rules["summary"]["name_accuracy"], "confusions": rules["summary"]["name_confusions"]}
    return {"table": table, "best": best, "lines": lines, "tune": tune, "sops": sops, "errors": errors, "variance": variance,
            "tree": result.get("tree") or [], "split": result["split"], "llm_count": len(llms),
            "run_at": max((r.get("run_at", "") for r in test), default="")}


_loaded = {"mtime": None, "result": None}


def load_cached():
    """load(), read again only when the file changes (the page's view)."""
    try:
        mtime = os.path.getmtime(RESULTS)
    except OSError:
        return None
    if _loaded["mtime"] != mtime:
        _loaded.update(mtime=mtime, result=load())
    return _loaded["result"]


# ── the results file ─────────────────────────────────────────────────────────

def load():
    try:
        with open(RESULTS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save(result):
    tmp = RESULTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, RESULTS)


def rescore(result=None):
    """Score the stored runs again (the scoring changed, not the answers)."""
    result = result or load() or {}
    for run_ in (result.get("runs") or {}).values():
        if run_["kind"] == "llm":
            run_["summary"] = summarise(run_["per_case"], run_.get("price"))
    return baselines(result)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--sop", default=None, help="the SOP version (default: the latest)")
    ap.add_argument("--examples", type=int, default=DEFAULT_EXAMPLES)
    ap.add_argument("--set", default="test", choices=("test", "tune"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="call at most this many cases per model (a pilot)")
    ap.add_argument("--baselines", action="store_true", help="the rules and the tree only, no model call")
    ap.add_argument("--rescore", action="store_true", help="score the stored runs again, no model call")
    args = ap.parse_args()
    if args.rescore:
        res = rescore()
    elif args.baselines:
        res = baselines()
    else:
        res = baselines(run([m for m in args.models.split(",") if m], args.sop, args.examples, args.set, args.workers,
                            args.retry_failed, args.limit))
    for key, r in sorted(res["runs"].items()):
        s = r["summary"]
        print(f"{key:44} outcome {s['accuracy']}%  settled {s['settled_accuracy']}%  automated {s['automated']}%  "
              f"missed TP {s['missed_true_matches']}  wrongly cleared {s['wrongly_cleared']}  name {s.get('name_accuracy')}%  "
              f"address {s.get('address_accuracy')}%  ${s.get('cost_per_case', 0)}/case  errors {s['errors']}")
