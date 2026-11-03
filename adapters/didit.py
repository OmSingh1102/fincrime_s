"""Didit ID verification: an uploaded ID checked by Didit's standalone ID
Verification API (is it genuine, unaltered, valid: screen captures, printed
copies, a replaced portrait, covered fields, the MRZ, expiry), with the API
key entered on the Settings page. Nothing about the account is in the code;
without a key no case is sent to Didit.

Written from Didit's public API reference (POST /v3/id-verification/, a
multipart upload with the x-api-key header) and NOT YET RUN against the
service (no account so far): the base URL and the path are settings. A
sandbox key answers every call with a fixed "Approved" sample ("Sandbox
User", SANDBOX-0001) without looking at the image: it is kept as a sandbox
answer, free, and never counts as an approval. Face match
and liveness need a selfie, which an uploaded ID does not bring: they are
not called.

The answer kept with the case: Approved or Declined, the warnings (each
with whether it declines), the document type, the name, date of birth and
number Didit read, and the photo-quality scores.
"""
import json
import secrets
import time
import urllib.error
import urllib.request

DEFAULTS = {"api_key": "", "base_url": "https://verification.didit.me", "path": "/v3/id-verification/", "auto": True,
            "document_liveness": True, "price_per_call": 0.20}
TIMEOUT = 120


class DiditError(Exception):
    """A call Didit refused or did not answer; the message says why."""


def configured(cfg):
    return bool((cfg or {}).get("api_key"))


def _send(req):
    """(status, body bytes) of one request (the seam the tests stand in for)."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def multipart(fields, files):
    """(body, content type) of a multipart/form-data request."""
    boundary = "----fcc" + secrets.token_hex(12)
    out = []
    for name, value in fields.items():
        out += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("utf-8"),
                str(value).encode("utf-8"), b"\r\n"]
    for name, (filename, data, mime) in files.items():
        out += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n".encode("utf-8"), data, b"\r\n"]
    out.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


_REFUSED = {401: ("Didit does not accept the API key (401): it is wrong, revoked or expired. In the Business Console, "
                  "Developers > API keys of the live application: create a key, copy it at once, and save it on Settings."),
            403: "Didit refused the call (403): a wrong or revoked key, a key without access to sessions, or no credits left.",
            429: "Didit's rate limit (429): try again in a minute."}


def verify(cfg, jpeg, reference=""):
    """Didit's verdict on one ID image, as kept with the case."""
    body, ctype = multipart({"perform_document_liveness": "true" if cfg.get("document_liveness", True) else "false",
                             "vendor_data": reference, "save_api_request": "true"},
                            {"front_image": ("id.jpg", jpeg, "image/jpeg")})
    url = cfg["base_url"].rstrip("/") + "/" + cfg["path"].lstrip("/")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"x-api-key": cfg["api_key"], "Content-Type": ctype, "Accept": "application/json"})
    started = time.monotonic()
    try:
        status, raw = _send(req)
    except (urllib.error.URLError, OSError) as exc:
        raise DiditError(f"Didit did not answer ({type(exc).__name__}): {str(exc)[:160]}") from exc
    if status == 403 and b"enough credits" in raw:
        raise DiditError("Didit accepted the key, but the account has no credits left (403): top up in the Business Console, "
                         "Settings > Organization > Billing and plan > Top up ($50 minimum; a check is about $0.20).")
    if status in _REFUSED:
        raise DiditError(_REFUSED[status] + f" {raw[:160].decode('utf-8', 'replace')}")
    if status == 400:
        raise DiditError(f"Didit could not use the image (400): {raw[:200].decode('utf-8', 'replace')}")
    if status >= 400:
        raise DiditError(f"Didit answered {status}: {raw[:200].decode('utf-8', 'replace')}")
    return summary(json.loads(raw), float(cfg.get("price_per_call") or 0), round(time.monotonic() - started, 1))


def is_sandbox(v):
    """Is this the sandbox's fixed sample ("Sandbox User", SANDBOX-0001) rather
    than a check of the image? (Didit's answer, or the summary kept of it.)"""
    return "sandbox" in f"{v.get('full_name') or ''} {v.get('document_number') or ''}".lower()


def summary(answer, price=0.0, seconds=0.0):
    """The parts of Didit's answer kept with the case."""
    v = answer.get("id_verification") or {}
    q = v.get("front_image_quality_score") or {}
    sandbox = is_sandbox(v)
    return {"provider": "Didit", "status": v.get("status") or "Unknown", "request_id": answer.get("request_id", ""), "sandbox": sandbox,
            "document_type": v.get("document_type") or "", "full_name": v.get("full_name") or "",
            "date_of_birth": v.get("date_of_birth") or "", "document_number": v.get("document_number") or "",
            "expiration_date": v.get("expiration_date") or "", "issuing_state": v.get("issuing_state_name") or v.get("issuing_state") or "",
            "warnings": [{"risk": w.get("risk", ""), "declines": w.get("log_type") == "error",
                          "text": w.get("short_description") or w.get("risk", "")} for w in v.get("warnings") or []],
            "quality": {k: q.get(k) for k in ("overall_score", "focus_score", "brightness_issue", "is_document_fully_visible")} if q else {},
            "cost": round(price, 4) if v.get("status") in ("Approved", "Declined") and not sandbox else 0.0, "seconds": seconds}
