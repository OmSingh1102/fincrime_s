"""The SOP's parameters, which the team lead changes on the SOP rules page
while the console runs: the proof of address's age limit and the documents
it does not apply to, the accepted proof-of-address types, the sanctioned or
prohibited countries, the two SLAs and whether OCR must read the passport's
MRZ. Every check reads them when it runs (workflow.poa_checks, the AI
Agent's checks, sla.active_due), so a change applies at once; the open cases
the agent decided and nobody has touched since are checked again on their
stored read (app._sop_recheck).

The defaults are the constants the console shipped with (fincrime/data/
vocab.py), so at the defaults every check, label and text reads as before.
One stored record (kind "settings", id "sop") holds the values, a version
the agent's decisions are stamped with, and the history of changes: who,
when, from what to what."""
import copy

from fincrime.data import vocab
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates

PARAMS = (
    {"key": "poa_max_age_days", "label": "Proof of address: issued within", "type": "int", "unit": "days",
     "min": 30, "max": 730, "default": vocab.POA_MAX_AGE_DAYS,
     "help": "A proof of address older than this fails its age check (183 days is the SOP's 6 months)."},
    {"key": "poa_no_age_limit", "label": "Proof of address with no age limit", "type": "poa_types",
     "default": list(vocab.POA_NO_AGE_LIMIT),
     "help": "Permanent cards that prove the address: their date of issue does not matter."},
    {"key": "accepted_poa_types", "label": "Accepted proof-of-address types", "type": "poa_types", "min_items": 1,
     "default": list(vocab.ACCEPTED_POA_TYPES),
     "help": "The internal library: any other document fails the type check."},
    {"key": "sanctioned_countries", "label": "Sanctioned or prohibited countries", "type": "countries",
     "default": list(vocab.SANCTIONED_OR_PROHIBITED),
     "help": "A proof of address from one fails; an ID and a proof of address both from one escalate; risk rises with them."},
    {"key": "sla_hours_l1", "label": "L1 SLA (Data Confirming and Data Screening)", "type": "int", "unit": "hours",
     "min": 1, "max": 240, "default": vocab.SLA_HOURS_L1,
     "help": "Counted from when the case was ingested; every open case's deadline moves with it."},
    {"key": "sla_days_edd", "label": "EDD SLA", "type": "int", "unit": "days", "min": 1, "max": 60,
     "default": vocab.SLA_HOURS_EDD // 24, "help": "Counted from the escalation to L2 / EDD."},
    {"key": "ocr_mrz_required", "label": "Passport MRZ must be readable by OCR", "type": "bool", "default": False,
     "help": "On: an uploaded passport whose MRZ Tesseract cannot read goes to a person. Off: shown, not failed."},
)
BY_KEY = {p["key"]: p for p in PARAMS}

_BOUND = {}


def default_settings():
    return {"id": "sop", "version": 0, "values": {p["key"]: copy.deepcopy(p["default"]) for p in PARAMS}, "history": []}


def configure(record):
    """Bind the console's stored record (app.SOP_SETTINGS)."""
    global _BOUND
    _BOUND = record


def value(key):
    values = _BOUND.get("values") or {}
    return copy.deepcopy(values[key]) if key in values else copy.deepcopy(BY_KEY[key]["default"])


def version():
    return int(_BOUND.get("version", 0))


# ── texts that name a value ──────────────────────────────────────────────────

def poa_age_text(days=None):
    """The age limit in words: whole months where it is one (183 days: "6
    months", the SOP's wording), else days."""
    days = value("poa_max_age_days") if days is None else days
    months = round(days / 30.44)
    if months >= 1 and abs(days - months * 30.44) <= 4:
        return f"{months} month" + ("s" if months != 1 else "")
    return f"{days} days"


def poa_age_label():
    return f"Issued within the last {poa_age_text()}"


def poa_old_reason():
    return f"The proof of address is older than {poa_age_text()}"


def resubmit_poa_reasons():
    """The Resubmit POA reasons, the age one in the current limit's words."""
    return [poa_old_reason()] + [r for r in vocab.RESUBMIT_POA_REASONS if not r.startswith("The proof of address is older than")]


def poa_type_labels(keys):
    return [vocab.ACCEPTED_POA_TYPES.get(k, k) for k in keys]


def shown(param, val):
    if param["type"] == "int":
        return f"{val} {param['unit']}"
    if param["type"] == "bool":
        return "on" if val else "off"
    if param["type"] == "poa_types":
        return ", ".join(poa_type_labels(val)) or "none"
    return ", ".join(vocab.COUNTRY_NAMES.get(c, c) for c in val) or "none"


def rows():
    """Every parameter with its value and default as shown, for the page."""
    return [dict(p, value=value(p["key"]), shown=shown(p, value(p["key"])), default_shown=shown(p, p["default"]),
                 changed=value(p["key"]) != p["default"]) for p in PARAMS]


# ── changing one ─────────────────────────────────────────────────────────────

def _parse(param, form):
    kind = param["type"]
    if kind == "int":
        raw = (form.get("value") or "").strip()
        if not raw.isdigit():
            raise WorkflowError(f"{param['label']}: give a whole number of {param['unit']}.")
        val = int(raw)
        if not param["min"] <= val <= param["max"]:
            raise WorkflowError(f"{param['label']}: between {param['min']} and {param['max']} {param['unit']}.")
        return val
    if kind == "bool":
        return (form.get("value") or "").strip().lower() in ("1", "true", "on", "yes")
    picked = form.getlist("value") if hasattr(form, "getlist") else list(form.get("value") or [])
    known = vocab.ACCEPTED_POA_TYPES if kind == "poa_types" else vocab.COUNTRY_NAMES
    unknown = [x for x in picked if x not in known]
    if unknown:
        raise WorkflowError(f"{param['label']}: not one the console knows: {', '.join(unknown)}.")
    val = [x for x in known if x in picked]                 # the catalogue's order, once each
    if len(val) < param.get("min_items", 0):
        raise WorkflowError(f"{param['label']}: keep at least {param['min_items']}.")
    return val


def set_value(key, form, actor):
    """Change one parameter from a submitted form; a WorkflowError says
    why not. Returns (param, shown before, shown after)."""
    param = BY_KEY.get(key)
    if param is None:
        raise WorkflowError("Unknown SOP rule.")
    new, old = _parse(param, form), value(key)
    if new == old:
        raise WorkflowError(f"{param['label']} is already {shown(param, new)}.")
    _BOUND.setdefault("values", {})[key] = new
    _BOUND["version"] = version() + 1
    before, after = shown(param, old), shown(param, new)
    _BOUND.setdefault("history", []).append({"at": dates.now_str(), "by": actor, "key": key, "label": param["label"],
                                             "from": before, "to": after})
    return param, before, after


configure(default_settings())           # until the console binds its stored record
