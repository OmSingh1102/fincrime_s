"""LSEG World-Check One API: screen the person on an uploaded ID, with the
API key, secret, group, host and paths entered on the Settings page. Nothing
about the account is in the code, and without a key and secret the console
screens against the stand-in watchlist instead (fincrime/watchlist.py).

Written from LSEG's public description of the API and NOT YET RUN against
the service (no licence so far): the host, the base path and the paths of
the screening, groups and profile calls are settings, to be checked against
the documentation that comes with the licence. "Test connection" on the
Settings page calls the groups list, which a working key and secret answer.

Each request is signed the way World-Check One asks: an HMAC-SHA256, keyed
with the API secret, over the request target, the host and the date (and,
for a POST, the content type, the content length and the body), base64,
sent as an HTTP Signature in the Authorization header. The secret itself is
never sent.
"""
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request
from email.utils import formatdate

DEFAULTS = {"api_key": "", "api_secret": "", "group_id": "", "host": "api-worldcheck.refinitiv.com",
            "base_path": "/v2/", "screen_path": "cases/screeningRequest", "groups_path": "groups",
            "profile_path": "reference/profile/{id}"}
TIMEOUT = 30


class WorldCheckError(Exception):
    """A call World-Check One refused or did not answer; the message says why."""


def configured(cfg):
    return bool((cfg or {}).get("api_key") and (cfg or {}).get("api_secret"))


def _path(cfg, name, **kw):
    base = "/" + (cfg.get("base_path") or "/").strip("/") + "/"
    return (base + (cfg.get(name) or DEFAULTS[name]).lstrip("/").format(**kw)).replace("//", "/")


def signed_headers(cfg, method, path, body=b"", date=None):
    """The headers of one signed request (date: an RFC 1123 date, now by default)."""
    date = date or formatdate(usegmt=True)
    lines = [f"(request-target): {method.lower()} {path}", f"host: {cfg['host']}", f"date: {date}"]
    names = "(request-target) host date"
    headers = {"Date": date, "Accept": "application/json"}
    if method.upper() == "POST":
        lines += ["content-type: application/json", f"content-length: {len(body)}", body.decode("utf-8")]
        names += " content-type content-length"
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
    mac = hmac.new(cfg["api_secret"].encode("utf-8"), "\n".join(lines).encode("utf-8"), hashlib.sha256).digest()
    headers["Authorization"] = (f'Signature keyId="{cfg["api_key"]}",algorithm="hmac-sha256",headers="{names}",'
                                f'signature="{base64.b64encode(mac).decode("ascii")}"')
    return headers


def _send(req):
    """(status, body bytes) of one request (the seam the tests stand in for)."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _call(cfg, method, path, payload=None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(f"https://{cfg['host']}{path}", data=body or None, method=method,
                                 headers=signed_headers(cfg, method, path, body))
    try:
        status, raw = _send(req)
    except (urllib.error.URLError, OSError) as exc:
        raise WorldCheckError(f"World-Check One did not answer ({type(exc).__name__}): {str(exc)[:160]}") from exc
    if status == 401:
        raise WorldCheckError("World-Check One refused the key and secret (401): check them, and that the clock of this machine is right.")
    if status >= 400:
        raise WorldCheckError(f"World-Check One answered {status}: {raw[:200].decode('utf-8', 'replace')}")
    try:
        return json.loads(raw) if raw else {}
    except ValueError as exc:
        raise WorldCheckError("World-Check One's answer was not JSON.") from exc


def groups(cfg):
    """The account's groups [(id, name)]: what "Test connection" shows."""
    data = _call(cfg, "GET", _path(cfg, "groups_path"))
    items = data if isinstance(data, list) else data.get("groups") or data.get("results") or []
    out = []
    for g in items:
        out.append((g.get("id", ""), g.get("name", "")))
        out += [(c.get("id", ""), c.get("name", "")) for c in g.get("children") or []]
    return out


def _secondary(person):
    fields = []
    sex = (person.get("sex") or "").upper()[:1]
    if sex in ("M", "F"):
        fields.append({"typeId": "SFCT_1", "value": "MALE" if sex == "M" else "FEMALE"})
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", person.get("dob") or ""):
        fields.append({"typeId": "SFCT_2", "dateTimeValue": person["dob"]})
    nat = (person.get("nationality") or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", nat):
        fields.append({"typeId": "SFCT_5", "value": nat})
    return fields


def _birth(result):
    for e in result.get("events") or []:
        if (e.get("type") or "").upper() == "BIRTH":
            if e.get("fullDate"):
                return str(e["fullDate"])[:10]
            if e.get("year"):
                return f"{e['year']} (year only)"
    return "Not listed"


def _places(result):
    out = []
    for link in result.get("countryLinks") or []:
        c = link.get("country") or {}
        name = c.get("name") or link.get("countryText") or ""
        if name:
            out.append(f"{name.title()} ({(link.get('type') or '').lower().replace('_', ' ')})".replace(" ()", ""))
    return "; ".join(dict.fromkeys(out)) or "Not listed"


def _field_result(result, type_id):
    for f in result.get("secondaryFieldResults") or []:
        if (f.get("typeId") or (f.get("field") or {}).get("typeId")) == type_id:
            return {"MATCHED": "Matched", "NOT_MATCHED": "Not matched"}.get(f.get("fieldResult", ""), "Unknown")
    return "Unknown"


def _hit(result, profile=None):
    strength = (result.get("matchStrength") or "").title() or "Unknown"
    details = [d.get("text", "") for d in (profile or {}).get("details") or [] if d.get("text")]
    return {"uid": result.get("referenceId") or result.get("resultId", ""), "result_id": result.get("resultId", ""),
            "name": result.get("primaryName") or result.get("matchedTerm", ""),
            "aka": [t for t in [result.get("matchedTerm")] if t and t != result.get("primaryName")],
            "dob_text": _birth(result), "places": _places(result),
            "category": ", ".join(result.get("categories") or []) or result.get("category", ""),
            "narrative": " ".join(details)[:1500] or "See the World-Check One profile.",
            "score": {"Exact": 100, "Strong": 90, "Medium": 80, "Weak": 70}.get(strength, 0), "strength": strength,
            "dob_result": _field_result(result, "SFCT_2"), "country_result": _field_result(result, "SFCT_5"),
            "source": "World-Check One", "row": ""}


def screen(cfg, person, limit=5):
    """Screen the person: World-Check One's results, best first, at most
    `limit`, each with its profile's further information when the profile
    call answers. Returns (hits, case_system_id)."""
    payload = {"groupId": cfg.get("group_id", ""), "entityType": "INDIVIDUAL", "providerTypes": ["WATCHLIST"],
               "name": person.get("name", ""), "secondaryFields": _secondary(person), "customFields": []}
    data = _call(cfg, "POST", _path(cfg, "screen_path"), payload)
    order = {"Exact": 0, "Strong": 1, "Medium": 2, "Weak": 3}
    results = sorted(data.get("results") or [], key=lambda r: order.get((r.get("matchStrength") or "").title(), 4))[:limit]
    hits = []
    for res in results:
        prof = None
        if res.get("referenceId"):
            try:
                prof = _call(cfg, "GET", _path(cfg, "profile_path", id=res["referenceId"]))
            except WorldCheckError:
                prof = None                 # the result stands without the profile's text
        hits.append(_hit(res, prof))
    return hits, data.get("caseSystemId", "")
