"""The investigation file (SOP 4.1.3 §6.2, 4.2.2 §6.2).

The SOP has the analyst create a share-drive folder named after the user
id, drop the World-Check export and the Skopenow report into it, and paste
the link into a tracker. Here the folder is a property of the case: it is
stamped when the case escalates (workflow.escalate) and its contents are
read live from the case, the screening case and the watchlist case — so the
link can never go stale and nothing is copied by hand. The ZIP download is
the same folder for anyone who still needs a file.
"""
import csv
import io
import zipfile

from fincrime.documents import uploads
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates
from fincrime.utils.text import display_name


def items(case, alert=None, wc=None):
    """What the folder holds right now, newest last. Each item has a `kind`
    the template turns into a link: export | skopenow | attachment |
    document | summary | audit."""
    out = []
    for e in (wc or {}).get("exports", []):
        out.append({"kind": "export", "label": e["filename"], "at": e["at"], "by": e["by"], "ref": (wc or {}).get("id", "")})
    sk = case.get("skopenow") or {}
    if sk:
        out.append({"kind": "skopenow", "label": f"Skopenow report — {sk.get('status', '')}",
                    "at": sk.get("reviewed_at") or sk.get("requested_at", ""), "by": sk.get("reviewed_by") or sk.get("requested_by", ""), "ref": case["id"]})
    for a in case.get("attachments", []):
        out.append({"kind": "attachment", "label": a["name"], "at": a["at"], "by": a["by"], "ref": a.get("ref", "")})
    for d in case.get("documents", []):
        out.append({"kind": "document", "label": f"{d['name']} — {d['side']}", "at": case["created_at"], "by": "onboarding flow", "ref": d["id"]})
    summary = next((r for r in case.get("review_decisions", []) if r["check_point"] == "Data Screening" and r.get("id_summary")), None)
    if summary:
        out.append({"kind": "summary", "label": "L1 summary", "at": summary.get("operated_at", ""), "by": summary.get("by", ""), "ref": summary["id_summary"]})
    out.append({"kind": "audit", "label": f"Audit trail ({len(case.get('audit', []))} events)", "at": dates.now_str(), "by": "system", "ref": case["id"]})
    return out


def _summary_text(case, alert, wc):
    c = case["client"]
    lines = [
        f"Investigation file: {(case.get('investigation_file') or {}).get('name', case['id'])}",
        f"Opened: {(case.get('investigation_file') or {}).get('created_at', '')} by {display_name((case.get('investigation_file') or {}).get('created_by', ''))}",
        "",
        f"KYC case:        {case['id']}",
        f"Client ID (UID): {case['client_id']}",
        f"Client name:     {c['legal_name']}",
        f"Date of birth:   {c.get('dob') or '-'}",
        f"Nationality:     {c.get('nationality')}",
        f"Tenant:          {case['tenant']}",
        f"Status:          {case['status']} {('(' + case['pending_reason'] + ')') if case.get('pending_reason') else ''}",
        f"Escalation:      {case.get('escalate_reason') or '-'}",
        f"Fraud typology:  {case.get('fraud_typology') or '-'}",
        f"L1 investigator: {display_name((case.get('reviewers_by_level') or {}).get('L1', [''])[-1] if (case.get('reviewers_by_level') or {}).get('L1') else '')}",
        f"Screening date:  {case['verification_points'][3]['at']}",
        f"WC case ID:      {(wc or {}).get('id', '-')}",
        f"Screening outcome: {((alert or {}).get('case_outcome') or {}).get('value', '-')}",
        "",
        "Review decisions:",
    ]
    for r in case.get("review_decisions", []):
        if r.get("decision"):
            lines.append(f"  - {r['check_point']}: {r['decision']} — {r.get('approve_reason') or r.get('rejection_reason') or r.get('id_summary') or ''}".rstrip(" —"))
    return "\n".join(lines) + "\n"


def _audit_csv(case):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["at", "actor", "action", "detail"])
    for e in case.get("audit", []):
        w.writerow([e["at"], e["actor"], e["action"], e.get("detail", "")])
    return buf.getvalue()


def zip_bytes(case, alert, wc, *, render_pdf, render_doc):
    """(filename, bytes). `render_pdf(wc, alert, case)` and
    `render_doc(case, doc)` are supplied by the app so this module needs no
    reportlab or SVG imports of its own."""
    folder = (case.get("investigation_file") or {}).get("name") or f"{case['client_id']} ({case['id']})"
    safe = "".join(ch if ch.isalnum() or ch in " ()-_" else "_" for ch in folder)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{safe}/README.txt", _summary_text(case, alert, wc))
        z.writestr(f"{safe}/audit_trail.csv", "﻿" + _audit_csv(case))
        for e in (wc or {}).get("exports", []):
            z.writestr(f"{safe}/{e['filename']}", render_pdf(wc, alert, case))
        sk = case.get("skopenow") or {}
        if sk.get("report"):
            text = [f"Skopenow report (simulated) — {sk.get('status')}", f"Generated {sk['report'].get('generated_at', '')}", ""]
            for s in sk["report"].get("sections", []):
                if isinstance(s, dict):
                    text.append(f"{s['title']} [{s.get('match', '')}]: {s['text']}")
                else:
                    text.append(f"{s[0]}: {s[1]}")
            if sk.get("notes"):
                text += ["", f"Analyst review ({sk.get('reviewed_by', '')}): {sk['notes']}"]
            z.writestr(f"{safe}/Skopenow_report.txt", "\n".join(text) + "\n")
        for d in case.get("documents", []):
            stem = f"{safe}/documents/{d['id']}_{d['kind']}_{d['side'].replace(' ', '')}"
            if d.get("upload"):             # the customer's own photo or scan, as uploaded (re-saved as JPEG)
                try:
                    z.writestr(stem + ".jpg", uploads.load(d["upload"]["path"]))
                except (OSError, WorkflowError):
                    z.writestr(stem + "_missing.txt", "The uploaded image is no longer on this machine.\n")
            else:
                z.writestr(stem + ".svg", render_doc(case, d))
        if case.get("attachments"):
            z.writestr(f"{safe}/attachments.txt", "\n".join(f"{a['at']}  {a['by']}  {a['name']}" for a in case["attachments"]) + "\n")
    return f"{safe}.zip", buf.getvalue()
