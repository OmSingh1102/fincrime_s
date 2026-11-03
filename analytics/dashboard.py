"""View-model for the Analytics page: the metrics the earlier prototype
promised (backlog, SLA pressure, escalations, throughput per reviewer,
screening outcomes) plus the SOP 5.2.4 progress tracking — tier pipeline,
backlog ageing, shift progress per analyst, hand-off lag, QC quality —
computed from the live case book."""
from collections import Counter

from fincrime.analytics.pipeline import COLUMNS, column_of, lag_summary
from fincrime.data import vocab
from fincrime.engines import ai_screening, qc, routing, sla, worklog
from fincrime.utils import dates
from fincrime.utils.text import display_name

AGE_BUCKETS = [("< 12 h", 0, 12), ("12–24 h", 12, 24), ("24–48 h", 24, 48), ("> 48 h", 48, 10 ** 9)]


class AnalyticsConsole:

    @staticmethod
    def compute(kyc_cases, alert_cases, wc_cases, tenant, now=None, analysts=()):
        now = now or dates.now()
        today = dates.fmt(now)[:10]
        cases = [c for c in kyc_cases if c["tenant"] == tenant]
        open_cases = [c for c in cases if c["status"] in vocab.OPEN_STATUSES]
        closed = [c for c in cases if c["status"] in vocab.CLOSED_STATUSES]
        waiting = [c for c in cases if c["status"].startswith("Resubmit")]
        breached = [c for c in open_cases if sla.breached(c, now)]
        at_risk = [c for c in open_cases if not sla.breached(c, now) and sla.remaining_hours(c, now) <= 12]

        status_counts = [(s, sum(1 for c in cases if c["status"] == s)) for s in vocab.STATUSES]
        status_counts = [(s, n) for s, n in status_counts if n]

        # Throughput: closures per reviewer.
        per_reviewer = Counter()
        for c in closed:
            closer = c.get("closed_by") or ""
            if closer == vocab.SYSTEM_USER:           # auto-verified: credit whoever confirmed the data
                closer = c.get("previous_reviewer") or closer
            per_reviewer[closer or c["previous_reviewer"] or "system"] += 1
        throughput = [(display_name(e) if "@" in e else e, n) for e, n in per_reviewer.most_common(8)]

        # Turnaround for closed cases (hours from submission to close).
        tats = []
        for c in closed:
            a, b = dates.parse(c["submitted_at"]), dates.parse(c["closed_at"])
            if a and b:
                tats.append((b - a).total_seconds() / 3600)
        tat_avg = round(sum(tats) / len(tats), 1) if tats else 0.0

        # Screening: alert outcomes across the tenant's screening cases.
        tenant_ids = {c["id"] for c in cases}
        alerts = [a for ac in alert_cases if ac["kyc_case_id"] in tenant_ids for a in ac["alerts"]]
        outcome_counts = [(o, sum(1 for a in alerts if a["outcome"] == o)) for o in vocab.ALERT_OUTCOMES]
        auto = sum(1 for a in alerts if a.get("resolved_by") == vocab.SYSTEM_USER)
        manual = sum(1 for a in alerts if a["outcome"] != "Pending" and a.get("resolved_by") != vocab.SYSTEM_USER)
        # The workflows (auto-verified = closed, waiting on the customer,
        # human-in-the-loop) and what the rules fired on.
        shown = {c["id"]: ai_screening.shown_workflow(c) for c in cases}
        asked = [c for c in cases if shown[c["id"]] == ai_screening.WAITING]
        hit = [c for c in cases if c["screening"]["hit"]]
        auto_hit = [c for c in hit if shown[c["id"]] == "auto-verified"]
        human = [c for c in hit if shown[c["id"]] == "human-in-the-loop"]
        no_hit = sum(1 for c in cases if not c["screening"]["hit"] and shown[c["id"]] != ai_screening.WAITING)
        rules_fired = Counter(a.get("smart_strategy") or "rule" for a in alerts if a.get("resolved_by") == vocab.SYSTEM_USER).most_common()
        rationale_counts = Counter(a["rationale"] for a in alerts if a["rationale"]).most_common(6)

        reasons = Counter(r for c in open_cases for r in c["manual_review_reason"]).most_common(6)
        escalations = [c for c in cases if c["status"] == "DataEscalatingL2"]

        # ── SOP 5.2.4 additions ──
        tier = Counter(column_of(c, now) for c in cases)
        tier_breached = Counter(column_of(c, now) for c in breached)
        pipeline = [(label, tier.get(key, 0), tier_breached.get(key, 0)) for key, label, _t, _s in COLUMNS if key != "closed"]

        ageing = []
        for label, lo, hi in AGE_BUCKETS:
            n = 0
            for c in open_cases:
                created = dates.parse(c["created_at"])
                age = (now - created).total_seconds() / 3600 if created else 0
                if lo <= age < hi:
                    n += 1
            ageing.append((label, n))

        shift = []
        for a in analysts:
            if not a.get("capacity") and "QC" in a.get("roles", []):
                continue
            email = a["email"]
            held = [c for c in kyc_cases if c.get("current_reviewer") == email and c["status"] not in vocab.CLOSED_STATUSES]
            handled = [worklog.hours(w, now) for c in kyc_cases for w in c.get("work", []) if w["analyst"] == email and w.get("to")]
            shift.append({
                "name": a["name"], "roles": "/".join(a.get("roles", [])), "shift": a.get("shift", ""),
                "wip": len(held), "capacity": int(a.get("capacity") or 0),
                "pct": int(100 * len(held) / a["capacity"]) if a.get("capacity") else 0,
                "routed_today": routing.routed_today(email, kyc_cases, today),
                "completed_today": sum(1 for c in kyc_cases if (c.get("closed_at") or "")[:10] == today
                                       and (c.get("closed_by") == email
                                            or (c.get("closed_by") == vocab.SYSTEM_USER and c.get("previous_reviewer") == email))),
                "at_risk": sum(1 for c in held if sla.remaining_hours(c, now) <= 12),
                "avg_handle_h": round(sum(handled) / len(handled), 1) if handled else 0.0,
            })

        quality = qc.scorecard(kyc_cases, tenant)
        return {
            "kpis": [
                ("Open backlog", len(open_cases), "Data Confirming, Screening and L2", "info"),
                ("SLA breached", len(breached), "open cases past the 48 h / 14 d clock", "danger" if breached else "success"),
                ("At risk", len(at_risk), "due within 12 hours", "warning" if at_risk else "success"),
                ("Escalated", len(escalations), "waiting at L2 / EDD", ""),
                ("Waiting on user", len(waiting), "resubmission requested", ""),
                ("Closed", len(closed), f"avg turnaround {tat_avg} h", "success"),
                ("Auto-verified", len(auto_hit), f"of {len(hit)} cases with a hit; {no_hit} more had no match", "success"),
            ],
            "workflow_split": [("Auto-verified (hit)", len(auto_hit)), ("Waiting on customer", len(asked)),
                               ("Human-in-the-loop", len(human)), ("No match", no_hit)],
            "rules_fired": rules_fired,
            "status_counts": status_counts,
            "throughput": throughput,
            "outcome_counts": outcome_counts,
            "auto_vs_manual": [("Auto-resolved (rules)", auto), ("Manually reviewed", manual),
                               ("Pending", sum(1 for a in alerts if a["outcome"] == "Pending"))],
            "rationales": rationale_counts,
            "reasons": reasons,
            "breached": [{"case": c, "sla": sla.chip(c, now)} for c in sorted(breached + at_risk, key=lambda c: sla.remaining_hours(c, now))][:8],
            "total_alerts": len(alerts),
            "pipeline": pipeline,
            "ageing": ageing,
            "shift": shift,
            "handoff_lag": lag_summary(cases),
            "quality": quality,
            "qc_pending": len(qc.pending(kyc_cases, tenant)),
            "unassigned": sum(1 for c in open_cases if not c.get("current_reviewer") and routing.level_for(c)),
        }
