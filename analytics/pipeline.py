"""View-model for the Pipeline page (SOP 5.2.3: real-time visibility into
the whole case pipeline across tiers) and the hand-off lag figures the
dashboard shares with it."""
from fincrime.data import vocab
from fincrime.engines import sla
from fincrime.utils import dates
from fincrime.utils.text import display_name

COLUMNS = [
    ("intake", "Intake", "System", "Submitted · ID verification"),
    ("l1_confirm", "L1 · Data Confirming", "L1", "documents and fields"),
    ("l1_screen", "L1 · Data Screening", "L1", "watchlist hits"),
    ("waiting", "Waiting on user", "—", "resubmission requested"),
    ("l2", "L2 · Escalation", "L2", "escalated hits · Checker 2"),
    ("edd", "L3 · EDD / PIC", "EDD", "enhanced due diligence"),
    ("closed", "Closed (24 h)", "—", "verified · rejected"),
]
CARD_LIMIT = 8


def column_of(case, now=None):
    s, p = case["status"], case.get("pending_reason", "")
    if s in ("Submitted", "IDVerification"):
        return "intake"
    if s == "DataConfirming":
        return "l1_confirm"
    if s == "DataScreening":
        return "l2" if p == "Pending Checker 2" else "l1_screen"
    if s.startswith("Resubmit") or s == "POAResubmit":
        return "waiting"
    if s == "DataEscalatingL2":
        return "edd" if p == "Pending EDD" else "l2"
    if s in vocab.CLOSED_STATUSES:
        closed = dates.parse(case.get("closed_at") or "")
        if closed and ((now or dates.now()) - closed).total_seconds() <= 24 * 3600:
            return "closed"
    return ""


def lag_text(seconds):
    if seconds is None:
        return "waiting"
    if seconds < 60:
        return f"{int(seconds)} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3600:.1f} h"


def lag_summary(cases):
    """Average hand-off lag, hub routing vs the bot-era history in the seed."""
    hub = [h["lag_seconds"] for c in cases for h in c.get("handoffs", []) if h.get("via") == "hub" and h.get("lag_seconds") is not None]
    bot = [h["lag_seconds"] for c in cases for h in c.get("handoffs", []) if h.get("via") == "bot" and h.get("lag_seconds") is not None]
    waiting = [h for c in cases for h in c.get("handoffs", []) if not h.get("assigned_at")]
    return {
        "hub_n": len(hub), "hub_avg": (sum(hub) / len(hub)) if hub else None,
        "hub_text": lag_text(sum(hub) / len(hub)) if hub else "—",
        "bot_n": len(bot), "bot_avg": (sum(bot) / len(bot)) if bot else None,
        "bot_text": lag_text(sum(bot) / len(bot)) if bot else "—",
        "waiting": len(waiting),
    }


class PipelineConsole:

    @staticmethod
    def compute(cases, tenant, now=None):
        now = now or dates.now()
        scoped = [c for c in cases if c["tenant"] == tenant]
        cols = {key: {"key": key, "label": label, "tier": tier, "sub": sub, "cases": [], "n": 0,
                      "breached": 0, "at_risk": 0, "unassigned": 0}
                for key, label, tier, sub in COLUMNS}
        for c in scoped:
            key = column_of(c, now)
            if not key:
                continue
            col = cols[key]
            col["n"] += 1
            remaining = sla.remaining_hours(c, now)
            open_case = c["status"] in vocab.OPEN_STATUSES
            if open_case and remaining < 0:
                col["breached"] += 1
            elif open_case and remaining <= 12:
                col["at_risk"] += 1
            if open_case and not c.get("current_reviewer") and key not in ("intake", "waiting"):
                col["unassigned"] += 1
            created = dates.parse(c["created_at"])
            age_h = round((now - created).total_seconds() / 3600, 1) if created else 0
            col["cases"].append({"case": c, "sla": sla.chip(c, now), "age_h": age_h, "remaining": remaining,
                                 "reviewer": display_name(c["current_reviewer"]) if c.get("current_reviewer") else "",
                                 "routed": (c.get("routing") or {}).get("status") == "routed",
                                 "queued": (c.get("routing") or {}).get("status") == "queued"})
        for col in cols.values():
            if col["key"] == "closed":
                col["cases"].sort(key=lambda r: r["case"].get("closed_at", ""), reverse=True)
            else:
                col["cases"].sort(key=lambda r: r["remaining"])
            col["more"] = max(0, len(col["cases"]) - CARD_LIMIT)
            col["cases"] = col["cases"][:CARD_LIMIT]

        feed = []
        for c in scoped:
            for h in c.get("handoffs", []):
                feed.append({"case": c, "from": h["from"], "to": h["to"], "at": h["at"], "via": h.get("via", ""),
                             "assigned_to": display_name(h["assigned_to"]) if h.get("assigned_to") else "",
                             "assigned_at": h.get("assigned_at", ""), "lag": lag_text(h.get("lag_seconds"))})
        feed.sort(key=lambda f: f["at"], reverse=True)
        return {"columns": list(cols.values()), "feed": feed[:12], "lag": lag_summary(scoped),
                "total_open": sum(1 for c in scoped if c["status"] in vocab.OPEN_STATUSES)}
