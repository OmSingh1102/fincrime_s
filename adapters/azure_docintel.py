"""Azure AI Document Intelligence: an uploaded ID read by Microsoft's
prebuilt ID-document model, with the endpoint and key entered on the
Settings page. Nothing about the account is in the code; without them the
console's AI model reads the ID instead (documents/id_scan).

Written from Microsoft's public REST reference (Document Intelligence v4,
api-version 2024-11-30, model prebuilt-idDocument) and NOT YET RUN against
the service (no Azure resource so far): the API version and the model are
settings. "Test connection" on the Settings page lists the resource's
models, which a working endpoint and key answer.

The read is asynchronous: the image goes in a POST (base64Source), the
answer comes from polling the Operation-Location it returns. The fields are
mapped to the read Scan ID's own model returns, so the same checks (the MRZ
check digits, expiry, age) run on it; a field read with a confidence under
the setting's floor is marked unsure. Azure does not judge the photo itself
(blur, glare): that stays with the checks and the analyst.
"""
import base64
import json
import time
import urllib.error
import urllib.request

DEFAULTS = {"endpoint": "", "api_key": "", "api_version": "2024-11-30", "model_id": "prebuilt-idDocument",
            "min_confidence": 0.8, "price_per_page": 0.01}
TIMEOUT = 30
POLL_EVERY, POLL_FOR = 1.0, 60.0
MODEL = {"model": "azure-docintel", "label": "Azure AI Document Intelligence"}

_DOC_TYPES = {"passport": "Passport", "nationalidentitycard": "National ID card", "driverlicense": "Driving licence",
              "residencepermit": "Residence permit"}


class AzureError(Exception):
    """A call Azure refused or did not answer; the message says why."""


def configured(cfg):
    return bool((cfg or {}).get("endpoint") and (cfg or {}).get("api_key"))


def _base(cfg):
    return cfg["endpoint"].rstrip("/") + "/documentintelligence"


def _send(req):
    """(status, headers, body bytes) of one request (the seam the tests stand in for)."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _call(cfg, method, url, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Ocp-Apim-Subscription-Key": cfg["api_key"], "Content-Type": "application/json"})
    try:
        status, headers, raw = _send(req)
    except (urllib.error.URLError, OSError) as exc:
        raise AzureError(f"Azure did not answer ({type(exc).__name__}): {str(exc)[:160]}") from exc
    if status in (401, 403):
        raise AzureError(f"Azure refused the key ({status}): check the key and the endpoint on Settings.")
    if status >= 400:
        raise AzureError(f"Azure answered {status}: {raw[:200].decode('utf-8', 'replace')}")
    return status, {k.lower(): v for k, v in headers.items()}, (json.loads(raw) if raw else {})


def models(cfg):
    """The resource's models (Test connection)."""
    _, _, body = _call(cfg, "GET", f"{_base(cfg)}/documentModels?api-version={cfg['api_version']}")
    return [m.get("modelId", "") for m in body.get("value", [])]


def analyze(cfg, jpeg, sleep=time.sleep):
    """Azure's analyzeResult for one image."""
    url = f"{_base(cfg)}/documentModels/{cfg['model_id']}:analyze?api-version={cfg['api_version']}"
    _, headers, _ = _call(cfg, "POST", url, {"base64Source": base64.b64encode(jpeg).decode("ascii")})
    where = headers.get("operation-location")
    if not where:
        raise AzureError("Azure accepted the image but gave no Operation-Location to collect the result from.")
    waited = 0.0
    while waited <= POLL_FOR:
        _, _, body = _call(cfg, "GET", where)
        if body.get("status") == "succeeded":
            return body.get("analyzeResult") or {}
        if body.get("status") == "failed":
            raise AzureError(f"Azure could not analyse the image: {json.dumps(body.get('error') or {})[:200]}")
        sleep(POLL_EVERY)
        waited += POLL_EVERY
    raise AzureError(f"Azure had no result after {int(POLL_FOR)} s.")


def _value(field):
    if not field:
        return ""
    for key in ("valueString", "valueDate", "valueCountryRegion", "valueGender", "valuePhoneNumber"):
        if field.get(key):
            return str(field[key])
    if field.get("valueAddress"):
        return field.get("content", "")
    return str(field.get("content") or "")


_FIELDS = {"surname": "LastName", "given_names": "FirstName", "date_of_birth": "DateOfBirth", "sex": "Sex",
           "nationality": "Nationality", "document_number": "DocumentNumber", "personal_number": "PersonalNumber",
           "date_of_issue": "DateOfIssue", "date_of_expiry": "DateOfExpiration", "place_of_birth": "PlaceOfBirth",
           "address": "Address", "issuing_authority": "IssuingAuthority"}


def to_read(result, field_keys, min_confidence=0.8):
    """Azure's analyzeResult as Scan ID's read: the document type and
    country, every field (fields Azure does not return left empty), the MRZ
    lines, the fields under the confidence floor as unsure."""
    docs = result.get("documents") or []
    if not docs:
        return {"document_type": "Not an identity document", "issuing_country": "", "readable": False, "problems": [],
                "fields": {k: "" for k in field_keys}, "mrz": [], "uncertain": [], "note": "Azure found no identity document."}
    doc = docs[0]
    f = doc.get("fields") or {}
    fields = {k: "" for k in field_keys}
    unsure = []
    for key, name in _FIELDS.items():
        if key in fields and f.get(name):
            fields[key] = _value(f[name]).strip()
            if (f[name].get("confidence") or 1) < min_confidence:
                unsure.append(key)
    fields["full_name"] = " ".join(x for x in (fields["given_names"], fields["surname"]) if x)
    mrz = [x.strip() for x in str((f.get("MachineReadableZone") or {}).get("content") or "").replace(" ", "").splitlines() if x.strip()]
    kind = (doc.get("docType") or "").split(".")[-1].lower()
    return {"document_type": _DOC_TYPES.get(kind, kind or "ID document"), "issuing_country": _value(f.get("CountryRegion")),
            "readable": bool(fields["full_name"] or fields["document_number"]), "problems": [], "fields": fields,
            "mrz": mrz, "uncertain": unsure, "note": "", "photo_judged": False}


def read(cfg, jpeg, field_keys, sleep=time.sleep):
    """(read, model record) for one image: Azure's read in Scan ID's shape,
    and what it took (seconds; the cost is the page price on Settings)."""
    started = time.monotonic()
    result = analyze(cfg, jpeg, sleep)
    pages = max(1, len(result.get("pages") or []) or 1)
    price = float(cfg.get("price_per_page") or 0)
    return (to_read(result, field_keys, float(cfg.get("min_confidence") or 0.8)),
            dict(MODEL, tokens_in=0, tokens_out=0, cost=round(price * pages, 4), seconds=round(time.monotonic() - started, 1)))
