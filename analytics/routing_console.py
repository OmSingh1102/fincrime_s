"""View-model for the Routing console: every analyst's skill book, load and
capacity, and the unassigned backlog with the router's pick for each case
and the reason — so a lead can see the allocation before running it.

The loads are counted once (routing.load_map) and the backlog shows the most
urgent BACKLOG_ROWS cases: a file-backed book can leave a thousand cases
unassigned, and the router only ever takes them most urgent first."""
from fincrime.data import vocab
from fincrime.engines import routing, sla
from fincrime.utils import dates
from fincrime.utils.text import display_name


BACKLOG_ROWS = 100


class RoutingConsole:

    @staticmethod
    def compute(cases, analysts, tenant, now=None):
        now = now or dates.now()
        today = dates.fmt(now)[:10]
        loads = routing.load_map(cases)
        people = []
        for a in analysts:
            cap = int(a.get("capacity") or 0)
            n = loads.get(a["email"], 0)
            skills = a.get("skills") or {}
            people.append({
                "analyst": a, "load": n, "capacity": cap, "pct": int(100 * n / cap) if cap else 0,
                "over": n > cap, "routed_today": routing.routed_today(a["email"], cases, today),
                "skills": list(skills.get("scripts") or []) + (["KYB"] if skills.get("kyb") else []),
                "tenants": [vocab.TENANTS[t]["label"] for t in a.get("tenants", [])],
            })
        backlog = []
        waiting = sorted(routing.unassigned(tenant, cases), key=lambda c: sla.remaining_hours(c, now))
        for c in waiting[:BACKLOG_ROWS]:
            best, reason, considered = routing.pick(c, cases, loads=loads)
            req = routing.requirements(c)
            backlog.append({
                "case": c, "sla": sla.chip(c, now), "req": req,
                "pick": best, "pick_name": display_name(best["email"]) if best else "", "reason": reason,
                "queued": (c.get("routing") or {}).get("status") == "queued",
                "candidates": [(display_name(a["email"]), ok, why) for a, ok, why in considered
                               if why not in ("case is not in a queue",) and not why.startswith("no ")],
            })
        routed_today = sum(p["routed_today"] for p in people)
        return {"people": people, "backlog": backlog,
                "queued": sum(1 for c in waiting if (c.get("routing") or {}).get("status") == "queued"),
                "unassigned": len(waiting), "more": max(0, len(waiting) - BACKLOG_ROWS), "routed_today": routed_today,
                "tenant_label": vocab.TENANTS[tenant]["label"]}
