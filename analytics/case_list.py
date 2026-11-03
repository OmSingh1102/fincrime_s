"""View-model for the case list: the tenant's rows, filtered, sorted and
paged on the server, plus every distinct value the filter grid offers, so the
template never computes.

Server-side because a tenant's book can hold a thousand cases (the
reconciliation-log book does): shipping every row to the browser and hiding
most of them made a multi-megabyte page that the live refresh re-downloaded
on every change. The filters keep the semantics the old client-side ones
had: text boxes match a substring (any case), selects match exactly, dates
are inclusive ranges, and a row with no date only survives an empty range.

With no filter in the grid the list opens on the open cases only: none that
is Verified or Rejected, so the front page is the work still to do. Sort,
page, page size and Priority Review keep that view; any filter searches the
whole book, so a verified case is still found by its ID or name."""
from fincrime.data import vocab
from fincrime.engines import ai_agent, ai_screening, sla
from fincrime.utils.text import display_name

# Rows a page: the real list's "N / Page" choices, 10 by default as it opens.
PAGE_SIZES = (10, 20, 30, 40, 50, 100)
PER_PAGE = 10

# Query parameters: the filter grid's control ids without their "f-" prefix.
TEXT_FILTERS = (("case_id", "id"), ("client_id", "client_id"), ("name", "name"), ("idnum", "id_number"),
                ("qcreason", "qc_reason"))
EXACT_FILTERS = (("reviewer", "current_reviewer"), ("prev", "previous_reviewer"), ("source", "source"),
                 ("vendor", "vendor"), ("gifter", "gifter"), ("nat", "nationality"), ("issuing", "issuing"),
                 ("poa", "poa"), ("level", "level"), ("pending", "pending_reason"), ("edd", "edd_rfi"),
                 ("qcrev", "qc_reviewer"), ("qcresult", "qc_result"), ("workflow", "workflow"), ("agent", "agent"),
                 ("verdict", "verdict"), ("watch", "watch"))
DATE_FILTERS = (("dob", "dob"), ("created", "created_at"), ("sub", "submitted_at"))


def _short(email):
    return display_name(email) if email else ""


def _row(c, analyst_email):
    client = c["client"]
    return {
        "case": c,
        "id": c["id"], "client_id": c["client_id"], "name": client["legal_name"], "dob": client["dob"],
        "status": c["status"], "pending_reason": c["pending_reason"], "tags": c["tags"],
        "previous_reviewer": _short(c["previous_reviewer"]), "current_reviewer": _short(c["current_reviewer"]),
        "vendor": (c["vendor_check"]["vendor"] or "").split("_")[0],
        "source": c["source"], "created_at": c["created_at"], "submitted_at": c["submitted_at"],
        "nationality": client["nationality"], "issuing": client["id_issuing_place"], "id_number": client["id_number"],
        "poa": "Yes" if c["poa_required"] else "No", "level": c["case_review_level"],
        "mrr": "; ".join(c["manual_review_reason"]), "qc_reviewer": _short(c["qc_reviewer"]),
        "qc_reason": c["qc_reason"], "qc_result": c["qc_result"],
        "gifter": next((t for t in c["tags"] if t.startswith("Tier")), ""),
        "edd_rfi": "Yes" if c["pending_reason"] in ("Pending EDD", "RFI Sent") else "No",
        "mine": bool(analyst_email) and c["current_reviewer"] == analyst_email,
        "workflow": ai_screening.shown_workflow(c),
        "agent": _agent(c),
        "verdict": _verdict(c),
        "watch": ((c.get("ai_agent") or {}).get("watchlist") or {}).get("finding", ""),
    }


def _agent(c):
    """The AI Agent's decision on the case, "not_run" for one it could still
    take, else empty."""
    if c.get("ai_agent"):
        return c["ai_agent"]["decision"]
    return "not_run" if ai_agent.eligible(c) else ""


def _verdict(c):
    """Does Claude's own verdict agree with the rules' decision? "" before one."""
    v = (c.get("ai_agent") or {}).get("verdict") or {}
    return "" if "agrees" not in v else ("agree" if v["agrees"] else "disagree")


