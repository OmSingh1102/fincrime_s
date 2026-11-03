"""View-model for the AI Screening page: the rule book with how often each
rule fired, the guards and rails, the tenant's split between auto-verified
and human-in-the-loop, and a feed of what the rules did most recently.

On the reconciliation-log book it also sets the rules beside what the file's
analysts recorded for the same cases (case["recon"]): how many of the cases
the rules cleared were recorded as false positives, and what the cases they
left for an analyst turned out to be."""
from collections import Counter

from fincrime.data import vocab
from fincrime.engines import ai_review, ai_screening


OUTCOME_LABEL = {"FP": "false positive", "TP": "true match", "Escalated": "escalated to L2", "Pending": "pending customer documents"}
OUTCOME_TONE = (("FP", "emerald"), ("TP", "rose"), ("Escalated", "amber"), ("Pending", "blue"))


class AiScreeningConsole:

    @staticmethod
    def compute(cases, alert_cases, wc_cases, tenant, settings, agent=None):
        tenant_cases = [c for c in cases if c["tenant"] == tenant]
        ids = {c["id"] for c in tenant_cases}
        by_id = {c["id"]: c for c in tenant_cases}
        alerts = [a for a in alert_cases if a["kyc_case_id"] in ids]
        rows = [r for a in alerts for r in a["alerts"]]
        auto_rows = [r for r in rows if r.get("resolved_by") == vocab.SYSTEM_USER]
        fired = Counter(r.get("smart_strategy") for r in auto_rows)
        enabled, guards_on = settings.get("enabled", {}), settings.get("guards", {})
        rules = [dict(r, on=bool(enabled.get(r["key"], r["default"])), fired=fired.get(r["label"], 0)) for r in ai_screening.RULES]
        guards = [dict(g, on=bool(guards_on.get(g["key"], g["default"]))) for g in ai_screening.GUARDS]
        switches_on = settings.get("switches", {})
        model = dict(ai_review.status(), on=bool(switches_on.get("model_review", True)),
                     count=sum(1 for r in rows if r.get("ai_assessment")))

        shown = {c["id"]: ai_screening.shown_workflow(c) for c in tenant_cases}
        asked = [c for c in tenant_cases if shown[c["id"]] == ai_screening.WAITING]
        hit = [c for c in tenant_cases if c["screening"]["hit"]]
        auto_hit = [c for c in hit if shown[c["id"]] == "auto-verified"]
        human = [c for c in hit if shown[c["id"]] == "human-in-the-loop"]
        no_hit = [c for c in tenant_cases if not c["screening"]["hit"] and shown[c["id"]] != ai_screening.WAITING]
        closed_by_rules = [c for c in tenant_cases if c.get("closed_by") == vocab.SYSTEM_USER]
        pending = sum(1 for r in rows if r["outcome"] == "Pending")
        tiles = [
            ("Auto-verified", len(auto_hit), f"of {len(hit)} cases with a screening hit, closed", "success"),
            ("Waiting on customer", len(asked), "rules cleared the alerts; still open for a document", ""),
            ("Human-in-the-loop", len(human), f"{pending} alert(s) waiting for an analyst", "warning" if human else ""),
            ("No match", len(no_hit), "auto-verified with nothing to clear", ""),
            ("Records auto-resolved", len(auto_rows), f"of {len(rows)} watchlist records returned", "info"),
            ("Cases closed by the rules", len(closed_by_rules), "verified with no analyst step", "success"),
        ]

        feed = []
        for w in wc_cases:
            k = by_id.get(w["kyc_case_id"])
            if k is None:
                continue
            for e in w.get("audit", []):
                if e["action"] == ai_screening.ACTION:
                    feed.append({"at": e["at"], "kind": "World-Check case", "name": k["client"]["legal_name"], "detail": e["detail"],
                                 "endpoint": "wc_case", "args": {"wc_case_id": w["id"]}, "new_tab": True})
        for c in tenant_cases:
            for e in c.get("audit", []):
                if e["actor"] == vocab.SYSTEM_USER and e["action"] == "Case verified":
                    feed.append({"at": e["at"], "kind": "KYC case", "name": c["client"]["legal_name"], "detail": e["detail"],
                                 "endpoint": "kyc_detail", "args": {"case_id": c["id"]}, "new_tab": False})
        feed.sort(key=lambda r: r["at"], reverse=True)

        against = None
        recon = [c for c in tenant_cases if c.get("recon") and not c["recon"].get("uploaded")]
        if recon:
            auto = [c for c in recon if shown[c["id"]] == "auto-verified"]
            held = [c for c in recon if shown[c["id"]] != "auto-verified"]

            def split(rows):
                n = Counter(c["recon"]["outcome"] for c in rows)
                return [(OUTCOME_LABEL[k], n[k], tone) for k, tone in OUTCOME_TONE if n.get(k)]

            fp = sum(1 for c in auto if c["recon"]["outcome"] == "FP")
            against = {"cases": len(recon), "auto": len(auto), "auto_fp": fp,
                       "auto_pct": round(100 * fp / len(auto)) if auto else 0, "auto_split": split(auto),
                       "held": len(held), "held_split": split(held),
                       "tp_cleared": sum(1 for c in auto if c["recon"]["outcome"] == "TP")}

        return {
            "tenant_label": vocab.TENANTS[tenant]["label"],
            "rules": rules, "guards": guards, "rails": ai_screening.RAILS, "model": model,
            "tiles": tiles, "hit": len(hit), "feed": feed[:10],
            "history": list(reversed(settings.get("history", [])))[:10],
            "against": against,
            "agent": agent_card(tenant_cases, agent or {}),
        }


