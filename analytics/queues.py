"""View-model for the L2 / EDD queue: escalated cases the signed-in analyst
is allowed to see, with the segregation rule applied and the hidden ones
counted so the page can say why. Businesses waiting for Checker 2 sit
in the same queue (SOP 4.2: the second reviewer on a business approval)."""
from fincrime.engines import segregation, sla


class QueueConsole:

    @staticmethod
    def compute(cases, tenant, analyst_email, levels):
        rows, hidden = [], 0
        for c in cases:
            if c["tenant"] != tenant:
                continue
            if c["status"] == "DataEscalatingL2":
                level = "EDD" if c["pending_reason"] == "Pending EDD" else "L2"
                if level not in levels and not ("QC" in levels):
                    continue
                if segregation.hidden_from(c, analyst_email, level):
                    hidden += 1
                    continue
            elif c["status"] == "DataScreening" and c.get("pending_reason") == "Pending Checker 2":
                level = "Checker 2"
                if not (levels & {"L1", "L2", "QC"}):
                    continue
                if (c.get("checker1") or {}).get("by") == analyst_email:
                    hidden += 1
                    continue
            else:
                continue
            rows.append({"case": c, "level": level, "sla": sla.chip(c),
                         "mine": c["current_reviewer"] == analyst_email,
                         "routed": (c.get("routing") or {}).get("status") == "routed"})
        rows.sort(key=lambda r: r["case"]["edd_due_at"] or r["case"]["sla_due_at"])
        return {"rows": rows, "hidden": hidden,
                "l2": sum(1 for r in rows if r["level"] == "L2"), "edd": sum(1 for r in rows if r["level"] == "EDD"),
                "c2": sum(1 for r in rows if r["level"] == "Checker 2")}
