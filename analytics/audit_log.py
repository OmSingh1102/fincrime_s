"""View-model for the global Audit Log (SOP 5.2.4: audit logs within a
single platform). One stream across KYC cases, screening cases and
watchlist cases, newest first, with the record each event belongs to."""
from fincrime.utils.text import display_name

KINDS = {"kyc": "KYC case", "alert": "Screening case", "wc": "World-Check case"}


class AuditLogConsole:

    @staticmethod
    def compute(kyc, alerts, wc, tenant, limit=500):
        kyc_by_id = {c["id"]: c for c in kyc}
        rows = []
        for c in kyc:
            if c["tenant"] != tenant:
                continue
            for e in c.get("audit", []):
                rows.append({"at": e["at"], "actor": e["actor"], "kind": "kyc", "kind_label": KINDS["kyc"],
                             "record_id": c["id"], "label": c["client"]["legal_name"], "action": e["action"],
                             "detail": e.get("detail", ""), "endpoint": "kyc_detail", "args": {"case_id": c["id"]}})
        for a in alerts:
            k = kyc_by_id.get(a["kyc_case_id"])
            if not k or k["tenant"] != tenant:
                continue
            for e in a.get("audit", []):
                rows.append({"at": e["at"], "actor": e["actor"], "kind": "alert", "kind_label": KINDS["alert"],
                             "record_id": a["id"], "label": a["screening_info"]["name"], "action": e["action"],
                             "detail": e.get("detail", ""), "endpoint": "ns_case", "args": {"alert_case_id": a["id"]}})
        for w in wc:
            k = kyc_by_id.get(w["kyc_case_id"])
            if not k or k["tenant"] != tenant:
                continue
            for e in w.get("audit", []):
                rows.append({"at": e["at"], "actor": e["actor"], "kind": "wc", "kind_label": KINDS["wc"],
                             "record_id": w["id"], "label": w["primary_name"], "action": e["action"],
                             "detail": e.get("detail", ""), "endpoint": "wc_case", "args": {"wc_case_id": w["id"]}})
        rows.sort(key=lambda r: r["at"], reverse=True)
        total = len(rows)
        rows = rows[:limit]
        for r in rows:
            r["actor_name"] = display_name(r["actor"]) if "@" in r["actor"] else r["actor"]
        return {
            "rows": rows, "total": total, "shown": len(rows),
            "filters": {
                "actors": sorted({r["actor_name"] for r in rows}),
                "actions": sorted({r["action"] for r in rows}),
                "kinds": list(KINDS.items()),
            },
        }
