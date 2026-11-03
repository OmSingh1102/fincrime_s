"""The SOP's Google Sheets, as live views of the case book (SOP 5.2.1:
"replaces ... external Google Sheets trackers with ... centralized tracking").

Four sheets appear in the SOP: the Productivity / Time Tracker every step
tells the analyst to fill in, the Fraud Tracker (4.1.3 §2.1.1),
the EDD Tracker (4.1.3 §6.1) and the Business Escalation
Tracker (4.2.2 §2.1.1 / §6.1). Their columns are reproduced verbatim; every
value is computed, so no analyst ever types into them again. CSV keeps the
spreadsheet-shaped export for anyone downstream who still wants one.
"""
import csv
import io

from fincrime.data import vocab
from fincrime.engines import worklog
from fincrime.utils import dates
from fincrime.utils.text import display_name

VIEWS = [("productivity", "Productivity / Time Tracker"), ("fraud", "Fraud Tracker"), ("edd", "Escalation Tracker")]


def _l1(case):
    l1 = (case.get("reviewers_by_level") or {}).get("L1") or []
    return display_name(l1[-1]) if l1 else (display_name(case.get("previous_reviewer", "")) if case.get("previous_reviewer") else "-")


def _l1_outcome(case):
    rows = {r["check_point"]: r for r in case.get("review_decisions", [])}
    esc = rows.get("Data Escalating L2", {})
    if esc.get("decision"):
        return f"Escalated — {case.get('escalate_reason') or esc.get('rejection_reason') or ''}".rstrip(" —")
    scr = rows.get("Data Screening", {})
    if scr.get("decision"):
        return scr["decision"]
    dc = rows.get("Data Confirming", {})
    return dc.get("decision") or "-"


def _pic(case):
    return display_name(case["current_reviewer"]) if case.get("current_reviewer") else "Unassigned"


def _wc_id(case, wc_by_kyc):
    return (wc_by_kyc.get(case["id"]) or {}).get("id", "") or "-"


class TrackerConsole:

    @staticmethod
    def compute(kyc, alerts, wc, tenant, view, now=None):
        now = now or dates.now()
        view = "edd" if view == "tts" else (view if view in dict(VIEWS) else "productivity")
        scoped = [c for c in kyc if c["tenant"] == tenant]
        wc_by_kyc = {w["kyc_case_id"]: w for w in wc}
        ttshop = tenant == "ttshop"
        tenant_label = vocab.TENANTS[tenant]["label"]

        if view == "productivity":
            title, sub = f"{tenant_label} — FinCrime Team Productivity / Time Tracker", "one row per analyst per level held; handle time is computed from the work log"
            columns = ["Case ID", "Client ID (UID)", "Client Name", "Analyst", "Level", "Assigned at", "Handed back", "Handle time (h)", "Outcome", "Status"]
            rows = []
            for c in scoped:
                for w in c.get("work", []):
                    rows.append({"case_id": c["id"], "sort": w["from"], "cells": [
                        c["id"], c["client_id"], c["client"]["legal_name"], display_name(w["analyst"]), vocab.LEVEL_LABELS.get(w["level"], w["level"]),
                        w["from"], w.get("to") or "open", f"{worklog.hours(w, now):.1f}", w.get("outcome") or "in progress", c["status"]]})
        elif view == "fraud":
            title = f"{tenant_label} — Fraud Tracker" if not ttshop else "Business Escalation — fraud cases"
            sub = "SOP 4.1.3 §2.1.1: every Fraud ID escalation, with its typology"
            columns = ["Client ID (UID)", "Case ID", "Client Name", "WC ID", "L1 Investigator", "Submission date", "L1 Outcome", "Fraud Typology", "Status"]
            rows = [{"case_id": c["id"], "sort": c["created_at"], "cells": [
                        c["client_id"], c["id"], c["client"]["legal_name"], _wc_id(c, wc_by_kyc), _l1(c), c["created_at"],
                        _l1_outcome(c), c.get("fraud_typology") or "-", c["status"]]}
                    for c in scoped if c.get("fraud_typology")]
        elif ttshop:
            title, sub = "Business Escalation Tracker", "SOP 4.2.2 §2.1.1 and §6.1: escalated businesses, their investigators and PIC"
            columns = ["Client ID (UID)", "Case ID", "Client Name", "WC ID", "Customer Type", "KYB Investigator", "KYB Creation Date",
                       "Fraud Typology", "Additional Links (Skopenow)", "L1 Outcome", "Pending Reason", "PIC", "Investigation File"]
            rows = [{"case_id": c["id"], "sort": c.get("escalated_at") or c["created_at"], "file": bool(c.get("investigation_file")), "cells": [
                        c["client_id"], c["id"], c["client"]["legal_name"], _wc_id(c, wc_by_kyc),
                        "Business (KYB)" if c.get("agency") else "Sole trader", _l1(c), c["created_at"],
                        c.get("fraud_typology") or "-", "Skopenow report" if c.get("skopenow") else "-", _l1_outcome(c),
                        c.get("pending_reason") or "-", _pic(c), (c.get("investigation_file") or {}).get("name", "-")]}
                    for c in scoped if c.get("escalated_at")]
        else:
            title, sub = f"{tenant_label} — EDD Tracker", "SOP 4.1.3 §6.1: every escalation, its investigator, screening date and investigation file"
            columns = ["Client ID (UID)", "Case ID", "Client Name", "WC ID", "L1 Investigator", "Screening Date", "L1 Outcome",
                       "Investigation File", "PIC", "Status", "Pending Reason"]
            rows = [{"case_id": c["id"], "sort": c.get("escalated_at") or c["created_at"], "file": bool(c.get("investigation_file")), "cells": [
                        c["client_id"], c["id"], c["client"]["legal_name"], _wc_id(c, wc_by_kyc), _l1(c),
                        c["verification_points"][3]["at"], _l1_outcome(c),
                        (c.get("investigation_file") or {}).get("name", "-"), _pic(c), c["status"], c.get("pending_reason") or "-"]}
                    for c in scoped if c.get("escalated_at")]
        rows.sort(key=lambda r: r["sort"], reverse=True)
        views = [(k, ("Business Escalation Tracker" if (k == "edd" and ttshop) else label)) for k, label in VIEWS]
        return {"view": view, "views": views, "title": title, "subtitle": sub, "columns": columns, "rows": rows,
                "file_col": columns.index("Investigation File") if "Investigation File" in columns else -1}


def to_csv(columns, rows):
    """UTF-8 with BOM and CRLF so Excel on Windows opens it correctly."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow(r["cells"] if isinstance(r, dict) else r)
    return ("﻿" + buf.getvalue()).encode("utf-8")