def agent_card(tenant_cases, runtime):
    """The AI Agent card: its queue, what it decided, why it handed cases to a
    person, its model calls, and its verified cases set against the file."""
    from fincrime.engines import ai_agent
    queue = [c for c in tenant_cases if ai_agent.eligible(c) and not c.get("ai_agent")]
    done = [c for c in tenant_cases if c.get("ai_agent")]
    decided = Counter(c["ai_agent"]["decision"] for c in done)
    why = Counter()
    for c in done:
        rec = c["ai_agent"]
        if rec["decision"] != "human":
            continue
        failed = [x["check"] for x in rec.get("checks", []) if not x["ok"]]
        for check in failed or (["Model call failed"] if rec.get("error") else ["Other"]):
            why[check] += 1
    secs = [c["ai_agent"]["seconds"] for c in done if c["ai_agent"].get("seconds") and not c["ai_agent"].get("error")]
    tokens = sum((c["ai_agent"].get("usage") or {}).get("input", 0) + (c["ai_agent"].get("usage") or {}).get("output", 0)
                 for c in done)
    verified = [c for c in done if c["ai_agent"]["decision"] == "verified"]
    file_split = Counter(c["recon"]["outcome"] for c in verified if c.get("recon") and not c["recon"].get("uploaded"))
    recent = sorted(done, key=lambda c: c["ai_agent"].get("at", ""), reverse=True)[:8]
    # Claude's own verdict beside the rules: how often the two agree, and where
    # not (a verdict given under an older policy counts as waiting, not judged)
    current = [c for c in done if not ai_agent.needs_verdict(c)]
    judged = [c["ai_agent"]["verdict"] | {"rules": ai_agent.rules_say(c["ai_agent"])} for c in current
              if (c["ai_agent"].get("verdict") or {}).get("decision")]
    agree = [v for v in judged if v["agrees"]]
    differ = [v for v in judged if not v["agrees"]]
    verdict = {"judged": len(judged), "agree": len(agree), "pct": round(100 * len(agree) / len(judged)) if judged else 0,
               "verify_vs_person": sum(1 for v in differ if v["rules"] == "verify"),
               "person_vs_verify": sum(1 for v in differ if v["rules"] == "send_to_person"),
               "conf_agree": round(sum(v["confidence"] for v in agree) / len(agree)) if agree else 0,
               "conf_differ": round(sum(v["confidence"] for v in differ) / len(differ)) if differ else 0,
               "waiting": sum(1 for c in done if ai_agent.needs_verdict(c)),
               "failed": sum(1 for c in current if (c["ai_agent"].get("verdict") or {}).get("error"))}
    return {"verdict": verdict, "running": bool(runtime.get("running")), "busy": len(runtime.get("busy") or ()),
            "started_by": runtime.get("started_by", ""), "started_at": runtime.get("started_at", ""),
            "stopped": runtime.get("stopped", ""), "workers": runtime.get("workers", 0), "model": ai_agent.MODEL,
            "queue": len(queue), "done": len(done), "verified": decided.get("verified", 0),
            "screening": decided.get("screening", 0), "human": decided.get("human", 0), "resubmit": decided.get("resubmit", 0), "rfi": decided.get("rfi", 0),
            "corrected": sum(1 for c in verified if c["ai_agent"].get("name_corrected") or c["ai_agent"].get("address_updated")),
            "why": why.most_common(), "calls": len(secs), "avg_seconds": round(sum(secs) / len(secs), 1) if secs else 0.0,
            "tokens": tokens, "file_fp": file_split.get("FP", 0), "file_tp": file_split.get("TP", 0),
            "file_other": sum(n for k, n in file_split.items() if k not in ("FP", "TP")), "has_file": bool(file_split) or any(c.get("recon") for c in tenant_cases),
            "recent": [{"id": c["id"], "name": c["client"]["legal_name"], "decision": c["ai_agent"]["decision"],
                        "reason": "; ".join(c["ai_agent"].get("reasons", [])[:2]), "at": c["ai_agent"].get("at", "")} for c in recent]}