def read_query(args):
    """The filter values from a request's query string (any mapping with
    get/getlist), cleaned, so the view and the template agree on them."""
    def one(key):
        return (args.get(key) or "").strip()
    q = {key: one(key) for key, _ in TEXT_FILTERS + EXACT_FILTERS}
    for key, _ in DATE_FILTERS:
        q[f"{key}_from"], q[f"{key}_to"] = one(f"{key}_from"), one(f"{key}_to")
    for key in ("tags", "mrr"):
        q[key] = one(key)
    q["status"] = [s for s in (args.getlist("status") if hasattr(args, "getlist") else []) if s in vocab.STATUSES]
    q["priority"] = one("priority") == "1"
    q["sort"] = "asc" if one("sort") == "asc" else "desc"
    try:
        q["page"] = max(1, int(one("page") or 1))
    except ValueError:
        q["page"] = 1
    return q


def _in_range(value, start, end):
    if not value:
        return not (start or end)
    day = value[:10]
    return not ((start and day < start) or (end and day > end))


def matches(row, q):
    for key, field in TEXT_FILTERS:
        if q[key] and q[key].lower() not in (row[field] or "").lower():
            return False
    for key, field in EXACT_FILTERS:
        if q[key] and row[field] != q[key]:
            return False
    for key, field in DATE_FILTERS:
        if not _in_range(row[field], q[f"{key}_from"], q[f"{key}_to"]):
            return False
    if q["status"] and row["status"] not in q["status"]:
        return False
    if q["tags"] and q["tags"] not in row["tags"]:
        return False
    if q["mrr"] and q["mrr"] not in row["mrr"]:
        return False
    if q["priority"] and "Priority Review" not in row["tags"]:
        return False
    return True


def active_filters(q):
    """How many filter controls hold a value (the fold summary's count)."""
    keys = [k for k, _ in TEXT_FILTERS + EXACT_FILTERS] + ["tags", "mrr"]
    keys += [f"{k}_{end}" for k, _ in DATE_FILTERS for end in ("from", "to")]
    return sum(1 for k in keys if q[k]) + len(q["status"])


class CaseListConsole:

    @staticmethod
    def compute(cases, tenant, analyst_email="", now=None, query=None, per_page=PER_PAGE):
        """`query` is read_query()'s dict; None means the unfiltered first page."""
        q = query or read_query({})
        rows = [_row(c, analyst_email) for c in cases if c["tenant"] == tenant]

        def distinct(key):
            return sorted({r[key] for r in rows if r[key]})

        reviewers = distinct("previous_reviewer")
        filters = {
            "statuses": vocab.STATUSES,
            "sources": vocab.SOURCES,
            "reviewers": reviewers + [x for x in distinct("current_reviewer") if x not in reviewers],
            "tags": vocab.TAGS,
            "issuing": distinct("issuing"),
            "pending": [p for p in vocab.PENDING_REASONS if p],
            "mrr": vocab.MANUAL_REVIEW_REASONS,
            "levels": vocab.CASE_REVIEW_LEVELS,
            "qc_results": [x for x in vocab.QC_RESULTS if x],
            "vendors": distinct("vendor"),
        }
        open_only = active_filters(q) == 0                 # the opening view: no closed case
        shown = [r for r in rows if matches(r, q) and not (open_only and r["status"] in vocab.CLOSED_STATUSES)]
        shown.sort(key=lambda r: r["created_at"], reverse=q["sort"] != "asc")
        pages = max(1, -(-len(shown) // per_page))
        page = min(q["page"], pages)
        rows_page = shown[(page - 1) * per_page: page * per_page]
        for r in rows_page:                       # the SLA chip is the costly field: page rows only
            r["sla"] = sla.chip(r["case"], now)
        return {"rows": rows_page, "total": len(rows), "shown": len(shown), "page": page, "pages": pages,
                "per_page": per_page, "first": (page - 1) * per_page + 1 if shown else 0,
                "last": (page - 1) * per_page + len(rows_page), "query": q, "active": active_filters(q), "open_only": open_only,
                "filters": filters}
