"""View-model for My Work: what the signed-in analyst holds, how much room
they have, and the feedback waiting for them."""
from fincrime.data import vocab
from fincrime.engines import qc, routing, sla, worklog
from fincrime.utils import dates


def _closer(case):
    """Who gets the credit: the closer, or the analyst who confirmed the data
    on a case the AI screening rules then verified."""
    closed_by = case.get("closed_by") or ""
    return case.get("previous_reviewer") or closed_by if closed_by == vocab.SYSTEM_USER else closed_by


class WorkConsole:

    @staticmethod
    def compute(cases, alert_cases, analyst, now=None):
        email = analyst["email"]
        now = now or dates.now()
        today = dates.fmt(now)[:10]
        mine = [c for c in cases if c.get("current_reviewer") == email and c["status"] not in vocab.CLOSED_STATUSES]
        rows = [{"case": c, "sla": sla.chip(c, now), "level": routing.level_for(c) or c.get("case_review_level", ""),
                 "tenant_label": vocab.TENANTS[c["tenant"]]["label"], "routing": c.get("routing") or {},
                 "remaining": sla.remaining_hours(c, now)} for c in mine]
        rows.sort(key=lambda r: r["remaining"])
        capacity = int(analyst.get("capacity") or 0)
        wip = len(mine)
        headroom = max(0, capacity - wip)
        due_soon = sum(1 for r in rows if r["remaining"] <= 4)
        completed_today = sum(1 for c in cases if _closer(c) == email and (c.get("closed_at") or "")[:10] == today)
        handled = [worklog.hours(w, now) for c in cases for w in c.get("work", [])
                   if w["analyst"] == email and w.get("to")]
        avg_handle = round(sum(handled) / len(handled), 1) if handled else 0.0
        screening = []
        kyc_by_id = {c["id"]: c for c in cases}
        for a in alert_cases:
            if a.get("assigned_to") == email and a.get("status") != "Closed":
                k = kyc_by_id.get(a["kyc_case_id"])
                screening.append({"alert": a, "kyc": k, "pending": sum(1 for x in a["alerts"] if x["outcome"] == "Pending"),
                                  "sla": sla.chip(k, now) if k else None})
        loads = routing.load_map(cases)
        eligible = [c for c in routing.unassigned(None, cases) if routing.eligible(analyst, c, cases, loads=loads)[0]]
        return {
            "analyst": analyst, "wip": wip, "capacity": capacity, "headroom": headroom,
            "pct": int(100 * wip / capacity) if capacity else 0,
            "due_soon": due_soon, "completed_today": completed_today, "avg_handle_h": avg_handle,
            "routed_today": routing.routed_today(email, cases, today),
            "rows": rows, "screening_rows": screening,
            "qc_feedback": qc.feedback_for(cases, email),
            "backlog_eligible": len(eligible),
            "default_n": min(3, headroom) if headroom else 1,
            "tenant_labels": " and ".join(vocab.TENANTS[t]["label"] for t in analyst.get("tenants", [])),
        }
