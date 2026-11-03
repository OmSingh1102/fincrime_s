"""FinCrime KYC/KYB Operations Hub — demo.

One Flask module, thin routes, all logic in the fincrime/ package. Built the
way the ChargeShield demo is built: no factory, no blueprints, demo data
loaded at import so the reloader's two processes agree, a SQLite state store
that survives a restart, one stylesheet, no JS framework.

Sections, in order: Modular imports · App Setup · Sign-in, analysts and roles
· Tenant · Nav tables · State · Context processor · Auth routes · KYC case
management · Documents · Name screening · World-Check (simulated) · Queues ·
Operations hub (My Work, Pipeline, Trackers, Investigation file, Audit log,
QC, Routing) · Analytics · Demo affordances · Main.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

try:  # A deployed copy reads .env (see .env.example); a dev checkout has none.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# ─── Modular imports ───────────────────────────────────────────────────────────
from fincrime.adapters import azure_docintel, didit, idv_vendor, world_check
from fincrime.analytics.audit_log import AuditLogConsole
from fincrime.analytics.case_list import PAGE_SIZES, PER_PAGE, CaseListConsole, read_query as read_case_query
from fincrime.analytics.ai_console import AiScreeningConsole
from fincrime.analytics.dashboard import AnalyticsConsole
from fincrime.analytics.pipeline import PipelineConsole
from fincrime.analytics.queues import QueueConsole
from fincrime.analytics.routing_console import RoutingConsole
from fincrime.analytics.trackers import TrackerConsole, to_csv
from fincrime.analytics.work import WorkConsole
from fincrime import benchmark, decision_model, id_screening, upload_eval, watchlist
from fincrime.data import vocab
from fincrime.data import recon_book
from fincrime.data.loader import SEED_VERSION, book_from, index_by_id, load_seed, relabel
from fincrime.data.state_store import StateStore
from fincrime.documents import id_scan, ocr, svg_cards, uploads
from fincrime.engines import (ai_agent, ai_review, ai_screening, case_documents, investigation, qc, recommendation, routing, screening,
                              segregation, sla, sop, worldcheck, workflow)
from fincrime.engines.errors import WorkflowError
from fincrime.engines.wc_report_pdf import render_wc_export
from fincrime.utils import dates
from fincrime.utils.text import display_name, initials, translit

# ─── App Setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fincrime-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024       # a CSV with a ZIP of its cases' ID images

# The state store must never raise — its restore runs at import, so an
# unwritable or locked database would take the app down rather than merely
# losing a decision. It reports here instead.
StateStore.on_error = app.logger.warning

DEBUG = os.environ.get("DEBUG", "1") == "1"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8010"))


def _who(value):
    """An email as a display name; anything else (system, FCC Router) as is."""
    return display_name(value) if "@" in (value or "") else (value or "-")


# Jinja *globals*, not context-processor values: a macro imported with
# {% from "_badges.html" import ... %} does not see the render context, but it
# does see globals, and the badge macros need the status colour map.
app.jinja_env.globals.update(status_tone=vocab.STATUS_TONE, vocab=vocab,
                             icon=app.jinja_env.get_template("_icon.html").module.icon,
                             ai_summary=ai_screening.summary, shown_workflow=ai_screening.shown_workflow, sop=sop)
app.jinja_env.filters.update(wc_date=dates.wc_date, wc_dt=dates.wc_datetime, who=_who, translit=translit)

# ─── Sign-in, analysts and roles ───────────────────────────────────────────────
# Demo gate only: one shared password, a profile picker, the session cookie
# signed with the dev secret above. It decides which analyst the app records
# as the actor — it is not a security control.
DEMO_PASSWORD = os.environ.get("STRAIVE_DEMO_PASSWORD", "1234")
DEMO_MODE = os.environ.get("DEMO_MODE", "1") == "1"
# The console's case book: "screening" (the default) starts with no case and
# makes one of every ID uploaded on Scan ID, screened against World-Check
# (the World-Check records of the reconciliation log until a World-Check One
# key is entered on Settings); "file" is the 1,000 cases of the
# reconciliation log the client sent; "demo" is the nineteen scripted
# scenarios and their filler, which the test suite and the original
# walkthrough use. Read once; /reset reloads the same book.
CASE_BOOK = book_from(os.environ.get("CONSOLE_CASES"))
CASE_BOOK_LABEL = {"screening": "ID screening (no case until an ID is uploaded)",
                   "file": "reconciliation log (1,000 cases)", "demo": "scripted demo (19 scenarios + filler)"}
# The screening book keeps its own database, so its empty start never
# touches the work stored on the 1,000 cases (switching books clears the
# console's records of the database it opens).
SCREENING_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fincrime_screening.db")
if CASE_BOOK == "screening" and not os.environ.get("FCC_STATE_DB"):
    StateStore.path = SCREENING_DB


def _home_endpoint():
    return "scan_id" if CASE_BOOK == "screening" else "kyc_list"
# The records the console owns in the state store; a new seed stamp clears
# only these.
CONSOLE_KINDS = ("kyc", "alert", "wc", "analyst", "settings", "upload")
# Only advertise the password on the card while it is the built-in one.
_SHOW_DEMO_HINT = "STRAIVE_DEMO_PASSWORD" not in os.environ

ROLE_LABEL = {"L1": "L1 Analyst", "L2": "L2 Analyst", "EDD": "EDD Analyst", "QC": "QC Lead"}
PUBLIC_ENDPOINTS = {"portal", "login", "logout", "static", "api_cases", "api_case"}     # the API checks its own key

ANALYSTS = []            # filled by _load_seed
ANALYST_BY_KEY = {}
ANALYST_BY_EMAIL = {}


def _analyst():
    return ANALYST_BY_KEY.get(session.get("user"))


def _actor() -> str:
    return session.get("email", "")


def _levels() -> set:
    return set(session.get("roles") or [])


def _analysts_with(levels, tenant) -> str:
    """'Amara Iyer, Jon Miranda or Mario Kim': who holds one of `levels` on
    `tenant`, for the hints that say who can act."""
    names = [a["name"] for a in ANALYSTS if set(levels) & set(a["roles"]) and tenant in a["tenants"]]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def _check_password(password: str) -> bool:
    return hmac.compare_digest((password or "").encode("utf-8"), DEMO_PASSWORD.encode("utf-8"))


def _safe_next(target: str, fallback=None) -> str:
    """Only follow same-site paths. Rejects absolute and protocol-relative URLs."""
    if target and target.startswith("/") and target[1:2] not in ("/", "\\"):
        return target
    return fallback or url_for(_home_endpoint())


def _back(fallback=None) -> str:
    """The page the request came from, when it is one of ours; else home. A
    refused action leaves the analyst where they were, with the reason shown
    as a notification (see _flashes.html)."""
    ref = urlsplit(request.referrer or "")
    if ref.scheme in ("http", "https") and ref.netloc == request.host and ref.path.startswith("/"):
        return ref.path + (f"?{ref.query}" if ref.query else "")
    return fallback or url_for("kyc_list")


def role_required(*levels):
    """Restrict a view to analysts holding one of the given levels. A
    signed-in analyst without the level is sent back to the page they came
    from (home when unknown) with the reason as a notification — the nav
    never offered them the link, but a shared button such as Reserve can
    still be pressed."""
    allowed = set(levels)

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            if allowed and not (_levels() & allowed):
                flash(f"That action needs {' or '.join(sorted(allowed))} access.", "error")
                return redirect(_back())
            return view(*args, **kwargs)
        return wrapper
    return decorator


@app.before_request
def _require_sign_in():
    """Everything outside PUBLIC_ENDPOINTS needs a session. One hook rather
    than a decorator on each route: easier to audit, impossible to forget."""
    if request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not session.get("user") or session.get("user") not in ANALYST_BY_KEY:
        for key in ("user", "email", "name", "role", "roles", "tenant"):
            session.pop(key, None)
        return redirect(url_for("portal"))
    # A deep link may name the tenant, the way the real tool's URLs do.
    wanted = request.args.get("tenant")
    if wanted in vocab.TENANTS and wanted in (_analyst() or {}).get("tenants", []):
        session["tenant"] = wanted
    return None


# ─── Tenant ────────────────────────────────────────────────────────────────────
def _tenant() -> str:
    a = _analyst() or {}
    allowed = a.get("tenants") or ["ttlive"]
    t = session.get("tenant")
    return t if t in allowed else allowed[0]


# ─── Nav tables ────────────────────────────────────────────────────────────────
TOP_TABS = [
    {"label": "Case Management", "endpoint": "kyc_list"},
]

# Which side-nav entry a page belongs to, by endpoint prefix. Order matters:
# `kyc_` catches kyc_file / kyc_qc_review before the hub prefixes.
_NAV_OWNER = (("kyc_", "kyc_list"), ("document", "kyc_list"), ("ns_", "ns_queue"), ("wc_", "ns_queue"),
              ("queue_", "queue_l2"), ("analytics", "analytics"),
              ("work", "work"), ("pipeline", "pipeline"), ("trackers", "trackers"), ("audit_log", "audit_log"),
              ("ai_", "ai_screening"), ("qc_", "qc_sampling"), ("routing_", "routing_console"), ("sop_", "sop_page"), ("architecture", "architecture"), ("model_evaluation", "model_evaluation"), ("scan", "scan_id"),
              ("screening_sop", "screening_sop"), ("screening_settings", "screening_settings"))


def _nav_owner(endpoint: str) -> str:
    for prefix, owner in _NAV_OWNER:
        if (endpoint or "").startswith(prefix):
            return owner
    return ""


def _qc_pending_count():
    """Memoised per request: the nav and the QC page both ask."""
    if not hasattr(g, "qc_pending"):
        g.qc_pending = len(qc.pending(KYC_CASES, _tenant()))
    return g.qc_pending


def _side_nav():
    """The left menu in sections: the cases an analyst works, the screening,
    the hub SOP 5.2 asks for on top of the as-is screens, the reports."""
    owner = _nav_owner(request.endpoint)
    setup = [
        {"label": "Screening SOP", "title": "The SOP the AI decides World-Check hits by", "icon": "book",
         "endpoint": "screening_sop", "active": owner == "screening_sop"},
        {"label": "Settings", "title": "World-Check connection, models, search", "icon": "sliders",
         "endpoint": "screening_settings", "active": owner == "screening_settings"},
    ]
    insights = [
        {"label": "Architecture", "icon": "layers", "endpoint": "architecture", "active": owner == "architecture"},
        {"label": "Model evaluation", "icon": "activity", "endpoint": "model_evaluation", "active": owner == "model_evaluation"}]
    if CASE_BOOK == "screening":
        return [
            {"title": "Screening", "items": [
                {"label": "Cases", "title": "Upload an ID, screen it on World-Check", "icon": "user-check", "endpoint": "scan_id",
                 "active": owner == "scan_id", "count": sum(1 for s in _scans() if s.get("status") == "open")}]},
            {"title": "Setup", "items": setup},
            {"title": "Insights", "items": insights},
        ]
    levels = _levels()
    tenant = _tenant()
    actor = _actor()
    open_l1 =sum(1 for c in KYC_CASES if c["tenant"] == tenant and c["status"] in ("DataConfirming", "DataScreening"))
    open_l2 = sum(1 for c in KYC_CASES if c["tenant"] == tenant and c["status"] == "DataEscalatingL2"
                  and not segregation.hidden_from(c, actor, "L2"))
    cases = [
        {"label": "Due Diligence", "title": "Due Diligence (KYC/KYB)", "icon": "user-check", "endpoint": "kyc_list",
         "active": owner == "kyc_list", "count": open_l1},
        {"label": "My Work", "icon": "clipboard", "endpoint": "work", "active": owner == "work",
         "count": routing.load(actor, KYC_CASES) if actor else 0},
        {"label": "Scan ID", "title": "Scan an ID document (OCR)", "icon": "zoom-in", "endpoint": "scan_id",
         "active": owner == "scan_id"},
    ]
    if levels & {"L2", "EDD", "QC"}:
        cases.append({"label": "L2 / EDD Queue", "icon": "layers", "endpoint": "queue_l2", "active": owner == "queue_l2", "count": open_l2})
    screening = [
        {"label": "Name Screening", "icon": "search", "endpoint": "ns_queue", "active": owner == "ns_queue"},
        {"label": "AI Screening", "icon": "check-circle", "endpoint": "ai_screening_page", "active": owner == "ai_screening"},
        {"label": "SOP rules", "icon": "sliders", "endpoint": "sop_page", "active": owner == "sop_page"},
    ] + setup
    hub = [
        {"label": "Pipeline", "icon": "trending-up", "endpoint": "pipeline", "active": owner == "pipeline"},
        {"label": "Trackers", "icon": "list", "endpoint": "trackers", "active": owner == "trackers"},
        {"label": "Audit Log", "icon": "book", "endpoint": "audit_log", "active": owner == "audit_log"},
    ]
    if "QC" in levels:
        hub.append({"label": "QC Sampling", "icon": "shield-check", "endpoint": "qc_sampling", "active": owner == "qc_sampling",
                    "count": _qc_pending_count()})
    if levels & {"L2", "QC"}:
        hub.append({"label": "Routing", "icon": "sliders", "endpoint": "routing_console", "active": owner == "routing_console",
                    "count": len(routing.unassigned(tenant, KYC_CASES))})
    return [
        {"title": "Cases", "items": cases},
        {"title": "Screening", "items": screening},
        {"title": "Operations Hub", "items": hub},
        {"title": "Insights", "items": [
            {"label": "Analytics", "icon": "bar-chart", "endpoint": "analytics", "active": owner == "analytics"}] + insights},
    ]


# ─── State ─────────────────────────────────────────────────────────────────────
# Bumped on every write; polled by _live_refresh.html so a second screen
# notices a change made on the first.
STATE_VERSION = 0

KYC_CASES, ALERT_CASES, WC_CASES = [], [], []
KYC_BY_ID, ALERT_BY_ID, WC_BY_ID = {}, {}, {}
AI_SETTINGS = {}         # the AI screening rules' switches; one stored record, kind "settings"
SOP_SETTINGS = {}        # the SOP's parameters (engines/sop); one stored record, kind "settings", id "sop"
SCREEN_SETTINGS = {}     # ID screening: models, search, World-Check account (id_screening); kind "settings", id "screening"


def _bump_state():
    global STATE_VERSION
    STATE_VERSION += 1


def _rebuild_indexes():
    KYC_BY_ID.clear(); KYC_BY_ID.update(index_by_id(KYC_CASES))
    ALERT_BY_ID.clear(); ALERT_BY_ID.update(index_by_id(ALERT_CASES))
    WC_BY_ID.clear(); WC_BY_ID.update(index_by_id(WC_CASES))
    ANALYST_BY_KEY.clear(); ANALYST_BY_KEY.update({a["key"]: a for a in ANALYSTS})
    ANALYST_BY_EMAIL.clear(); ANALYST_BY_EMAIL.update({a["email"]: a for a in ANALYSTS})


def _load_seed():
    """Rebuild the case lists from the seed, then lay stored actions back on
    top. Rebinds the lists in place so every reference stays valid — including
    the router's, which is bound to the list objects.

    A stored seed version that differs from the code's means the record shape
    may have moved; the store is cleared and the log says so. Done only in
    the reloader's child (or outside debug) so the parent process never
    wipes the database on its own boot."""
    started = time.perf_counter()
    analysts, kyc, alerts, wc = load_seed(CASE_BOOK)
    app.logger.info("Console case book: %s; %d cases built in %.2f s.", CASE_BOOK_LABEL[CASE_BOOK], len(kyc),
                    time.perf_counter() - started)
    ANALYSTS[:] = analysts
    KYC_CASES[:] = kyc
    ALERT_CASES[:] = alerts
    WC_CASES[:] = wc
    _rebuild_indexes()
    routing.configure(ANALYSTS, KYC_CASES)
    AI_SETTINGS.clear()
    AI_SETTINGS.update(ai_screening.default_settings())
    ai_screening.configure(AI_SETTINGS)
    SOP_SETTINGS.clear()
    SOP_SETTINGS.update(sop.default_settings())
    sop.configure(SOP_SETTINGS)

    in_serving_process = (not DEBUG) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    stamp = f"{SEED_VERSION}/{CASE_BOOK}"
    stored = StateStore.seed_version()
    if stored and stored != stamp and in_serving_process:
        app.logger.warning("Seed version or case book changed (%s -> %s); clearing the console's stored state.", stored, stamp)
        StateStore.clear(kinds=CONSOLE_KINDS)
        stored = ""
    if not stored and in_serving_process:
        StateStore.set_seed_version(stamp)

    # cases uploaded in the console: rebuilt from their stored rows, oldest
    # batch first, so the actions stored on them below have a case to land on
    if CASE_BOOK == "file":
        for batch in sorted(StateStore.records("upload"), key=lambda b: b.get("at", "")):
            if batch.get("removed_at"):
                continue                # removed on the Upload page: kept as history, no cases
            _add_uploaded([r for r in batch.get("rows", []) if r.get("kyc_case_id") not in KYC_BY_ID])

    redrawn = _redrawn_cases()
    if redrawn:
        app.logger.info("%d stored record(s) were read from an older drawing of their documents, without the proof of "
                        "address's name, or before the agent read every document; they start again.", len(redrawn))
    redecided = _redecided_cases(redrawn)
    skip = set(redrawn)
    for case_id in redecided:
        skip.add(case_id)
        skip.update(r["id"] for r in _linked(KYC_BY_ID[case_id]) if r)
    restored = (StateStore.restore("kyc", KYC_BY_ID, skip=skip) + StateStore.restore("alert", ALERT_BY_ID, skip=skip)
                + StateStore.restore("wc", WC_BY_ID, skip=skip) + StateStore.restore("analyst", ANALYST_BY_KEY)
                + StateStore.restore("settings", {AI_SETTINGS["id"]: AI_SETTINGS, SOP_SETTINGS["id"]: SOP_SETTINGS}))
    SCREEN_SETTINGS.clear()
    SCREEN_SETTINGS.update(id_screening.settings_from(
        next((r for r in StateStore.records("settings") if r.get("id") == id_screening.SETTINGS_ID), None)))
    if restored:
        app.logger.info("Restored %d stored record(s) onto the seed.", restored)
        relabelled = relabel(KYC_CASES, ALERT_CASES, WC_CASES, ANALYSTS)
        if relabelled:
            app.logger.info("Relabelled %d stored value(s) to the console's current names.", relabelled)
        resexed = _sync_sex()
        if resexed:
            app.logger.info("%d stored case(s) take their sex from the file's names now.", resexed)
        replaced = _sync_wc_places()
        if replaced:
            app.logger.info("%d stored watchlist record(s) list every place the file gives now.", replaced)
    if redecided:
        app.logger.info("%d case(s) confirmed on a name or an address the checks now refuse, or asked about a name the "
                        "rules now settle, were decided again on their stored read.", _redecide(redecided))


def _sync_sex():
    """A file case's sex comes from what the file says about its holder
    (filebook documents.holder_sex); records stored before that rule carry a coin toss.
    Bring the case, its screening alert and its watchlist case in line with
    the drawn passport. No one can edit the sex, so nothing a person set is
    overwritten. Returns how many cases changed."""
    alerts = {a.get("kyc_case_id"): a for a in ALERT_CASES}
    changed = 0
    for case in KYC_CASES:
        v = recon_book.VIEWS.get((case.get("recon") or {}).get("id", ""))
        if not v or case.get("agency") or case["client"].get("gender") == v["synth"]["sex"]:
            continue
        case["client"]["gender"] = v["synth"]["sex"]
        label = {"M": "Male", "F": "Female"}[v["synth"]["sex"]]
        alert = alerts.get(case["id"])
        if alert:
            alert.setdefault("screening_info", {})["gender"] = label
            if alert["id"] in WC_BY_ID:
                WC_BY_ID[alert["id"]]["gender"] = label
        changed += 1
    return changed


def _sync_wc_places():
    """A file case's watchlist match lists every place the record gives
    (recon_book.wc_places); matches stored before kept only the first. Bring
    them in line: no one edits a match's places, so nothing a person set is
    overwritten. Returns how many matches gained a place."""
    changed = 0
    for case in KYC_CASES:
        v = recon_book.VIEWS.get((case.get("recon") or {}).get("id", ""))
        wc = _linked(case)[1] if v else None
        if not wc:
            continue
        country, locations = recon_book.wc_places(v)
        for m in wc.get("matches", []):
            if m.get("wc_id") == v["wc"]["uid"]:
                changed += m.get("country") != country
                m["country"] = country
                m["key_data"].update(location=locations[0], locations=locations)
    return changed


def _redrawn_cases():
    """Ids (KYC, screening and watchlist) of stored cases whose ID or proof-
    of-address image has been redrawn since they were stored: what was read or decided on them
    was about another picture, so they keep their seeded state and the AI
    Agent reads them again. So do the cases whose stored read lacks the
    proof of address's name (ai_agent.needs_reread), and those decided
    before the agent read every document (ai_agent.read_before_all_documents)."""
    out = set()
    for stored in StateStore.records("kyc"):
        case = KYC_BY_ID.get(stored.get("id", ""))
        if case is None or "recon" not in case or case_documents.has_uploads(stored):
            continue                    # real images uploaded: nothing was drawn, and the seed would drop them
        if ((stored.get("recon") or {}).get("drawn", "") != case["recon"].get("drawn", "") or ai_agent.needs_reread(stored)
                or ai_agent.read_before_all_documents(stored)):
            out.add(case["id"])
            alert, wc = _linked(case)
            out.update(r["id"] for r in (alert, wc) if r)
    return out


def _redecided_cases(redrawn):
    """{KYC id: the agent's stored record} of the cases it confirmed on a name
    the checks now refuse (ai_agent.confirmed_on_older_names), asked the
    customer about a name the rules now settle (ai_agent.asked_on_older_names)
    or confirmed with an address it would not write now
    (ai_agent.address_written_otherwise): they keep their seeded state, and
    the agent decides them again on the stored read, with no new look at the
    images. A case read again (`redrawn`) is not decided again."""
    out = {}
    for stored in StateStore.records("kyc"):
        case = KYC_BY_ID.get(stored.get("id", ""))
        if case is None or "recon" not in case or case["id"] in redrawn or case_documents.has_uploads(stored):
            continue
        if (ai_agent.confirmed_on_older_names(stored, case) or ai_agent.asked_on_older_names(stored, case)
                or ai_agent.address_written_otherwise(stored, case)):
            out[case["id"]] = stored["ai_agent"]
    return out


def _redecide(stored):
    """Decide again the cases _redecided_cases chose, each on its seed and its
    stored read, as the worker would have (a case a person holds on the seed
    stays theirs, as it would for a read again); save them. Returns how many."""
    done = 0
    for case_id, record in stored.items():
        case = KYC_BY_ID[case_id]
        if not ai_agent.eligible(case):
            continue
        reviewer = case.get("current_reviewer", "")
        alert, wc = _linked(case)
        ai_agent.redecide(case, alert, wc, record)
        if reviewer and case["status"] == "Verified":
            workflow.worklog.close_entry(case, "Verified by the AI Agent", email=reviewer)
        _save("kyc", case)
        if alert:
            _save("alert", alert)
            _save("wc", wc)
        done += 1
    return done


def _add_uploaded(rows):
    """Uploaded rows → cases on the live lists, screened at intake. Returns
    (built cases, [(line, reason)] for rows that could not be built)."""
    built, failed = recon_book.add_rows(rows, len(KYC_CASES) + 1)
    for kyc, alert, wc in built:
        KYC_CASES.append(kyc)
        ALERT_CASES.append(alert)
        WC_CASES.append(wc)
    _rebuild_indexes()
    return [k for k, _, _ in built], failed


def _remove_uploaded(ids):
    """Take uploaded cases off the live lists and out of the store, with the
    screening and watchlist records paired with them. The lists are trimmed
    in place, so the router's binding stays valid. Returns how many went."""
    ids = set(ids)
    paired = {a["id"] for a in ALERT_CASES if a.get("kyc_case_id") in ids}   # a watchlist case shares its alert's id
    gone = sum(1 for c in KYC_CASES if c["id"] in ids)
    KYC_CASES[:] = [c for c in KYC_CASES if c["id"] not in ids]
    ALERT_CASES[:] = [a for a in ALERT_CASES if a["id"] not in paired]
    WC_CASES[:] = [w for w in WC_CASES if w["id"] not in paired]
    _rebuild_indexes()
    StateStore.delete("kyc", ids)
    StateStore.delete("alert", paired)
    StateStore.delete("wc", paired)
    for cid in ids:
        recon_book.VIEWS.pop(cid, None)
        uploads.remove_case(cid)
    _bump_state()
    return gone


def _save(kind: str, record: dict):
    """Persist one touched record and tell the live-refresh poll about it."""
    _bump_state()
    StateStore.save(kind, record)


def _kyc(case_id: str) -> dict:
    return KYC_BY_ID.get(case_id) or abort(404)


def _alert(alert_case_id: str) -> dict:
    return ALERT_BY_ID.get(alert_case_id) or abort(404)


def _wc(wc_case_id: str) -> dict:
    return WC_BY_ID.get(wc_case_id) or abort(404)


def _run_action(fn, *args, **kwargs):
    """Call an engine action; a WorkflowError becomes an error flash and the
    caller redirects. Returns (ok, result)."""
    try:
        return True, fn(*args, **kwargs)
    except WorkflowError as exc:
        flash(str(exc), "error")
        return False, None


def _csv_response(filename, columns, rows):
    return Response(to_csv(columns, rows), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _routing_note(case):
    """One clause for a flash: where the router sent the case."""
    r = case.get("routing") or {}
    if r.get("status") == "routed" and case.get("current_reviewer"):
        return f"Routed to {display_name(case['current_reviewer'])} at once."
    if r.get("status") == "queued":
        return "No analyst has free capacity right now; the case waits in the pipeline."
    return ""


# ─── Context processor ─────────────────────────────────────────────────────────
@app.context_processor
def _inject_nav():
    a = _analyst() or {}
    signed_in = bool(a)
    return {
        "tenants": vocab.TENANTS,
        "tenant": _tenant() if signed_in else "ttlive",
        "analyst_tenants": a.get("tenants") or ["ttlive"],
        "top_tabs": [dict(t, endpoint=_home_endpoint(), active=True) for t in TOP_TABS],
        "side_nav": _side_nav() if signed_in else [],
        "role_label": " / ".join(a.get("roles", [])),
        "user_initials": initials(a.get("name", "")),
        "home_url": url_for(_home_endpoint()) if signed_in else url_for("portal"),
        "demo_mode": DEMO_MODE,
        "switch_users": ANALYSTS if (DEMO_MODE and signed_in) else [],
        "all_analysts": ANALYSTS,
        "status_tone": vocab.STATUS_TONE,
        "state_version": STATE_VERSION,
        # The line-icon macro, delivered to every template including the
        # standalone auth pages. Fetched per render so editing _icon.html
        # shows up without a restart, like any other template.
        "icon": app.jinja_env.get_template("_icon.html").module.icon,
    }


# ─── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/state/version")
def state_version():
    """Cheap staleness probe polled by every open console."""
    return jsonify({"v": STATE_VERSION})


@app.route("/")
def portal():
    """The console's front door: the case list when signed in, else sign-in."""
    return redirect(url_for(_home_endpoint() if session.get("user") in ANALYST_BY_KEY else "login"))


def _login_page(error="", selected="", next_url="", status=200):
    return render_template("login.html", error=error, selected=selected or ANALYSTS[0]["key"], next=next_url,
                           analysts=ANALYSTS, demo_password=DEMO_PASSWORD if _SHOW_DEMO_HINT else ""), status


def _sign_in(analyst: dict):
    session.clear()
    session["user"] = analyst["key"]
    session["email"] = analyst["email"]
    session["name"] = analyst["name"]
    session["role"] = analyst["role"]
    session["roles"] = list(analyst["roles"])
    session["tenant"] = analyst["tenants"][0]


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")
    if request.method == "POST":
        key = request.form.get("analyst", "")
        analyst = ANALYST_BY_KEY.get(key)
        if not analyst:
            return _login_page(error="Choose an analyst profile to sign in as.", next_url=next_url, status=400)
        if not _check_password(request.form.get("password", "")):
            return _login_page(error="Incorrect password.", selected=key, next_url=next_url, status=401)
        _sign_in(analyst)
        return redirect(_safe_next(next_url))
    if session.get("user") in ANALYST_BY_KEY:
        return redirect(_safe_next(next_url))
    return _login_page(next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("portal"))


@app.route("/tenant", methods=["POST"])
def set_tenant():
    wanted = request.form.get("tenant", "")
    if wanted in (_analyst() or {}).get("tenants", []):
        session["tenant"] = wanted
    return redirect(_safe_next(request.form.get("next", "")))


@app.route("/switch-user", methods=["POST"])
def switch_user():
    """Demo shortcut: jump between analyst profiles without the login page,
    so the presenter can go from L1 to L2 in one click. Off unless DEMO_MODE."""
    if not DEMO_MODE:
        abort(404)
    analyst = ANALYST_BY_KEY.get(request.form.get("analyst", ""))
    if analyst:
        keep_tenant = session.get("tenant")
        _sign_in(analyst)
        if keep_tenant in analyst["tenants"]:
            session["tenant"] = keep_tenant
        flash(f"Now working as {analyst['name']} ({' / '.join(analyst['roles'])}).", "success")
    return redirect(_safe_next(request.form.get("next", "")))


# ─── KYC case management ───────────────────────────────────────────────────────
def _pager(page, pages):
    """Links for templates/_pager.html: first, last and two either side of
    the current page, each keeping the request's query string."""
    args = request.args.to_dict(flat=False)

    def link(n):
        return url_for(request.endpoint, **dict(request.view_args or {}, **dict(args, page=[str(n)])))

    shown = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    items, last = [], 0
    for n in shown:
        if n - last > 1:
            items.append({"gap": True})
        items.append({"n": n, "url": link(n), "on": n == page})
        last = n
    return {"page": page, "pages": pages, "links": items,
            "prev": link(page - 1) if page > 1 else "", "next": link(page + 1) if page < pages else ""}


def _case_page_size():
    """Rows a page on the case list: ?per_page= (one of PAGE_SIZES) chooses
    it, and the choice holds for the rest of the session, through Search,
    Clear and the menu link."""
    asked = request.args.get("per_page", "")
    if asked.isdigit() and int(asked) in PAGE_SIZES:
        session["case_per_page"] = int(asked)
    size = session.get("case_per_page", PER_PAGE)
    return size if size in PAGE_SIZES else PER_PAGE


@app.route("/kyc/list")
def kyc_list():
    vm = CaseListConsole.compute(KYC_CASES, _tenant(), _actor(), query=read_case_query(request.args),
                                 per_page=_case_page_size())
    return render_template("kyc_list.html", rows=vm["rows"], total=vm["total"], shown=vm["shown"], filters=vm["filters"],
                           query=vm["query"], per_page=vm["per_page"], page_sizes=PAGE_SIZES, first=vm["first"], last=vm["last"],
                           pages=_pager(vm["page"], vm["pages"]), open_only=vm["open_only"], can_upload=CASE_BOOK == "file",
                           can_assign=bool(_levels() & {"L2", "QC"}))      # kyc_bulk_assign's roles


def _linked(case):
    """(screening case, watchlist case) for a KYC case with a hit, else (None, None)."""
    alert = ALERT_BY_ID.get(case["screening"]["alert_case_id"]) if case["screening"]["hit"] else None
    wc = WC_BY_ID.get(alert["id"]) if alert else None
    return alert, wc


_POA_RESUBMIT = {          # a failed proof-of-address check (workflow.poa_checks key) -> the Resubmit POA reason
    "poa_age": sop.poa_old_reason,
    "poa_type": lambda: "The document type is not an accepted proof of address",
    "poa_country": lambda: "The proof of address is from a prohibited country",
}


@app.route("/kyc/detail/<case_id>")
def kyc_detail(case_id):
    case = _kyc(case_id)
    alert, wc = _linked(case)
    levels, actor = _levels(), _actor()
    l2_level = "EDD" if case["pending_reason"] == "Pending EDD" else "L2"
    stage, needed = workflow.review_needs(case)
    hit = case["screening"]["hit"]
    checks = workflow.poa_checks(case)
    failed = [c for c in checks if not c["ok"]]
    return render_template(
        "kyc_detail.html", case=case, alert=alert, wc=wc, levels=levels, actor=actor,
        points=workflow.verification_points(case), review=workflow.review_action(case, levels, actor),
        is_mine=case["current_reviewer"] == actor, closed=case["status"] in vocab.CLOSED_STATUSES,
        review_needs=(stage, needed), level_names=_analysts_with(needed, case["tenant"]),
        sla=sla.chip(case), vendor=idv_vendor.details(case), confirm_fields=workflow.confirm_fields(case),
        pending_alerts=sum(1 for a in (alert or {}).get("alerts", []) if a["outcome"] == "Pending"),
        exports=(wc or {}).get("exports", []), l2_level=l2_level,
        segregated=segregation.hidden_from(case, actor, l2_level) if case["status"] == "DataEscalatingL2" else False,
        edd_decisions=(list(vocab.EDD_DECISIONS) if hit else ["No match - Approve"] + list(vocab.EDD_DECISIONS)),
        analysts=ANALYSTS,
        poa_checks=checks, poa_failed=bool(failed),
        poa_sanction_fail=any(c["label"].startswith("Sanctioned-country") for c in failed),
        poa_resubmit_reason=next((_POA_RESUBMIT[c["key"]]() for c in failed if c.get("key") in _POA_RESUBMIT), ""),
        id_resubmit_reason=(case.get("ai_agent") or {}).get("resubmit", "") if case["status"] == "DataConfirming" else "",
        file_items=investigation.items(case, alert, wc) if case.get("investigation_file") else [],
        agent=_agent_view(case, actor, levels), upload_slots=uploads.SLOTS,
        can_upload_docs=(case["status"] not in vocab.CLOSED_STATUSES and not case.get("agency")
                         and bool(levels & {"L1", "L2", "EDD", "QC"})))


@app.route("/kyc/detail/<case_id>/audit")
def kyc_audit(case_id):
    return render_template("kyc_audit.html", case=_kyc(case_id), view=request.args.get("view", "audit"))


def _case_action(case_id, fn, *args, success="", open_modal="", extra=(), **kwargs):
    """Run one workflow action on a case as the signed-in analyst; persist on
    success (plus any `extra` (kind, record) pairs the action touched); go
    back to the case, reopening the modal on a validation flash. `success`
    may be a callable taking the case, for messages that depend on the
    outcome (where the router sent it)."""
    case = _kyc(case_id)
    ok, result = _run_action(fn, case, _actor(), *args, **kwargs)
    if ok:
        _save("kyc", case)
        for kind, record in extra:
            if record is not None:
                _save(kind, record)
        text = success(case) if callable(success) else success
        if text:
            flash(text, "success")
    target = url_for("kyc_detail", case_id=case_id)
    if not ok and open_modal:
        target += f"?open={open_modal}"
    return redirect(target)


@app.route("/kyc/<case_id>/reserve", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_reserve(case_id):
    return _case_action(case_id, workflow.reserve, success="Case reserved. You are the current reviewer.")


@app.route("/kyc/<case_id>/release", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_release(case_id):
    return _case_action(case_id, workflow.release, success="Case released to the queue.")


@app.route("/kyc/<case_id>/assign", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_assign(case_id):
    return _case_action(case_id, workflow.assign, request.form.get("assignee", ""), success="Case assigned.")


@app.route("/kyc/<case_id>/tag", methods=["POST"])
def kyc_tag(case_id):
    return _case_action(case_id, workflow.add_tag, request.form.get("tag", ""))


@app.route("/kyc/<case_id>/modify", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_modify(case_id):
    return _case_action(case_id, workflow.modify_user_info, request.form, success="User info updated; see the Change Log.",
                        open_modal="modify-modal")


@app.route("/kyc/<case_id>/status", methods=["POST"])
@role_required("L2", "QC")
def kyc_status(case_id):
    return _case_action(case_id, workflow.change_status, request.form.get("status", ""), request.form.get("comment", ""),
                        success="Status changed.", open_modal="status-modal")


def _confirmed_message(case):
    """What Data Confirm led to, now that the AI screening rules run on it."""
    screening = case["screening"]
    if case["status"] == "Verified":
        return "Data confirmed — the AI screening rules left nothing for an analyst, so the case is auto-verified."
    if screening.get("workflow") == "auto-verified" and screening["hit"]:
        return ("Data confirmed — the AI screening rules cleared every alert and the screening case closed itself; "
                "recalculate the risk rating and run the EDD Review to approve the business.")
    if screening.get("workflow") == "auto-verified":
        return "Data confirmed — no screening hit; recalculate the risk rating and run the EDD Review to approve the business."
    left = (screening.get("ai") or {}).get("for_review", 0)
    return f"Data confirmed — the case moved to Data Screening and was re-screened; {left} alert(s) are left for you to review."


@app.route("/kyc/<case_id>/data_confirm", methods=["POST"])
@role_required("L1")
def kyc_data_confirm(case_id):
    alert, wc = _linked(_kyc(case_id))
    return _case_action(case_id, workflow.data_confirm, request.form, alert_case=alert, wc=wc,
                        extra=(("alert", alert), ("wc", wc)), success=_confirmed_message, open_modal="dc-modal")


@app.route("/kyc/<case_id>/resubmit", methods=["POST"])
@role_required("L1")
def kyc_resubmit(case_id):
    return _case_action(case_id, workflow.resubmit, request.form.getlist("decision"), request.form.get("id_reason", ""),
                        request.form.get("poa_reason", ""), request.form.get("note", ""),
                        success="Resubmission requested. The case leaves your queue until the user uploads again.",
                        open_modal="dc-modal")


@app.route("/kyc/<case_id>/escalate", methods=["POST"])
@role_required("L1")
def kyc_escalate(case_id):
    case = _kyc(case_id)
    alert, _wc = _linked(case)
    back = "dc-modal" if case["status"] == "DataConfirming" else "edd-modal"
    return _case_action(case_id, workflow.escalate, request.form.get("reason", ""), request.form.get("comment", ""),
                        request.form.get("fraud_typology", ""), alert_case=alert, extra=(("alert", alert),),
                        success=lambda c: f"Escalated to L2. {_routing_note(c)} The 14-day EDD clock has started.".replace("  ", " "),
                        open_modal=back)


@app.route("/kyc/<case_id>/rfi", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_rfi(case_id):
    return _case_action(case_id, workflow.rfi, request.form.get("message", ""), success="RFI sent to the user.",
                        open_modal="rfi-modal")


@app.route("/kyc/<case_id>/edd_review", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_edd_review(case_id):
    case = _kyc(case_id)
    alert, _wc = _linked(case)
    attachment = request.form.get("attachment", "") or request.form.get("attachment_name", "")
    level = "L1" if "L1" in _levels() else "L2"

    def done(c):
        if c.get("pending_reason") == "Pending Checker 2":
            return f"Checker 1 approval recorded — the business now waits for Checker 2. {_routing_note(c)}".strip()
        if c["status"] == "DataEscalatingL2":
            return f"Escalated to L2. {_routing_note(c)} The 14-day EDD clock has started.".replace("  ", " ")
        return "Review recorded."
    return _case_action(case_id, workflow.edd_review, request.form.get("decision", ""), request.form.get("summary", ""),
                        attachment, alert_case=alert, level=level, extra=(("alert", alert),),
                        success=done, open_modal="edd-modal")


@app.route("/kyc/<case_id>/l2_decision", methods=["POST"])
@role_required("L2", "EDD")
def kyc_l2_decision(case_id):
    case = _kyc(case_id)
    level = "EDD" if case["pending_reason"] == "Pending EDD" else "L2"
    return _case_action(case_id, workflow.l2_decision, level, request.form.get("decision", ""), request.form.get("comment", ""),
                        levels=_levels(),
                        success=lambda c: (f"Escalated to EDD. {_routing_note(c)}".strip() if c.get("pending_reason") == "Pending EDD"
                                           else f"{level} decision recorded."),
                        open_modal="l2-modal")


@app.route("/kyc/<case_id>/checker2", methods=["POST"])
@role_required("L1", "L2")
def kyc_checker2(case_id):
    return _case_action(case_id, workflow.checker2_signoff, request.form.get("decision", ""), request.form.get("comment", ""),
                        success=lambda c: ("Checker 2 sign-off recorded — the business is verified." if c["status"] == "Verified"
                                           else f"Checker 2 decision recorded. {_routing_note(c)}".strip()),
                        open_modal="checker2-modal")


@app.route("/kyc/<case_id>/skopenow", methods=["GET", "POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_skopenow(case_id):
    case = _kyc(case_id)
    if request.method == "POST":
        ok, _ = _run_action(workflow.skopenow_clear, case, _actor(), request.form.get("outcome", ""), request.form.get("notes", ""))
        if ok:
            _save("kyc", case)
            flash("Skopenow outcome recorded.", "success")
            return redirect(url_for("kyc_detail", case_id=case_id))
        return redirect(url_for("kyc_skopenow", case_id=case_id))
    return render_template("skopenow.html", case=case, is_mine=case["current_reviewer"] == _actor())


@app.route("/kyc/<case_id>/recalculate_risk", methods=["POST"])
@role_required("L1", "L2", "EDD")
def kyc_recalculate_risk(case_id):
    case = _kyc(case_id)
    ok, rating = _run_action(workflow.recalculate_risk, case, _actor())
    if ok:
        _save("kyc", case)
        flash(f"Risk rating recalculated: {rating}.", "success")
    return redirect(url_for("kyc_detail", case_id=case_id))


@app.route("/kyc/bulk_assign", methods=["POST"])
@role_required("L2", "QC")
def kyc_bulk_assign():
    assignee = request.form.get("assignee", "")
    done = 0
    for case_id in request.form.getlist("case_id"):
        case = KYC_BY_ID.get(case_id)
        if not case:
            continue
        ok, _ = _run_action(workflow.assign, case, _actor(), assignee)
        if ok:
            _save("kyc", case)
            done += 1
    flash(f"Assigned {done} case(s) to {assignee}.", "success" if done else "error")
    return redirect(url_for("kyc_list"))


# ─── Documents ─────────────────────────────────────────────────────────────────
@app.route("/documents/<case_id>/<doc_id>.svg")
def document_svg(case_id, doc_id):
    """A document's image: the photo or scan uploaded for it (a real
    person's ID: only to analysts of the case's tenant, never cached), else
    the synthetic document drawn from the case data (see documents/svg_cards)."""
    case = _kyc(case_id)
    doc = next((d for d in case["documents"] if d["id"] == doc_id), None) or abort(404)
    up = doc.get("upload")
    if up:
        if case["tenant"] not in ((_analyst() or {}).get("tenants") or ["ttlive"]):
            abort(404)
        try:
            data = uploads.load(up["path"])
        except (OSError, WorkflowError):
            abort(404)
        return Response(data, mimetype=up["media_type"],
                        headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f'inline; filename="{case_id}-{doc_id}.jpg"'})
    return Response(_render_document(case, doc), mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.route("/kyc/<case_id>/documents", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_documents_upload(case_id):
    """The customer's real document images onto a case: each one checked and
    cleaned (documents/uploads), then in its slot in place of the drawing
    (case_documents.attach). The AI Agent reads them when the case is its to
    take."""
    case = _kyc(case_id)
    back = url_for("kyc_detail", case_id=case_id) + "#documents"
    if case["tenant"] not in ((_analyst() or {}).get("tenants") or ["ttlive"]):
        abort(404)
    holder = case.get("current_reviewer", "")
    if holder and holder != _actor():
        flash(f"{display_name(holder)} holds this case; documents go on it once it is released or with them.", "error")
        return redirect(back)
    images = {}
    for slot, label in uploads.SLOTS:
        f = request.files.get(slot)
        if f and f.filename:
            ok, res = _run_action(uploads.prepare, f.read(), label)
            if not ok:
                return redirect(back)
            images[slot] = (res[0], res[1], f.filename)
    poa = None
    if "poa" in images:
        ok, poa = _run_action(case_documents.poa_details, request.form, case)
        if not ok:
            return redirect(back)
    with AI_AGENT_LOCK:
        if case_id in AI_AGENT["busy"]:
            flash("The AI Agent is reading this case right now. Upload the images again in a minute.", "error")
            return redirect(back)
        ok, filled = _run_action(case_documents.attach, case, images, _actor(), poa)
        if not ok:
            return redirect(back)
        _save("kyc", case)
    reads = ai_agent.eligible(case) and not case.get("ai_agent")
    if reads and ai_review.configured():
        _agent_start("documents")
        then = " The AI Agent is reading them now."
    elif reads:
        then = " The AI Agent reads them once LLM_FOUNDRY_KEY is set."
    elif case["status"] != "DataConfirming":
        then = " The case is past Data Confirming, so the AI Agent does not read them; the analyst on it reviews them."
    else:
        then = " The AI Agent does not take this case (a person holds or has worked it); the analyst reviews them."
    flash(f"Uploaded: {', '.join(filled)}." + then, "success")
    return redirect(back)


@app.errorhandler(413)
def _too_large(_exc):
    flash(f"The upload is larger than {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB in all; send fewer or smaller files.",
          "error")
    return redirect(_back())


def _render_document(case, doc):
    """A reconciliation-log case's passport, secondary ID and proof of
    address are the file book's images (the file's printed names, MRZ and
    addresses); everything else is drawn by svg_cards from the case. A slot
    still waiting for its uploaded image says so."""
    if doc.get("awaiting") and not doc.get("upload"):
        return svg_cards.awaiting_svg(case_documents.slot_title(doc))
    if doc.get("hub_file"):
        svg = recon_book.render(case, doc)
        if svg:
            return svg
    return svg_cards.render(case, doc)


# ─── Name screening ────────────────────────────────────────────────────────────
NS_PER_PAGE = 100


def _tenant_alerts():
    t = _tenant()
    return [a for a in ALERT_CASES if (KYC_BY_ID.get(a["kyc_case_id"]) or {}).get("tenant") == t]


@app.route("/name_screening/queue")
def ns_queue():
    level = request.args.get("level", "L1")
    mine = request.args.get("mine") == "1"
    # Row copies, not the records: "pending" is a view field and must not be
    # persisted into the store the next time the record is saved.
    alerts = [dict(a, pending=sum(1 for x in a["alerts"] if x["outcome"] == "Pending"),
                   auto=bool(a["case_outcome"] and a["case_outcome"].get("by") == vocab.SYSTEM_USER)) for a in _tenant_alerts()]
    queues = []
    for lv in ("L1", "L2"):
        rows = [a for a in alerts if a["case_level"] == lv]
        auto_closed = sum(1 for a in rows if a["auto"])
        queues.append({"level": lv, "name": f"{vocab.TENANTS[_tenant()]['cms_queue']}_{lv}", "total": len(rows),
                       "pending": sum(1 for a in rows if a["status"] != "Closed"),
                       "completed": sum(1 for a in rows if a["status"] == "Closed"),
                       "auto_closed": auto_closed, "human": len(rows) - auto_closed})
    rows = [a for a in alerts if a["case_level"] == level and (not mine or a["assigned_to"] == _actor())]
    rows.sort(key=lambda a: (a["status"] == "Closed", a["created_at"]), reverse=False)
    # Paged: a file-backed book puts a thousand screening cases in one queue.
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    pages = max(1, -(-len(rows) // NS_PER_PAGE))
    page = min(page, pages)
    shown = rows[(page - 1) * NS_PER_PAGE: page * NS_PER_PAGE]
    return render_template("ns_queue.html", level=level, mine_only=mine, queues=queues, rows=shown, total=len(rows),
                           first=(page - 1) * NS_PER_PAGE + 1 if rows else 0, last=(page - 1) * NS_PER_PAGE + len(shown),
                           pages=_pager(page, pages))


@app.route("/name_screening/search")
def ns_search():
    q = (request.args.get("q") or "").strip().lower()
    if q:
        # Ids compare without case: the reconciliation log's are KYC-2026-3011 and WC-554473.
        for a in ALERT_CASES:
            k = KYC_BY_ID.get(a["kyc_case_id"]) or {}
            if q in (a["id"].lower(), a["merchant_uid"].lower(), a["kyc_case_id"].lower())                     or q in k.get("client", {}).get("legal_name", "").lower():
                return redirect(url_for("ns_case", alert_case_id=a["id"]))
        for wc in WC_CASES:
            mid = next((m["id"] for m in wc["matches"] if str(m["wc_id"]).lower() == q), None)
            if mid:
                return redirect(url_for("wc_match", wc_case_id=wc["id"], match_id=mid))
    flash(f"No screening case or record matches '{q}'." if q else "Enter a case ID, merchant UID, KYC case ID or name.", "error")
    return redirect(request.referrer or url_for("ns_queue"))


@app.route("/name_screening/case/<alert_case_id>")
def ns_case(alert_case_id):
    alert = _alert(alert_case_id)
    wc = WC_BY_ID.get(alert["id"])
    types = sorted({a["type"] for a in alert["alerts"] if a["type"]})
    kyc = KYC_BY_ID.get(alert.get("kyc_case_id", ""))
    return render_template("ns_case.html", alert=alert, wc=wc, actor=_actor(), types=types,
                           kyc=kyc, watch_labels=ai_agent.WATCH_LABELS, details=_alert_details(alert, wc, kyc),
                           counts=screening.tab_counts(alert["alerts"]), strategies=[r["label"] for r in ai_screening.RULES],
                           recommendation=recommendation.recommend_case_outcome(alert, wc),
                           ai_ready=ai_review.ready(), ai_why=ai_review.why_not(), ai_model=ai_review.MODEL,
                           ai_tone=ai_review.VERDICT_TONE)


def _alert_details(alert, wc, kyc):
    """{alert id: what its row opens to}: the World-Check record behind the
    alert (Overview, Others), and Smart Review Results: the rules' verdicts
    on it, submitted against matched data, the AI Agent's finding from the
    documents."""
    client = (kyc or {}).get("client") or {}
    findings = {f["wc_id"]: f for f in ((((kyc or {}).get("ai_agent") or {}).get("watchlist") or {}).get("matches") or [])}
    out = {}
    for a in alert["alerts"]:
        m = worldcheck.find_match(wc, a["match_id"]) if wc else None
        out[a["id"]] = {"m": m, "rules": ai_screening.review_results(m, client) if m else [],
                        "compare": worldcheck.comparison_rows(wc, m, client) if m else [],
                        "finding": findings.get((m or {}).get("wc_id", ""))}
    return out


def _alert_action(alert_case_id, fn, *args, success="", open_modal="", **kwargs):
    alert = _alert(alert_case_id)
    ok, _ = _run_action(fn, alert, _actor(), *args, **kwargs)
    if ok:
        _save("alert", alert)
        if success:
            flash(success, "success")
    target = url_for("ns_case", alert_case_id=alert_case_id)
    return redirect(target + (f"?open={open_modal}" if not ok and open_modal else ""))


@app.route("/name_screening/<alert_case_id>/handle", methods=["POST"])
@role_required("L1", "L2")
def ns_handle(alert_case_id):
    return _alert_action(alert_case_id, screening.handle, success="You are handling this screening case.")


@app.route("/name_screening/<alert_case_id>/alert_outcome", methods=["POST"])
@role_required("L1", "L2")
def ns_alert_outcome(alert_case_id):
    n = len([i for i in request.form.getlist("alert_id") if i])
    return _alert_action(alert_case_id, screening.alert_outcome, request.form,
                         success="Alert outcome recorded." if n < 2 else f"Alert outcome recorded for {n} alerts.",
                         open_modal="alert-outcome")


@app.route("/name_screening/<alert_case_id>/ai_assess", methods=["POST"])
@role_required("L1", "L2")
def ns_ai_assess(alert_case_id):
    """Ask the model about the ticked alerts (every pending one when none
    are ticked). Advisory: nothing is resolved. What was assessed is saved
    even when a later call fails."""
    alert = _alert(alert_case_id)
    kyc, wc = KYC_BY_ID.get(alert["kyc_case_id"]), WC_BY_ID.get(alert["id"])
    ids = [i for i in request.form.getlist("alert_id") if i]
    ok, res = _run_action(ai_review.assess_alerts, kyc, alert, wc, _actor(), ids)
    if ok:
        done, error = res
        if done:
            _save("alert", alert)
            for kind, record in (("wc", wc), ("kyc", kyc)):
                if record is not None:
                    _save(kind, record)
            flash(f"AI assessment recorded for {len(done)} alert(s) ({ai_review.MODEL}).", "success")
        if error:
            flash(f"Assessment stopped after {len(done)} alert(s): {error}", "error")
    return redirect(url_for("ns_case", alert_case_id=alert_case_id))


@app.route("/name_screening/<alert_case_id>/case_outcome", methods=["POST"])
@role_required("L1", "L2")
def ns_case_outcome(alert_case_id):
    alert = _alert(alert_case_id)
    kyc = KYC_BY_ID.get(alert["kyc_case_id"])
    ok, _ = _run_action(screening.case_outcome, alert, _actor(), request.form, kyc=kyc)
    if ok:
        _save("alert", alert)
        if kyc:
            _save("kyc", kyc)
        flash(f"Case outcome recorded: {alert['case_outcome']['value']}. Back on the KYC case, run the EDD Review.", "success")
        return redirect(url_for("ns_case", alert_case_id=alert_case_id))
    return redirect(url_for("ns_case", alert_case_id=alert_case_id) + "?open=case-outcome")


# ─── World-Check (simulated) ───────────────────────────────────────────────────
def _wc_context(wc):
    alert = ALERT_BY_ID.get(wc["alert_case_id"])
    return {"wc": wc, "alert": alert, "back_url": url_for("ns_case", alert_case_id=wc["alert_case_id"]),
            "can_resolve": bool(alert) and alert["status"] == "Handling" and alert["assigned_to"] == _actor()}


def _wc_page(wc_case_id):
    """(wc, context, screening case or None). A screening case (SCAN-…) is
    shown in the console from its hits (id_screening.wc_view); resolving a
    match there confirms the hit, the console's three verdicts only."""
    if not wc_case_id.startswith("SCAN-"):
        wc = _wc(wc_case_id)
        return wc, _wc_context(wc), None
    rec = _scan(wc_case_id)
    wc = id_screening.wc_view(rec)
    is_open = rec.get("status") != "closed"
    return wc, {"wc": wc, "alert": None, "back_url": url_for("scan_alerts", scan_id=rec["id"]),
                "can_resolve": is_open and bool(_levels() & {"L1", "L2"}), "case_open": is_open, "wc_statuses": list(id_screening.FROM_WC_STATUS),
                "resolve_hint": "The case is closed" if not is_open else "An L1 or L2 analyst resolves matches"}, rec


@app.route("/wc/<wc_case_id>")
def wc_case(wc_case_id):
    wc, ctx, _ = _wc_page(wc_case_id)
    status, mtype = request.args.get("status", ""), request.args.get("type", "")
    filtered = worldcheck.filter_matches(wc["matches"], status, mtype)
    page = worldcheck.paginate(filtered, int(request.args.get("page", "1") or 1))
    return render_template("wc_case.html", tab=request.args.get("tab", "worldcheck"), status=status, mtype=mtype,
                           facets=worldcheck.facets(wc["matches"]), page=page, **ctx)


@app.route("/wc/<wc_case_id>/match/<match_id>")
def wc_match(wc_case_id, match_id):
    wc, ctx, rec = _wc_page(wc_case_id)
    m = worldcheck.find_match(wc, match_id) or abort(404)
    status, mtype = request.args.get("status", ""), request.args.get("type", "")
    if rec:
        who = rec["screening"]["person"]
        client = {"country": who.get("issuing_country", ""), "nationality": who.get("nationality", "")}
    else:
        client = (KYC_BY_ID.get(wc["kyc_case_id"]) or {}).get("client")
    return render_template("wc_match.html", m=m, status=status, mtype=mtype,
                           nav=worldcheck.match_nav(wc, match_id, status, mtype),
                           comparison=worldcheck.comparison_rows(wc, m, client),
                           unresolved=worldcheck.facets(wc["matches"])["status"]["Unresolved"], **ctx)


@app.route("/wc/<wc_case_id>/match/<match_id>/resolve", methods=["POST"])
@role_required("L1", "L2")
def wc_resolve(wc_case_id, match_id):
    wc, ctx, rec = _wc_page(wc_case_id)
    f = request.form
    back = redirect(url_for("wc_match", wc_case_id=wc_case_id, match_id=match_id, status=f.get("status_filter", ""),
                            type=f.get("type_filter", "")))
    if rec:
        status, risk, reason, comment = f.get("status", ""), f.get("risk", ""), f.get("reason", ""), f.get("comment", "").strip()
        problem = ("The case is closed." if not ctx["can_resolve"] else "Pick a status." if status not in id_screening.FROM_WC_STATUS
                   else "Pick a risk level." if risk not in vocab.WC_RISKS else "Select a reason." if reason not in vocab.WC_REASONS
                   else "A review comment is required." if not comment else "")
        err = [problem] if problem else []

        def apply(r):
            try:
                id_screening.resolve(r, match_id, id_screening.FROM_WC_STATUS[status], comment, _actor(), risk, reason)
            except ValueError as exc:
                err.append(str(exc))
        if not err:
            rec = _screen_update(wc_case_id, apply)
        if err:
            flash(err[0], "error")
        elif rec.get("status") == "closed":
            flash(f"Match resolved. Every match is resolved: the case is closed as {rec['closed']['outcome']}.", "success")
        else:
            flash("Match resolved; the screening case shows it.", "success")
        return back
    if not ctx["can_resolve"]:
        flash("Handle the screening case first — only its handler can resolve matches.", "error")
    else:
        ok, _ = _run_action(worldcheck.resolve_match, wc, _actor(), match_id, f.get("status", ""), f.get("risk", ""),
                            f.get("reason", ""), f.get("comment", ""))
        if ok:
            _save("wc", wc)
            flash("Match resolved. Record the matching alert outcome on the screening case.", "success")
    return back


@app.route("/wc/<wc_case_id>/export", methods=["POST"])
def wc_export(wc_case_id):
    wc, _, rec = _wc_page(wc_case_id)
    if rec:
        export = {}
        _screen_update(wc_case_id, lambda r: export.update(id_screening.wc_export(r, _actor())))
        flash(f"Exported {export['filename']}.", "success")
    else:
        export = worldcheck.record_export(wc, _actor())
        _save("wc", wc)
        flash(f"Exported {export['filename']} — it is now available to attach in the EDD Review.", "success")
    return redirect(url_for("wc_case", wc_case_id=wc_case_id, tab="summary"))


@app.route("/wc/<wc_case_id>/export.pdf")
def wc_export_pdf(wc_case_id):
    wc, _, rec = _wc_page(wc_case_id)
    alert = None if rec else ALERT_BY_ID.get(wc["alert_case_id"])
    pdf = render_wc_export(wc, alert, None if rec else KYC_BY_ID.get(wc["kyc_case_id"]), _actor())
    name = (wc["exports"][-1]["filename"] if wc["exports"] else f"WorldCheck_{wc['id'][:8]}.pdf")
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


# ─── Queues ────────────────────────────────────────────────────────────────────
@app.route("/queue/l2")
@role_required("L2", "EDD", "QC")
def queue_l2():
    vm = QueueConsole.compute(KYC_CASES, _tenant(), _actor(), _levels())
    return render_template("queue_l2.html", vm=vm)


# ─── Operations hub: My Work and case on-demand (SOP 5.2.2) ────────────────────
@app.route("/work")
def work():
    return render_template("work.html", vm=WorkConsole.compute(KYC_CASES, ALERT_CASES, _analyst()))


@app.route("/work/next", methods=["POST"])
@role_required("L1", "L2", "EDD")
def work_next():
    ok, routed = _run_action(routing.request_cases, _analyst(), 1, tenant=_tenant(), trigger="next")
    if not ok:
        return redirect(url_for("work"))
    case = routed[0]
    _save("kyc", case)
    flash(f"Routed to you: {case['client']['legal_name']} — {case['routing']['reason']}.", "success")
    return redirect(url_for("kyc_detail", case_id=case["id"]))


@app.route("/work/request", methods=["POST"])
@role_required("L1", "L2", "EDD")
def work_request():
    ok, routed = _run_action(routing.request_cases, _analyst(), request.form.get("n", "1"), tenant=_tenant())
    if ok:
        for c in routed:
            _save("kyc", c)
        flash(f"Routed {len(routed)} case(s) to you: " + ", ".join(c["client"]["legal_name"] for c in routed) + ".", "success")
    return redirect(url_for("work"))


@app.route("/kyc/<case_id>/qc_ack", methods=["POST"])
def kyc_qc_ack(case_id):
    case = _kyc(case_id)
    ok, _ = _run_action(workflow.qc_ack, case, _actor())
    if ok:
        _save("kyc", case)
        flash("QC feedback acknowledged.", "success")
    return redirect(url_for("work"))


# ─── Operations hub: pipeline, trackers, investigation file, audit log (SOP 5.2.3–5.2.4)
@app.route("/pipeline")
def pipeline():
    return render_template("pipeline.html", vm=PipelineConsole.compute(KYC_CASES, _tenant()))


@app.route("/trackers")
def trackers():
    vm = TrackerConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant(), request.args.get("view", "productivity"))
    if request.args.get("format") == "csv":
        return _csv_response(f"tracker_{vm['view']}_{_tenant()}.csv", vm["columns"], vm["rows"])
    return render_template("trackers.html", vm=vm)


@app.route("/kyc/detail/<case_id>/file")
def kyc_file(case_id):
    case = _kyc(case_id)
    alert, wc = _linked(case)
    return render_template("investigation_file.html", case=case, alert=alert, wc=wc,
                           items=investigation.items(case, alert, wc))


@app.route("/kyc/<case_id>/investigation_file.zip")
def kyc_file_zip(case_id):
    case = _kyc(case_id)
    alert, wc = _linked(case)
    actor = _actor()
    name, data = investigation.zip_bytes(case, alert, wc,
                                        render_pdf=lambda w, a, k: render_wc_export(w, a, k, actor),
                                        render_doc=_render_document)
    return Response(data, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.route("/audit")
def audit_log():
    vm = AuditLogConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant())
    if request.args.get("format") == "csv":
        rows = [[r["at"], r["actor"], r["kind_label"], r["record_id"], r["label"], r["action"], r["detail"]] for r in vm["rows"]]
        return _csv_response(f"audit_log_{_tenant()}.csv", ["At", "Actor", "Record", "Record ID", "Name", "Action", "Detail"], rows)
    return render_template("audit_log.html", vm=vm)


# ─── Operations hub: QC sampling (SOP 2.1) ─────────────────────────────────────
@app.route("/qc")
@role_required("QC")
def qc_sampling():
    tenant = _tenant()
    sample = qc.sample(KYC_CASES, tenant)
    return render_template("qc.html", sample=[{"case": c, "sla": sla.chip(c)} for c in sample],
                           scorecard=qc.scorecard(KYC_CASES, tenant),
                           pending=sum(1 for c in sample if not c.get("qc_result")), day=dates.now_str()[:10])


@app.route("/kyc/<case_id>/qc_review", methods=["POST"])
@role_required("QC")
def kyc_qc_review(case_id):
    case = _kyc(case_id)
    ok, _ = _run_action(workflow.qc_review, case, _actor(), request.form.get("result", ""),
                        request.form.get("reason", ""), request.form.get("comment", ""))
    if ok:
        _save("kyc", case)
        flash(f"QC review recorded: {case['qc_result']} — {case['client']['legal_name']}.", "success")
    return redirect(url_for("qc_sampling"))


# ─── Operations hub: routing console (SOP 5.2.1) ───────────────────────────────
@app.route("/routing")
@role_required("L2", "QC")
def routing_console():
    return render_template("routing.html", vm=RoutingConsole.compute(KYC_CASES, ANALYSTS, _tenant()),
                           can_edit="QC" in _levels())


@app.route("/routing/run", methods=["POST"])
@role_required("L2", "QC")
def routing_run():
    ids = request.form.getlist("case_id")
    wanted = len(ids) if ids else len(routing.unassigned(_tenant(), KYC_CASES))
    results = routing.route_backlog(_tenant(), only_ids=ids if ids else None)
    for c, _a, _r in results:                    # only the cases the run touched
        _save("kyc", c)
    routed = [(c, a) for c, a, _ in results if a]
    waiting = wanted - len(routed)
    if not wanted:
        flash("Nothing to route.", "error")
    else:
        detail = ", ".join(f"{c['client']['legal_name']} → {display_name(a['email'])}" for c, a in routed[:6])
        flash(f"Routed {len(routed)} case(s){' — ' + detail if detail else ''}."
              + (f" {waiting} still waiting for capacity." if waiting else ""), "success" if routed else "error")
    return redirect(url_for("routing_console"))


# ─── AI Screening (the preset automation and its switches) ──────────────────
@app.route("/ai-screening")
def ai_screening_page():
    vm = AiScreeningConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant(), AI_SETTINGS,
                                    agent=dict(AI_AGENT, workers=AI_AGENT_WORKERS))
    return render_template("ai_screening.html", vm=vm, can_edit="QC" in _levels(), agent_ready=ai_review.configured(),
                           agent_labels=AGENT_LABELS)


# ─── SOP rules (the parameters the checks take from the SOP) ────────────────
@app.route("/sop")
def sop_page():
    return render_template("sop.html", rules=sop.rows(), history=list(reversed(SOP_SETTINGS.get("history", [])))[:30],
                           can_edit="QC" in _levels())


@app.route("/sop/change", methods=["POST"])
@role_required("QC")
def sop_change():
    """One SOP parameter changed by the team lead: saved, then the open cases
    the AI Agent decided and nobody has touched since checked again on their
    stored read under it (_sop_recheck)."""
    ok, res = _run_action(sop.set_value, request.form.get("key", ""), request.form, _actor())
    if not ok:
        return redirect(url_for("sop_page"))
    param, before, after = res
    _save("settings", SOP_SETTINGS)
    counts = _sop_recheck()
    done = sum(counts.values())
    flash(f"{param['label']}: {before} → {after}. It applies at once"
          + (f"; {done} open case(s) the AI Agent decided were checked again: {counts['verified']} verified, "
             f"{counts['screening']} to Data Screening, {counts['resubmit'] + counts['rfi']} waiting on the customer, "
             f"{counts['human']} with a person." if done else "; no open case of the AI Agent's needed checking again."),
          "success")
    return redirect(url_for("sop_page"))


def _sop_recheck():
    """The open cases the agent decided under other SOP values, that nobody
    has touched since (ai_agent.needs_sop_recheck), decided again on their
    stored read (ai_agent.sop_recheck; no model call). Returns a Counter of
    the decisions they end on."""
    from collections import Counter
    counts = Counter()
    with AI_AGENT_LOCK:
        for case in KYC_CASES:
            if case["id"] in AI_AGENT["busy"] or not ai_agent.needs_sop_recheck(case):
                continue
            reviewer = case.get("current_reviewer", "")
            alert, wc = _linked(case)
            record = ai_agent.sop_recheck(case, alert, wc)
            if reviewer and case["status"] == "Verified":
                workflow.worklog.close_entry(case, "Verified by the AI Agent", email=reviewer)
            _save("kyc", case)
            if alert:
                _save("alert", alert)
                _save("wc", wc)
            counts[record["decision"]] += 1
    return counts


@app.route("/ai-screening/rules", methods=["POST"])
@role_required("QC")
def ai_screening_rules():
    key = request.form.get("key", "")
    ok, res = _run_action(ai_screening.set_rule, AI_SETTINGS, _actor(), key, request.form.get("enabled", ""))
    if ok:
        _save("settings", AI_SETTINGS)
        rule = (ai_screening.RULE_BY_KEY.get(key) or ai_screening.GUARD_BY_KEY.get(key)
                or ai_screening.SWITCH_BY_KEY.get(key) or {})
        when = "Applies at once." if key in ai_screening.SWITCH_BY_KEY else "Applies to the next screening run."
        flash(f"{rule.get('label', key)}: {'on' if res[0] else 'off'} → {'on' if res[1] else 'off'}. {when}", "success")
    return redirect(url_for("ai_screening_page"))


# ─── AI Agent (engines/ai_agent): Claude reads the documents, checks decide ─
# It starts by itself on the first request the serving process handles and
# reads every document of an individual's case in Data Confirming: one the
# AI screening rules cleared (auto-verified) verifies itself when every check
# passes; one with an alert left that no person holds is confirmed and goes
# to Data Screening with the agent's watchlist finding, for an analyst to
# clear the alert; anything that fails goes to an analyst with the reasons.
# A team lead can pause it on the AI
# Screening page. Workers read outside the lock (the slow model call) and
# decide under it, re-checking the case is still theirs to take. A read is
# stored on the case, so a restart does not read a case twice.
AI_AGENT_WORKERS = 2          # the gateway refuses more concurrent connections
AI_AGENT_LOCK = threading.Lock()
AI_AGENT = {"running": False, "busy": set(), "tried": set(), "threads": [], "done": 0, "errors": 0, "verdicts": 0, "rechecked": 0, "started_by": "",
            "started_at": "", "stopped": "", "autostarted": False}
AGENT_LABELS = {"verified": "Handled by AI", "screening": "Confirmed by AI · alert left for an analyst",
                "human": "Sent to a person", "resubmit": "Asked the customer · new proof of address",
                "rfi": "Asked the customer · information"}


def _agent_view(case, actor, levels):
    """What the case page shows: the agent's read and checks, once it has
    worked the case, and Claude's own verdict beside them."""
    return {"show": bool(case.get("ai_agent")), "labels": AGENT_LABELS, "verdict_labels": ai_agent.VERDICT_LABELS,
            "watch_labels": ai_agent.WATCH_LABELS, "verdict_watch": ai_agent.VERDICT_WATCHLIST,
            "slot_labels": uploads.SLOT_LABELS}


def _agent_finish(case, perceived):
    """Under the lock: decide on a read and save what changed."""
    reviewer = case.get("current_reviewer", "")
    alert, wc = _linked(case)
    record = ai_agent.decide(case, alert, wc, perceived)
    if reviewer and case["status"] == "Verified":
        # an analyst had it reserved; their turn ends with the agent's close
        workflow.worklog.close_entry(case, "Verified by the AI Agent", email=reviewer)
    AI_AGENT["done"] += 1
    AI_AGENT["errors"] += 1 if record.get("error") else 0
    _save("kyc", case)
    if alert:
        _save("alert", alert)
        _save("wc", wc)
    return record


def _agent_recheck(case):
    """Under the lock: the agent's current checks on a stored read (no model
    call); save what changed."""
    alert, wc = _linked(case)
    ai_agent.recheck(case, alert, wc)
    AI_AGENT["rechecked"] += 1
    _save("kyc", case)
    if alert:
        _save("alert", alert)
        _save("wc", wc)


def _agent_address(case):
    """Under the lock: a confirmed case's record address corrected to its
    proof of address's (no model call); save it."""
    ai_agent.update_address(case)
    AI_AGENT["addresses"] = AI_AGENT.get("addresses", 0) + 1
    _save("kyc", case)


def _agent_verdict(case):
    """A case read before verdicts existed: Claude's verdict from the stored
    read (one text call, no images), stored beside the decision."""
    verdict = ai_agent.opinion(case, case["ai_agent"]["read"], wc=_linked(case)[1])
    with AI_AGENT_LOCK:
        if ai_agent.needs_verdict(case):
            ai_agent.attach_verdict(case["ai_agent"], verdict)
            AI_AGENT["verdicts"] = AI_AGENT.get("verdicts", 0) + 1
            _save("kyc", case)


def _agent_worker():
    while AI_AGENT["running"]:
        backfill = False
        with AI_AGENT_LOCK:
            case = next((c for c in KYC_CASES if ai_agent.eligible(c) and not c.get("ai_agent")
                         and c["id"] not in AI_AGENT["busy"]), None)
            if case is None:            # new cases first; then cases changed checks may now pass (no model call)
                case = next((c for c in KYC_CASES if ai_agent.needs_recheck(c) and c["id"] not in AI_AGENT["busy"]), None)
                if case is not None:
                    _agent_recheck(case)
                    continue
                case = next((c for c in KYC_CASES if ai_agent.needs_address_update(c) and c["id"] not in AI_AGENT["busy"]), None)
                if case is not None:        # then addresses confirmed before the agent wrote them back
                    _agent_address(case)
                    continue
            if case is None:            # then verdicts for earlier reads, one try per case per run
                case = next((c for c in KYC_CASES if ai_agent.needs_verdict(c) and c["id"] not in AI_AGENT["busy"]
                             and c["id"] not in AI_AGENT["tried"]), None)
                backfill = case is not None
                if backfill:
                    AI_AGENT["tried"].add(case["id"])
            if case is None:
                break
            AI_AGENT["busy"].add(case["id"])
        if backfill:
            try:
                _agent_verdict(case)
            except Exception:        # one bad case must not stop the queue
                app.logger.exception("AI Agent verdict failed on %s", case["id"])
                case["ai_agent"]["verdict"] = {"error": "internal error", "at": dates.now_str(), "model": ai_agent.MODEL}
            finally:
                AI_AGENT["busy"].discard(case["id"])
            continue
        try:
            perceived = ai_agent.perceive(case, _render_document, wc=_linked(case)[1])
            with AI_AGENT_LOCK:
                if ai_agent.eligible(case) and not case.get("ai_agent"):
                    _agent_finish(case, perceived)
        except Exception:            # one bad case must not stop the queue
            app.logger.exception("AI Agent failed on %s", case["id"])
            case["ai_agent"] = {"decision": "human", "error": "internal error", "reasons": ["AI Agent failed on this case"],
                                "checks": [], "read": {}, "at": dates.now_str(), "model": ai_agent.MODEL, "seconds": 0}
        finally:
            AI_AGENT["busy"].discard(case["id"])
    with AI_AGENT_LOCK:
        AI_AGENT["threads"] = [t for t in AI_AGENT["threads"] if t is not threading.current_thread() and t.is_alive()]
        if not AI_AGENT["threads"] and AI_AGENT["running"]:
            AI_AGENT["running"] = False
            AI_AGENT["stopped"] = "Queue finished"
            AI_AGENT["tried"] = set()       # the next run tries a failed verdict once more
            _bump_state()


def _agent_start(by):
    with AI_AGENT_LOCK:
        if AI_AGENT["running"]:
            return
        AI_AGENT.update(running=True, started_by=by, started_at=dates.now_str(), stopped="", tried=set())
        AI_AGENT["threads"] = [threading.Thread(target=_agent_worker, name=f"ai-agent-{i}", daemon=True)
                               for i in range(AI_AGENT_WORKERS)]
        for t in AI_AGENT["threads"]:
            t.start()


@app.before_request
def _agent_autostart():
    """The agent starts by itself once per serving process, when the model
    is configured (never under the test client)."""
    if AI_AGENT["autostarted"] or app.testing:
        return
    AI_AGENT["autostarted"] = True
    _screen_resume()
    if ai_review.configured() and CASE_BOOK != "screening":
        _agent_start("System")


@app.route("/ai-agent/start", methods=["POST"])
@role_required("QC")
def ai_agent_start():
    if not ai_review.configured():
        flash("The AI Agent needs LLM_FOUNDRY_KEY (see .env.example).", "error")
        return redirect(url_for("ai_screening_page"))
    _agent_start(_actor())
    flash(f"AI Agent running: {AI_AGENT_WORKERS} cases at a time.", "success")
    return redirect(url_for("ai_screening_page"))


@app.route("/ai-agent/pause", methods=["POST"])
@role_required("QC")
def ai_agent_pause():
    AI_AGENT.update(running=False, stopped=f"Paused by {display_name(_actor())}")
    flash("AI Agent pausing: the cases it is reading now finish, then it stops.", "success")
    return redirect(url_for("ai_screening_page"))


# ─── Upload cases: new onboarding rows in the reconciliation log's layout ────
@app.route("/kyc/upload", methods=["GET", "POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_upload():
    if CASE_BOOK != "file":
        flash("Uploading cases works on the reconciliation-log book (CONSOLE_CASES=file).", "error")
        return redirect(url_for("kyc_list"))
    if request.method == "GET":
        batches = sorted(StateStore.records("upload"), key=lambda b: b.get("at", ""), reverse=True)
        return render_template("kyc_upload.html", columns=recon_book.UPLOAD_COLUMNS, optional=recon_book.HISTORY_COLUMNS,
                               document_columns=recon_book.DOCUMENT_COLUMNS + recon_book.TYPE_COLUMNS,
                               max_rows=recon_book.UPLOAD_MAX_ROWS, batches=batches, agent_ready=ai_review.configured(),
                               ocr_ready=ocr.available())
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a CSV file to upload.", "error")
        return redirect(url_for("kyc_upload"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("The file is not UTF-8 text; save it as CSV UTF-8 and try again.", "error")
        return redirect(url_for("kyc_upload"))
    ok, rows = _run_action(recon_book.read_upload, text)
    if not ok:
        return redirect(url_for("kyc_upload"))
    zipped, zip_skipped = {}, []
    z = request.files.get("images")
    if z and z.filename:
        ok, res = _run_action(case_documents.read_zip, z.read())
        if not ok:
            return redirect(url_for("kyc_upload"))
        zipped, zip_skipped = res
    seen, fresh, skipped = set(KYC_BY_ID), [], []
    for i, r in enumerate(rows, start=2):
        cid = r["kyc_case_id"]
        if not cid:
            skipped.append(f"line {i}: no case id")
        elif cid in seen:
            skipped.append(f"line {i}: {cid} already exists")
        else:
            seen.add(cid)
            fresh.append(r)
    cases, failed = _add_uploaded(fresh) if fresh else ([], [])
    bad = {line for line, _ in failed}
    skipped += [f"line {line}: {why}" for line, why in failed]
    kept = [r for i, r in enumerate(fresh, start=2) if i not in bad] if failed else fresh
    if cases:
        now = dates.now_str()
        _save("upload", {"id": f"upload-{now}-{len(StateStore.records('upload')) + 1}", "file": f.filename, "by": _actor(),
                         "at": now, "rows": kept, "cases": [c["id"] for c in cases]})
        attached = _attach_zipped(cases, zipped, zip_skipped)
        auto = sum(1 for c in cases if c["screening"].get("workflow") == "auto-verified")
        reading = any(ai_agent.eligible(c) for c in cases) and ai_review.configured()
        if reading:
            _agent_start("upload")
        waiting = sum(1 for c in cases if c.get("awaiting_documents"))
        flash(f"{len(cases)} case(s) added from {f.filename}: {auto} auto-verified"
              + (" — the AI Agent is reading them now" if reading else "")
              + f", {len(cases) - auto} for analysts."
              + (f" {attached} image(s) from {z.filename} put on their cases." if z and z.filename else "")
              + (f" {waiting} case(s) wait for their images (Upload images on the case)." if waiting else "")
              + (f" Skipped {len(skipped)}: {'; '.join(skipped[:5])}" if skipped else "")
              + (f" Images skipped {len(zip_skipped)}: {'; '.join(zip_skipped[:5])}" if zip_skipped else ""),
              "success")
        return redirect(url_for("kyc_list", tags="Uploaded"))
    flash("No case was added. " + "; ".join(skipped[:8]), "error")
    return redirect(url_for("kyc_upload"))


def _attach_zipped(cases, zipped, skipped):
    """The ZIP's images onto the cases this upload made, each checked and
    cleaned as one uploaded on the case (case_documents.attach); a proof of
    address takes the type its row names (else "other", for an analyst to
    set). Images for any other case id are reported, not attached. Returns
    how many images went on."""
    attached = 0
    for case in cases:
        files = zipped.pop(case["id"].upper(), {})
        images = {}
        for slot, (blob, name) in files.items():
            try:
                jpeg, info = uploads.prepare(blob, name)
            except WorkflowError as exc:
                skipped.append(str(exc))
                continue
            images[slot] = (jpeg, info, name)
        if not images:
            continue
        poa = case_documents.poa_details({}, case, strict=False) if "poa" in images else None
        try:
            case_documents.attach(case, images, _actor(), poa)
        except WorkflowError as exc:
            skipped.append(f"{case['id']}: {exc}")
            continue
        _save("kyc", case)
        attached += len(images)
    skipped += [f"{cid}: no case of that id in this upload" for cid in zipped]
    return attached


@app.route("/kyc/upload/<batch_id>/remove", methods=["POST"])
@role_required("L1", "L2", "EDD", "QC")
def kyc_upload_remove(batch_id):
    """Remove one upload: its cases go, with everything done on them; the
    batch stays on the Upload page marked removed, by whom and when."""
    batch = next((b for b in StateStore.records("upload") if b.get("id") == batch_id and not b.get("removed_at")), None)
    if batch is None:
        flash("That upload is not there any more; it may have been removed already.", "error")
        return redirect(url_for("kyc_upload"))
    with AI_AGENT_LOCK:
        reading = next((cid for cid in batch["cases"] if cid in AI_AGENT["busy"]), None)
        if reading:
            flash(f"The AI Agent is reading {reading} right now. Remove the upload again in a minute.", "error")
            return redirect(url_for("kyc_upload"))
        gone = _remove_uploaded(batch["cases"])
    batch.update(removed_at=dates.now_str(), removed_by=_actor())
    _save("upload", batch)
    app.logger.info("Upload %s (%s) removed by %s: %d case(s).", batch_id, batch.get("file"), _actor(), gone)
    flash(f"Removed the upload {batch.get('file', '')}: {gone} case(s) and their screening records.", "success")
    return redirect(url_for("kyc_upload"))


def _sample_rows():
    nums = [int(m.group(1)) for c in KYC_CASES for m in [re.search(r"(\d+)$", c["id"])] if m]
    return recon_book.sample_rows(max(nums, default=4010) + 1, taken=set(KYC_BY_ID))


@app.route("/kyc/upload/sample.csv")
def kyc_upload_sample():
    """Five new cases in the log's layout. ?images=1: without the columns the
    documents are drawn from, for an upload whose images come in the ZIP
    (/kyc/upload/sample-images.zip, the same five cases)."""
    rows = _sample_rows()
    if request.args.get("images") == "1":
        columns = list(recon_book.UPLOAD_COLUMNS + recon_book.TYPE_COLUMNS + recon_book.HISTORY_COLUMNS)
        return _csv_response("new_cases_for_images_sample.csv", columns, [[r[c] for c in columns] for r in rows])
    columns = list(recon_book.UPLOAD_COLUMNS + recon_book.DOCUMENT_COLUMNS + recon_book.TYPE_COLUMNS + recon_book.HISTORY_COLUMNS)
    return _csv_response("new_cases_sample.csv", columns, [[r[c] for c in columns] for r in rows])


@app.route("/kyc/upload/sample-images.zip")
def kyc_upload_sample_images():
    """JPEG photos of the five sample cases' documents (drawn, so fictional),
    named for the ZIP upload: what a real batch of ID images looks like."""
    return Response(recon_book.sample_images(_sample_rows()), mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="new_cases_images_sample.zip"'})


@app.route("/routing/capacity", methods=["POST"])
@role_required("QC")
def routing_capacity():
    a = ANALYST_BY_KEY.get(request.form.get("key", ""))
    if not a:
        flash("Unknown analyst.", "error")
        return redirect(url_for("routing_console"))
    ok, res = _run_action(routing.set_capacity, a, request.form.get("capacity", ""))
    if ok:
        _save("analyst", a)
        flash(f"{a['name']}: capacity {res[0]} → {res[1]}.", "success")
    return redirect(url_for("routing_console"))


# ─── Architecture: what the console is built of, and the models evaluated ───
@app.route("/architecture")
def architecture():
    labels = {m["id"]: m["label"] for m in benchmark.MODELS}
    reader, decider = SCREEN_SETTINGS["reader_model"], SCREEN_SETTINGS["decision_model"]
    t = upload_eval.totals(_scans())
    headline = (f"{t['read']} uploaded ID{'s' if t['read'] != 1 else ''} read, {t['decided']} hit{'s' if t['decided'] != 1 else ''} "
                f"decided" + (f", the analysts agreeing with the AI on {t['agreed']} of the {t['confirmed']} they gave an outcome"
                              if t["confirmed"] else "") if t["uploads"] else "")
    labels[AZURE_READER["id"]] = AZURE_READER["label"]
    return render_template("architecture.html", reader_label=labels.get(reader, reader), decider_label=labels.get(decider, decider),
                           llm_ready=ai_review.configured(), wc_live=id_screening.live(SCREEN_SETTINGS), headline=headline,
                           az_live=azure_docintel.configured(SCREEN_SETTINGS["azure"]), dd_live=didit.configured(SCREEN_SETTINGS["didit"]),
                           intake_n=len(SCREEN_SETTINGS.get("intake_keys") or []))


# ─── Scan ID: an uploaded ID read, screened on World-Check, decided by the AI ──
# Each upload is a case (StateStore kind "scan"): the read (documents/id_scan),
# the World-Check hits (id_screening.search: World-Check One once a key is on
# Settings, else the stand-in watchlist), the model's decision on each hit
# under the current SOP, and the analyst's confirmation. The model calls run
# on worker threads so the upload returns at once; a record is only changed
# under _SCREEN_LOCK, read fresh from the store, so a decision landing while an
# analyst confirms another hit loses neither.
_SCREEN_LOCK = threading.Lock()
_SCREEN_JOBS = queue.Queue()
_SCREEN_WORKERS = []
SCREEN_WORKERS = 3


def _scans():
    return sorted(StateStore.records("scan"), key=lambda r: r.get("at", ""), reverse=True)


def _scan_or_none(scan_id):
    return next((r for r in StateStore.records("scan") if r.get("id") == scan_id), None)


def _scan(scan_id):
    return _scan_or_none(scan_id) or abort(404)


def _sop_stored():
    return StateStore.records(id_screening.SOP_KIND)


def _screen_update(scan_id, change):
    """Apply `change` to the stored record and save it; the record, or None."""
    with _SCREEN_LOCK:
        rec = _scan_or_none(scan_id)
        if rec is None:
            return None
        change(rec)
        _save("scan", rec)
        return rec


def _hit(rec, uid):
    return next((h for h in (rec.get("screening") or {}).get("hits") or [] if h["uid"] == uid), None)


def _screen_job(job):
    """One hit decided by the model; dropped if the hit was queued again
    (a newer SOP) or confirmed meanwhile."""
    scan_id, uid, token = job
    rec = _scan_or_none(scan_id)
    hit = rec and _hit(rec, uid)
    if not hit or (hit.get("ai") or {}).get("job") != token or hit.get("resolution"):
        return
    ai = id_screening.decide(scan_id, rec["screening"]["person"], hit, id_screening.sop_current(_sop_stored()),
                             dict(SCREEN_SETTINGS))

    def land(r):
        h = _hit(r, uid)
        if h and (h.get("ai") or {}).get("job") == token and not h.get("resolution"):
            h["ai"] = dict(ai, job=token)
            id_screening.auto_verify(r, SCREEN_SETTINGS)
    _screen_update(scan_id, land)


def _screen_worker():
    while True:
        job = _SCREEN_JOBS.get()
        try:
            _screen_job(job)
        except Exception:                   # one failed job never stops the worker
            app.logger.exception("Screening decision %s failed", job)
        finally:
            _SCREEN_JOBS.task_done()


def _screen_queue(scan_id, uids=None, why=""):
    """Send the unconfirmed hits (or those of `uids`) of an open case to the
    model. Returns how many were sent."""
    jobs = []

    def mark(rec):
        for h in id_screening.open_hits(rec):
            if uids is None or h["uid"] in uids:
                h["ai"] = {"status": "pending", "job": secrets.token_hex(6), "queued_at": dates.now_str(), "why": why}
                jobs.append((scan_id, h["uid"], h["ai"]["job"]))
    _screen_update(scan_id, mark)
    if app.testing:                         # the suite wants the answer before the next line
        for j in jobs:
            _screen_job(j)
        return len(jobs)
    while len(_SCREEN_WORKERS) < SCREEN_WORKERS:
        t = threading.Thread(target=_screen_worker, name=f"screening-{len(_SCREEN_WORKERS) + 1}", daemon=True)
        _SCREEN_WORKERS.append(t)
        t.start()
    for j in jobs:
        _SCREEN_JOBS.put(j)
    return len(jobs)


def _screen_resume():
    """After a restart: hits that were waiting on the model are sent again."""
    for rec in _scans():
        waiting = [h["uid"] for h in id_screening.open_hits(rec) if (h.get("ai") or {}).get("status") == "pending"]
        if waiting:
            _screen_queue(rec["id"], waiting, why="restart")
        if (rec.get("idv") or {}).get("status") == "pending":
            _idv_start(rec["id"])


def _screen_search(rec):
    """The World-Check search for the person the ID names (hits confirmed
    before keep their confirmation)."""
    who = id_screening.person(rec.get("read"))
    hits, source, error, csid = id_screening.search(who, SCREEN_SETTINGS)
    before = {h["uid"]: h for h in (rec.get("screening") or {}).get("hits") or []}
    for h in hits:
        if before.get(h["uid"], {}).get("resolution"):
            h.update(ai=before[h["uid"]].get("ai"), resolution=before[h["uid"]]["resolution"])
    rec["screening"] = {"person": who, "source": source, "source_label": id_screening.source_label(SCREEN_SETTINGS),
                        "at": dates.now_str(), "hits": hits, "error": error, "case_system_id": csid,
                        "searched": {k: who[k] for k in ("name", "dob", "sex", "nationality")}}


@app.route("/scan", methods=["GET", "POST"])
def scan_id():
    """The cases, and the upload: a vision model reads the ID
    (documents/id_scan), the person is searched on World-Check, and the
    model decides each hit under the SOP (id_screening)."""
    if request.method == "GET":
        scans = _scans()
        status = request.args.get("status", "")
        kind = lambda s: ("open" if s.get("status", "open") == "open"                       # noqa: E731
                          else "auto" if (s.get("closed") or {}).get("auto") else "closed")
        shown = [s for s in scans if not status or kind(s) == status]
        return render_template("scan.html", models=[m for m in _readers() if m["id"] != OCR_READER["id"]],
                               default_model=SCREEN_SETTINGS["reader_model"], ocr_ready=ocr.available(),
                               model_ready=id_scan.model_ready(), scans=shown, status=status,
                               counts={k: sum(1 for s in scans if kind(s) == k) for k in ("open", "auto", "closed")} | {"all": len(scans)},
                               auto_on=SCREEN_SETTINGS.get("auto_verify"),
                               source=id_screening.source_label(SCREEN_SETTINGS), outcome=id_screening.outcome)
    f = request.files.get("image")
    if not f or not f.filename:
        flash("Choose a photo or scan of the ID document.", "error")
        return redirect(url_for("scan_id"))
    reader = OCR_READER["id"] if request.form.get("via") == "ocr" else (request.form.get("model") or None)
    ok, record = _run_action(_new_scan, f.read(), f.filename, reader, _actor())
    if not ok:
        return redirect(url_for("scan_id"))
    for message in (record.get("fallback"), record["error"], (record.get("screening") or {}).get("error")):
        if message:
            flash(message, "error")
    return redirect(url_for("scan_result", scan_id=record["id"]))


AZURE_READER = {"id": azure_docintel.MODEL["model"], "label": azure_docintel.MODEL["label"], "price": None}
OCR_READER = {"id": ocr.MODEL["model"], "label": ocr.MODEL["label"], "price": None, "free": True}


def _readers():
    """What can read an uploaded ID: the AI models, Azure AI Document
    Intelligence once its endpoint and key are on Settings, and OCR
    (Tesseract) when this machine has it."""
    out = list(id_scan.models())
    if azure_docintel.configured(SCREEN_SETTINGS["azure"]):
        out.append(dict(AZURE_READER, per_page=SCREEN_SETTINGS["azure"].get("price_per_page")))
    if ocr.available():
        out.append(OCR_READER)
    return out


def _read_id(jpeg, reader):
    """Scan ID's result for one image, read by `reader`. Azure's read gets
    the same checks; when Azure cannot read it, the AI model does, and the
    case says so (result["fallback"]). OCR's read is OCR's alone: when it
    cannot run, the case says why and nothing else reads it."""
    if reader == OCR_READER["id"]:
        if not ocr.available():
            return {"read": None, "model": None, "checks": [], "error": "OCR (Tesseract) is not installed on this machine: the image was not read."}
        try:
            read, used = ocr.id_read(jpeg, [k for k, _ in id_scan.FIELDS])
        except ocr.OcrError as exc:
            return {"read": None, "model": None, "checks": [], "error": str(exc)[:300]}
        return {"read": read, "model": used, "checks": id_scan.checks(read), "error": ""}
    if reader != AZURE_READER["id"]:
        return id_scan.scan(jpeg, reader)
    cfg = SCREEN_SETTINGS["azure"]
    why = "its endpoint and key are not on Settings"
    if azure_docintel.configured(cfg):
        try:
            read, used = azure_docintel.read(cfg, jpeg, [k for k, _ in id_scan.FIELDS])
            return {"read": read, "model": used, "checks": id_scan.checks(read), "error": ""}
        except azure_docintel.AzureError as exc:
            why = str(exc)
    result = id_scan.scan(jpeg, id_scan.DEFAULT_MODEL)
    result["fallback"] = f"Azure AI Document Intelligence did not read the ID ({why}): the AI model read it instead."
    return result


def _new_scan(data, filename, reader, actor, reference=""):
    """A new screening case from an ID image, for the upload page and the
    intake API: prepared, read, searched on World-Check, and the rule run on
    a case with no hit; its hits go to the AI and the ID to the verification
    service in the background. WorkflowError when the image is unusable."""
    jpeg, info = uploads.prepare(data, "The ID image")
    result = _read_id(jpeg, reader or SCREEN_SETTINGS["reader_model"])
    at = dates.now_str()
    sid = "SCAN-" + "".join(ch for ch in at if ch.isdigit()) + "-" + info["sha256"][:6].upper()
    while _scan_or_none(sid):                       # the same image twice in a second (a client's retry)
        sid += "-" + secrets.token_hex(2).upper()
    record = dict(result, id=sid, at=at, by=actor, file=(filename or "id")[:120], info=info, status="open",
                  image=uploads.store("scans", sid, jpeg), verdict=id_scan.verdict(result))
    if reference:
        record["reference"] = str(reference)[:120]
    if id_screening.is_id(result["read"]):
        _screen_search(record)
        if _idv_wanted():
            record["idv"] = {"provider": "Didit", "status": "pending", "at": at}
        id_screening.auto_verify(record, SCREEN_SETTINGS)          # a case with no hit (hits wait for the AI)
    _save("scan", record)
    _screen_queue(sid, why="upload")
    if (record.get("idv") or {}).get("status") == "pending":
        _idv_start(sid)
    return record


# ─── ID verification by Didit (adapters/didit), in the background ───────────────
def _idv_wanted():
    cfg = SCREEN_SETTINGS["didit"]
    return didit.configured(cfg) and bool(cfg.get("auto", True))


def _idv_job(scan_id):
    rec = _scan_or_none(scan_id)
    if not rec:
        return
    try:
        res = didit.verify(SCREEN_SETTINGS["didit"], uploads.load(rec["image"]), rec.get("reference") or scan_id)
    except (didit.DiditError, OSError, ValueError) as exc:
        res = {"provider": "Didit", "status": "error", "error": str(exc)[:300]}
    res["at"] = dates.now_str()

    def land(r):
        r["idv"] = res
        id_screening.auto_verify(r, SCREEN_SETTINGS)
    _screen_update(scan_id, land)


def _idv_start(scan_id):
    if app.testing:                         # the suite wants the answer before the next line
        _idv_job(scan_id)
    else:
        threading.Thread(target=_idv_job, args=(scan_id,), name="didit", daemon=True).start()


@app.route("/scan/<scan_id>/idv", methods=["POST"])
def scan_idv(scan_id):
    """Verify the ID with Didit now (when it is not run on upload, or again)."""
    rec = _scan(scan_id)
    if not didit.configured(SCREEN_SETTINGS["didit"]):
        flash("Enter the Didit API key on Settings first.", "error")
    elif rec.get("status") == "closed":
        flash("The case is closed.", "error")
    else:
        _screen_update(scan_id, lambda r: r.update(idv={"provider": "Didit", "status": "pending", "at": dates.now_str()}))
        _idv_start(scan_id)
        flash("Sent to Didit.", "success")
    return redirect(url_for("scan_result", scan_id=scan_id))


# ─── The intake API: the client's onboarding system sends IDs ─────────────────
def _intake_key():
    """The intake key the request carries (X-API-Key, or Authorization:
    Bearer), or None. Keys are kept as their SHA-256 only."""
    given = (request.headers.get("X-API-Key") or "").strip()
    auth = request.headers.get("Authorization") or ""
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if not given:
        return None
    digest = hashlib.sha256(given.encode("utf-8")).hexdigest()
    return next((k for k in SCREEN_SETTINGS.get("intake_keys") or [] if hmac.compare_digest(k["hash"], digest)), None)


def _api_error(status, message):
    return jsonify({"error": message}), status


@app.route("/api/v1/cases", methods=["GET", "POST"])
def api_cases():
    """POST an ID image (multipart `image`, or JSON `image_base64`), with an
    optional `reference` (the client's own ID for the customer) and `model`:
    the case as /api/v1/cases/<id> returns it, 201. GET: the cases, or those
    of one `reference`."""
    key = _intake_key()
    if not key:
        return _api_error(401, "A valid intake API key is required: the X-API-Key header, or Authorization: Bearer.")
    if request.method == "GET":
        ref = request.args.get("reference", "")
        return jsonify({"cases": [_scan_export(r) for r in _scans() if not ref or r.get("reference") == ref][:100]})
    if request.files.get("image"):
        f = request.files["image"]
        data, name = f.read(), f.filename or "id"
        reference, reader = request.form.get("reference", ""), request.form.get("model", "")
    else:
        body = request.get_json(silent=True) or {}
        try:
            data = base64.b64decode(body.get("image_base64") or "", validate=True)
        except (binascii.Error, ValueError):
            return _api_error(400, "image_base64 is not base64.")
        name, reference, reader = str(body.get("filename") or "id.jpg"), str(body.get("reference") or ""), str(body.get("model") or "")
    if not data:
        return _api_error(400, "Send the ID image: a multipart field `image`, or `image_base64` in a JSON body.")
    if reader and reader not in {m["id"] for m in _readers()}:
        return _api_error(400, f"Unknown model {reader!r}: one of {', '.join(m['id'] for m in _readers())}.")
    try:
        rec = _new_scan(data, name, reader or None, f"Intake API ({key['name']})", reference)
    except WorkflowError as exc:
        return _api_error(400, str(exc))
    key["last_used"] = dates.now_str()
    _save("settings", SCREEN_SETTINGS)
    return jsonify(_scan_export(_scan(rec["id"]))), 201, {"Location": url_for("api_case", scan_id=rec["id"])}


@app.route("/api/v1/cases/<scan_id>")
def api_case(scan_id):
    """One case: its status, the read, the World-Check hits with the AI's
    suggestion and the outcome, the ID verification."""
    if not _intake_key():
        return _api_error(401, "A valid intake API key is required: the X-API-Key header, or Authorization: Bearer.")
    rec = _scan_or_none(scan_id)
    return jsonify(_scan_export(rec)) if rec else _api_error(404, f"No case {scan_id}.")


@app.route("/scan/<scan_id>")
def scan_result(scan_id):
    rec = _scan(scan_id)
    return render_template("scan_result.html", s=rec, fields=id_scan.FIELDS, points=id_screening.progress(rec),
                           didit_ready=didit.configured(SCREEN_SETTINGS["didit"]), idv_sandbox=didit.is_sandbox(rec.get("idv") or {}),
                           idv_diffs=id_screening.idv_differences(rec),
                           outcomes=id_screening.ALERT_OUTCOME, sop=id_screening.sop_current(_sop_stored()))


@app.route("/scan/<scan_id>/screening")
def scan_alerts(scan_id):
    """The screening case as the console's name-screening page shows one:
    basic and screening information, a row per World-Check hit (the AI's
    assessment, the analyst's outcome, the record it opens to)."""
    rec = _scan(scan_id)
    wc = id_screening.wc_view(rec)
    who = (rec.get("screening") or {}).get("person") or id_screening.person(rec.get("read"))
    client = {"country": who.get("issuing_country", ""), "nationality": who.get("nationality", "")}
    details = {m["id"]: {"m": m, "compare": worldcheck.comparison_rows(wc, m, client)} for m in wc["matches"]}
    hits = (rec.get("screening") or {}).get("hits") or []
    counts = {"All": len(hits), "Pending": 0, **{o: 0 for o in reversed(id_screening.FROM_ALERT_OUTCOME)}}
    for h in hits:
        counts[id_screening.ALERT_OUTCOME[h["resolution"]["status"]] if h.get("resolution") else "Pending"] += 1
    return render_template("scan_alerts.html", s=rec, wc=wc, who=who, details=details, counts=counts,
                           outcomes=id_screening.ALERT_OUTCOME, sop=id_screening.sop_current(_sop_stored()),
                           wc_group=SCREEN_SETTINGS["world_check"].get("group_id", "") if id_screening.live(SCREEN_SETTINGS) else "")


@app.route("/scan/<scan_id>/hits/outcome", methods=["POST"])
def scan_hits_outcome(scan_id):
    """Change Alert Outcome: one outcome, risk, reason and comment written to
    every ticked hit (the case closes when the last is confirmed)."""
    _scan(scan_id)
    uids = request.form.getlist("alert_id")
    status = id_screening.FROM_ALERT_OUTCOME.get(request.form.get("outcome", ""), "")
    err = []
    if not uids:
        err.append("Tick at least one alert first.")
    elif not request.form.get("comment", "").strip():
        err.append("Write a comment.")

    def apply(rec):
        for uid in uids:
            try:
                id_screening.resolve(rec, uid, status, request.form.get("comment", ""), _actor(),
                                     risk=request.form.get("risk", ""), reason=request.form.get("reason", ""))
            except ValueError as exc:
                err.append(str(exc))
                return
    rec = _scan(scan_id) if err else _screen_update(scan_id, apply)
    if err:
        flash(err[0], "error")
    elif rec.get("status") == "closed":
        flash(f"Alert outcome saved. Every hit is confirmed: case closed as {rec['closed']['outcome']}.", "success")
    else:
        flash(f"Alert outcome saved on {len(uids)} alert{'s' if len(uids) > 1 else ''}.", "success")
    return redirect(url_for("scan_alerts", scan_id=scan_id))


@app.route("/scan/<scan_id>/ai", methods=["POST"])
def scan_ai(scan_id):
    """AI assessment: the ticked hits (or every unconfirmed one) to the AI again."""
    _scan(scan_id)
    n = _screen_queue(scan_id, request.form.getlist("alert_id") or None, why="asked again")
    flash(f"Sent to the AI again: {n} alert{'s' if n != 1 else ''}." if n else "Nothing to send: every hit is confirmed, or the case is closed.",
          "success" if n else "error")
    return redirect(url_for("scan_alerts", scan_id=scan_id))


@app.route("/scan/<scan_id>/screen", methods=["POST"])
def scan_screen(scan_id):
    """Search World-Check again (after a failed call, or a change on Settings)."""
    rec = _scan(scan_id)
    if rec.get("status") == "closed" or not id_screening.is_id(rec.get("read")):
        flash("Only an open case with a read ID can be searched again.", "error")
        return redirect(url_for("scan_result", scan_id=scan_id) + "#screening")
    rec = _screen_update(scan_id, lambda r: (_screen_search(r), id_screening.auto_verify(r, SCREEN_SETTINGS)))
    n = _screen_queue(scan_id, why="searched again")
    flash(rec["screening"]["error"] or f"Searched again on {rec['screening']['source_label']}: "
          f"{len(rec['screening']['hits'])} hit(s); {n} sent to the AI.", "error" if rec["screening"]["error"] else "success")
    return redirect(url_for("scan_result", scan_id=scan_id) + "#screening")


@app.route("/scan/<scan_id>/hits/<uid>/ai", methods=["POST"])
def scan_hit_ai(scan_id, uid):
    _scan(scan_id)
    n = _screen_queue(scan_id, [uid], why="asked again")
    flash("Sent to the AI again." if n else "That hit is confirmed already, or the case is closed.", "success" if n else "error")
    if request.form.get("back") == "wc":
        return redirect(url_for("wc_match", wc_case_id=scan_id, match_id=uid))
    return redirect(url_for("scan_alerts", scan_id=scan_id))


@app.route("/scan/<scan_id>/close", methods=["POST"])
def scan_close(scan_id):
    """Close a case with no hit (the analyst confirms there is none)."""
    _scan(scan_id)
    err = []

    def apply(rec):
        try:
            id_screening.close(rec, _actor(), request.form.get("note", ""))
        except ValueError as exc:
            err.append(str(exc))
    rec = _screen_update(scan_id, apply)
    flash(err[0] if err else f"Case closed as {rec['closed']['outcome']}.", "error" if err else "success")
    return redirect(url_for("scan_result", scan_id=scan_id) + "#screening")


@app.route("/scan/<scan_id>/image")
def scan_image(scan_id):
    """The scanned image (a real person's ID: never cached)."""
    rec = _scan(scan_id)
    try:
        data = uploads.load(rec["image"])
    except (OSError, WorkflowError):
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff",
                                                          "Content-Disposition": f'inline; filename="{scan_id}.jpg"'})


@app.route("/scan/<scan_id>/data.json")
def scan_json(scan_id):
    """The case for another system: the document, its fields, the checks,
    the World-Check hits with the AI's decision and the analyst's."""
    body = _scan_export(_scan(scan_id))
    return Response(json.dumps(body, indent=2, ensure_ascii=False), mimetype="application/json",
                    headers={"Cache-Control": "no-store, private", "Content-Disposition": f'attachment; filename="{scan_id}.json"'})


def _scan_export(rec):
    """The case as another system reads it (Download JSON, the intake API)."""
    read = rec.get("read") or {}
    sc = rec.get("screening") or {}
    body = {"scan_id": rec["id"], "scanned_at": rec["at"], "verdict": rec["verdict"], "status": rec.get("status", "open"),
            "outcome": id_screening.outcome(rec) if rec.get("status") == "closed" else None,
            "document_type": read.get("document_type", ""), "issuing_country": read.get("issuing_country", ""),
            "fields": read.get("fields", {}), "uncertain": read.get("uncertain", []), "mrz": read.get("mrz", []),
            "checks": rec.get("checks", []), "model": rec.get("model"),
            "screening": sc and {"source": sc.get("source"), "searched": sc.get("searched"), "at": sc.get("at"), "error": sc.get("error"),
                                 "hits": [{k: h.get(k) for k in ("uid", "name", "aka", "dob_text", "places", "category", "score",
                                                                  "strength", "dob_result", "country_result", "ai", "resolution")}
                                          for h in sc.get("hits") or []]},
            "closed": rec.get("closed"), "reference": rec.get("reference", ""), "uploaded_by": rec.get("by", ""),
            "auto_verified": bool((rec.get("closed") or {}).get("auto")), "qc_sample": bool(rec.get("qc_sample")),
            "id_verification": rec.get("idv") and dict(rec["idv"], sandbox=didit.is_sandbox(rec["idv"]),
                                                       approved=id_screening.idv_approved(rec)[0],
                                                       differs=[{"field": w, "verification": a, "id": b}
                                                                for w, a, b in id_screening.idv_differences(rec)]),
            "reader_fallback": rec.get("fallback", "")}
    return body


@app.route("/scan/<scan_id>/delete", methods=["POST"])
def scan_delete(scan_id):
    rec = _scan(scan_id)
    try:
        os.remove(os.path.join(uploads.base(), rec["image"]))
    except (OSError, KeyError):
        pass
    with _SCREEN_LOCK:
        StateStore.delete("scan", [scan_id])
    _bump_state()
    flash(f"Case {scan_id} and its image deleted.", "success")
    return redirect(url_for("scan_id"))


# ─── The screening SOP and settings ─────────────────────────────────────────────
@app.route("/screening/sop", methods=["GET", "POST"])
def screening_sop():
    """The SOP the AI decides hits by: read it, or (the QC lead) save a new
    version, which sends every open case's unconfirmed hits to the AI again."""
    stored = _sop_stored()
    if request.method == "POST":
        if "QC" not in _levels():
            flash("Only the QC lead saves a new SOP version.", "error")
            return redirect(url_for("screening_sop"))
        try:
            rec = id_screening.sop_new(stored, request.form.get("text", ""), request.form.get("note", ""), _actor())
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("screening_sop"))
        _save(id_screening.SOP_KIND, rec)
        sent = sum(_screen_queue(s["id"], why=f"SOP {rec['version']}") for s in _scans() if id_screening.open_hits(s))
        flash(f"SOP {rec['version']} saved and in force. "
              + (f"{sent} unconfirmed hit(s) on open cases sent to the AI again under it." if sent else "No open case has an unconfirmed hit."),
              "success")
        return redirect(url_for("screening_sop"))
    versions = id_screening.sop_versions(stored)
    return render_template("screening_sop.html", current=versions[-1], versions=list(reversed(versions)),
                           can_edit="QC" in _levels())


def _masked(value):
    return ("•" * 8 + value[-4:]) if value and len(value) > 4 else ("•" * len(value or ""))


@app.route("/screening/settings", methods=["GET", "POST"])
def screening_settings():
    """The models, the search, the World-Check One account (QC lead edits)."""
    s = SCREEN_SETTINGS
    if request.method == "POST":
        if "QC" not in _levels():
            flash("Only the QC lead changes the settings.", "error")
            return redirect(url_for("screening_settings"))
        f = request.form
        reader_ids = {m["id"] for m in _readers()}
        decision_ids = {m["id"] for m in id_screening.decision_models()}
        try:
            examples = int(f.get("examples", s["examples"]))
            threshold = int(f.get("name_threshold", s["name_threshold"]))
            max_hits = int(f.get("max_hits", s["max_hits"]))
        except ValueError:
            flash("Examples, name score and hits are whole numbers.", "error")
            return redirect(url_for("screening_settings"))
        if not (0 <= examples <= 12 and 50 <= threshold <= 100 and 1 <= max_hits <= 20):
            flash("Examples 0-12, name score 50-100, hits 1-20.", "error")
            return redirect(url_for("screening_settings"))
        if f.get("auto_form"):                      # the auto-verify card was on the page: unticked boxes are off
            try:
                confidence, qc_pct = int(f.get("auto_min_confidence", 0)), int(f.get("auto_qc_pct", 0))
            except ValueError:
                flash("The AI's confidence and the QC sample are whole numbers.", "error")
                return redirect(url_for("screening_settings"))
            if not (50 <= confidence <= 100 and 0 <= qc_pct <= 100):
                flash("Confidence 50-100, QC sample 0-100%.", "error")
                return redirect(url_for("screening_settings"))
            s.update(auto_verify=bool(f.get("auto_verify")), auto_no_hit=bool(f.get("auto_no_hit")),
                     auto_dob_differs=bool(f.get("auto_dob_differs")), auto_min_confidence=confidence, auto_qc_pct=qc_pct,
                     auto_person_lists=", ".join(x.strip() for x in f.get("auto_person_lists", "").split(",") if x.strip()))
        s.update(reader_model=f.get("reader_model") if f.get("reader_model") in reader_ids else s["reader_model"],
                 decision_model=f.get("decision_model") if f.get("decision_model") in decision_ids else s["decision_model"],
                 examples=examples, name_threshold=threshold, max_hits=max_hits)
        wc = s["world_check"]
        for key in ("api_key", "group_id", "host", "base_path", "screen_path", "groups_path", "profile_path"):
            if key in f:
                wc[key] = f.get(key, "").strip() or (world_check.DEFAULTS[key] if key not in ("api_key", "group_id") else "")
        if f.get("api_secret", "").strip():         # blank keeps the stored secret
            wc["api_secret"] = f["api_secret"].strip()
        if f.get("forget_secret"):
            wc["api_secret"] = ""
        if f.get("services_form"):                  # the Azure and Didit cards were on the page
            err = _services_update(s, f)
            if err:
                flash(err, "error")
                return redirect(url_for("screening_settings"))
        _save("settings", s)
        flash("Settings saved. World-Check: " + id_screening.source_label(s) + ".", "success")
        return redirect(url_for("screening_settings"))
    return render_template("screening_settings.html", s=s, wc=s["world_check"], secret_shown=_masked(s["world_check"]["api_secret"]),
                           az=s["azure"], az_key_shown=_masked(s["azure"]["api_key"]), az_live=azure_docintel.configured(s["azure"]),
                           dd=s["didit"], dd_key_shown=_masked(s["didit"]["api_key"]), dd_live=didit.configured(s["didit"]),
                           intake_keys=s.get("intake_keys") or [], new_key=session.pop("new_intake_key", None),
                           api_base=request.url_root.rstrip("/") + "/api/v1",
                           readers=_readers(), deciders=id_screening.decision_models(), live=id_screening.live(s),
                           source=id_screening.source_label(s), can_edit="QC" in _levels(), model_ready=id_scan.model_ready(),
                           qc_names=_analysts_with(["QC"], _tenant()), watchlist_size=len(watchlist.profiles()))


def _services_update(s, f):
    """Azure's and Didit's settings from the form ("" when saved, else what is wrong)."""
    az, dd = s["azure"], s["didit"]
    try:
        floor, page_price, call_price = (float(f.get("az_min_confidence", az["min_confidence"])),
                                         float(f.get("az_price_per_page", az["price_per_page"]) or 0),
                                         float(f.get("dd_price_per_call", dd["price_per_call"]) or 0))
    except ValueError:
        return "The confidence floor and the prices are numbers."
    if not (0 <= floor <= 1 and page_price >= 0 and call_price >= 0):
        return "The confidence floor is 0-1; prices are 0 or more."
    az.update(endpoint=f.get("az_endpoint", "").strip(), api_version=f.get("az_api_version", "").strip() or azure_docintel.DEFAULTS["api_version"],
              model_id=f.get("az_model_id", "").strip() or azure_docintel.DEFAULTS["model_id"], min_confidence=floor, price_per_page=page_price)
    dd.update(base_url=f.get("dd_base_url", "").strip() or didit.DEFAULTS["base_url"], path=f.get("dd_path", "").strip() or didit.DEFAULTS["path"],
              auto=bool(f.get("dd_auto")), document_liveness=bool(f.get("dd_liveness")), price_per_call=call_price)
    for cfg, field, forget in ((az, "az_api_key", "az_forget"), (dd, "dd_api_key", "dd_forget")):
        if f.get(field, "").strip():                # blank keeps the stored key
            cfg["api_key"] = f[field].strip()
        if f.get(forget):
            cfg["api_key"] = ""
    if s["reader_model"] == AZURE_READER["id"] and not azure_docintel.configured(az):
        s["reader_model"] = id_scan.DEFAULT_MODEL
    return ""


@app.route("/screening/settings/azure-test", methods=["POST"])
def screening_azure_test():
    """List the Azure resource's models with the saved endpoint and key."""
    cfg = SCREEN_SETTINGS["azure"]
    if not azure_docintel.configured(cfg):
        flash("Enter and save the Azure endpoint and key first.", "error")
        return redirect(url_for("screening_settings") + "#settings-azure")
    try:
        found = azure_docintel.models(cfg)
    except azure_docintel.AzureError as exc:
        flash(f"Connection failed: {exc}", "error")
        return redirect(url_for("screening_settings") + "#settings-azure")
    has = cfg["model_id"] in found
    flash(f"Connected to Azure AI Document Intelligence: {len(found)} models"
          + ("." if has else f"; {cfg['model_id']} is not among them: check the model on Settings."), "success" if has else "error")
    return redirect(url_for("screening_settings") + "#settings-azure")


@app.route("/screening/settings/intake-keys", methods=["POST"])
def intake_key_new():
    """A new intake API key, shown once (the QC lead)."""
    if "QC" not in _levels():
        flash("Only the QC lead creates API keys.", "error")
        return redirect(url_for("screening_settings") + "#settings-intake")
    name = (request.form.get("name") or "").strip()[:60] or "Client system"
    raw = "fcc_" + secrets.token_urlsafe(24)
    SCREEN_SETTINGS.setdefault("intake_keys", []).append(
        {"id": secrets.token_hex(4), "name": name, "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(), "prefix": raw[:8],
         "created_at": dates.now_str(), "created_by": _actor(), "last_used": ""})
    _save("settings", SCREEN_SETTINGS)
    session["new_intake_key"] = {"name": name, "key": raw}
    return redirect(url_for("screening_settings") + "#settings-intake")


@app.route("/screening/settings/intake-keys/<key_id>/revoke", methods=["POST"])
def intake_key_revoke(key_id):
    if "QC" not in _levels():
        flash("Only the QC lead revokes API keys.", "error")
        return redirect(url_for("screening_settings") + "#settings-intake")
    keys = SCREEN_SETTINGS.get("intake_keys") or []
    gone = [k for k in keys if k["id"] == key_id]
    SCREEN_SETTINGS["intake_keys"] = [k for k in keys if k["id"] != key_id]
    _save("settings", SCREEN_SETTINGS)
    flash(f"Revoked the key for {gone[0]['name']}: calls with it are refused from now." if gone else "No such key.",
          "success" if gone else "error")
    return redirect(url_for("screening_settings") + "#settings-intake")


@app.route("/screening/settings/test", methods=["POST"])
def screening_settings_test():
    """Call World-Check One's groups list with the saved key and secret."""
    wc = SCREEN_SETTINGS["world_check"]
    if not world_check.configured(wc):
        flash("Enter and save the World-Check One API key and secret first.", "error")
        return redirect(url_for("screening_settings"))
    try:
        found = world_check.groups(wc)
    except world_check.WorldCheckError as exc:
        flash(f"Connection failed: {exc}", "error")
        return redirect(url_for("screening_settings"))
    names = ", ".join(f"{n} ({i})" for i, n in found[:8]) or "no group"
    flash(f"Connected to World-Check One. Groups: {names}." + ("" if wc.get("group_id") else " Enter the group ID to screen in."),
          "success")
    return redirect(url_for("screening_settings"))


# ─── Model evaluation: the Screening cases flow's two models, on the console's own uploads ───
@app.route("/model-evaluation")
def model_evaluation():
    """The Screening cases flow's two models, measured on the IDs uploaded in
    the console as they are read and decided (fincrime/upload_eval)."""
    scans = _scans()
    labels = {m["id"]: m["label"] for m in benchmark.MODELS}
    reader, decider = SCREEN_SETTINGS["reader_model"], SCREEN_SETTINGS["decision_model"]
    danger = lambda t, c: t != "False positive" and c == "False positive"      # noqa: E731
    deciding = [dict(m, view=decision_model.matrix_view(m["matrix"], None, danger)) for m in upload_eval.deciding(scans)]
    return render_template("model_evaluation.html", reading=upload_eval.reading(scans), deciding=deciding,
                           by_sop=upload_eval.by_sop(scans), totals=upload_eval.totals(scans), reader=reader, decider=decider,
                           reader_label=labels.get(reader, reader), decider_label=labels.get(decider, decider))


# ─── Analytics ─────────────────────────────────────────────────────────────────
@app.route("/analytics")
def analytics():
    vm = AnalyticsConsole.compute(KYC_CASES, ALERT_CASES, WC_CASES, _tenant(), analysts=ANALYSTS)
    return render_template("analytics.html", vm=vm)


# ─── Demo affordances ──────────────────────────────────────────────────────────
@app.route("/reset", methods=["POST"])
def reset_demo():
    """Back to the seeded case book: drop every stored action and reload."""
    if not DEMO_MODE:
        abort(404)
    if CASE_BOOK == "screening":            # its cases and their images; the SOP versions and settings stay
        scans = _scans()
        for rec in scans:
            try:
                os.remove(os.path.join(uploads.base(), rec["image"]))
            except (OSError, KeyError):
                pass
        with _SCREEN_LOCK:
            StateStore.delete("scan", [r["id"] for r in scans])
        _bump_state()
        flash(f"Demo reset: {len(scans)} case(s) and their images deleted. The SOP versions and the settings stay.", "success")
        return redirect(url_for("scan_id"))
    cleared = StateStore.clear()
    uploads.remove_all()                # every case is back on its seed, drawn documents and all
    _load_seed()
    _bump_state()
    flash("Demo reset — the seeded case book is back." if cleared else "Could not clear the stored state.",
          "success" if cleared else "error")
    return redirect(_safe_next(request.form.get("next", "")) if request.form.get("next") else url_for("kyc_list"))


# ─── Main ──────────────────────────────────────────────────────────────────────
_load_seed()

if __name__ == "__main__":
    print(f" * FinCrime Operations Hub on http://{HOST}:{PORT}  (demo password: {DEMO_PASSWORD})")
    print(f" * Console case book: {CASE_BOOK_LABEL[CASE_BOOK]}  (CONSOLE_CASES=screening|file|demo)")
    app.run(debug=DEBUG, host=HOST, port=PORT)
