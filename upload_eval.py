"""The Model evaluation page, measured on the IDs uploaded in the console as
they are uploaded and decided: no sample data, no separate run.

An upload has no answer key, so the two models are measured on what the
console itself knows about each one:

  reading   per reader model: the uploads it read, the photos it called
            spoiled, whether the check digits of its MRZ transcription add
            up and its printed fields agree with the zone (code checks them,
            documents/id_scan.checks), the reads with a field it was unsure
            of, seconds and cost a read;
  deciding  per decision model: the hits it decided, and on the hits an
            analyst then gave an outcome, whether the analyst agreed with
            its suggestion (the analyst's outcome is the answer), the hits it
            suggested clearing that the analyst did not clear, a confusion
            matrix (the analyst's outcome against the AI's suggestion),
            seconds and cost a hit; and the agreement under each SOP version.

Built from the "scan" records each time the page is drawn.
"""
import statistics

from fincrime import id_screening

OUTCOMES = id_screening.RESOLUTIONS[::-1]          # False positive, Possible match, True match


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def _avg(xs, digits):
    return round(statistics.mean(xs), digits) if xs else None


def _check(scan, prefix):
    return next((c["state"] for c in scan.get("checks") or [] if c["label"].startswith(prefix)), None)


def reading(scans):
    """One row per model that read an upload, most uploads first."""
    by = {}
    for s in scans:
        m = s.get("model")
        if not m:
            continue
        r = by.setdefault(m["model"], {"id": m["model"], "label": m["label"], "reads": 0, "flagged": 0, "unsure": 0,
                                       "not_id": 0, "mrz": [], "mrz_fields": [], "seconds": [], "costs": []})
        read = s.get("read") or {}
        r["reads"] += 1
        r["flagged"] += (not read.get("readable", True)) or bool(read.get("problems"))
        r["unsure"] += bool(read.get("uncertain"))
        r["not_id"] += read.get("document_type") == "Not an identity document"
        for key, prefix in (("mrz", "MRZ check digits"), ("mrz_fields", "Printed fields against the MRZ")):
            state = _check(s, prefix)
            if state:
                r[key].append(state == "ok")
        r["seconds"].append(m.get("seconds") or 0)
        r["costs"].append(m.get("cost") or 0)
    out = []
    for r in by.values():
        out.append({"id": r["id"], "label": r["label"], "reads": r["reads"], "flagged": r["flagged"], "unsure": r["unsure"],
                    "not_id": r["not_id"], "mrz_ok": [sum(r["mrz"]), len(r["mrz"])],
                    "mrz_fields_ok": [sum(r["mrz_fields"]), len(r["mrz_fields"])],
                    "seconds": _avg(r["seconds"], 1), "cost": _avg(r["costs"], 4), "total": round(sum(r["costs"]), 4)})
    return sorted(out, key=lambda r: (-r["reads"], r["label"]))


def _hits(scans):
    for s in scans:
        for h in (s.get("screening") or {}).get("hits") or []:
            yield s, h


def deciding(scans):
    """One row per model that decided a hit, most hits first, each with its
    confusion matrix on the hits an analyst gave an outcome."""
    by = {}
    for s, h in _hits(scans):
        ai, res = h.get("ai") or {}, h.get("resolution")
        if ai.get("status") not in ("done", "failed"):
            continue
        r = by.setdefault(ai["model"], {"id": ai["model"], "label": ai.get("model_label", ai["model"]), "decided": 0, "failed": 0,
                                        "confirmed": 0, "agreed": 0, "cleared_wrongly": 0, "auto": 0, "seconds": [], "costs": [],
                                        "cells": {t: {c: 0 for c in OUTCOMES} for t in OUTCOMES}})
        if ai["status"] == "failed":
            r["failed"] += 1
            continue
        r["decided"] += 1
        r["seconds"].append(ai.get("seconds") or 0)
        r["costs"].append(ai.get("cost") or 0)
        said = (res or {}).get("ai_suggested")
        if res and res.get("auto"):                 # cleared by the rule: no analyst's answer to score it on
            r["auto"] += 1
        elif res and said in OUTCOMES:
            r["confirmed"] += 1
            r["agreed"] += bool(res.get("agreed"))
            r["cleared_wrongly"] += said == "False positive" and res["status"] != "False positive"
            r["cells"][res["status"]][said] += 1
    out = []
    for r in by.values():
        out.append({"id": r["id"], "label": r["label"], "decided": r["decided"], "failed": r["failed"], "confirmed": r["confirmed"],
                    "agreed": r["agreed"], "agreement": _pct(r["agreed"], r["confirmed"]), "cleared_wrongly": r["cleared_wrongly"], "auto": r["auto"],
                    "seconds": _avg(r["seconds"], 1), "cost": _avg(r["costs"], 4), "total": round(sum(r["costs"]), 4),
                    "matrix": {"rows": list(OUTCOMES), "cols": list(OUTCOMES), "cells": r["cells"]}})
    return sorted(out, key=lambda r: (-r["decided"], r["label"]))


def by_sop(scans):
    """The analysts' agreement with the AI under each SOP version it decided by."""
    by = {}
    for _s, h in _hits(scans):
        ai, res = h.get("ai") or {}, h.get("resolution")
        if ai.get("status") == "done" and res and res.get("ai_suggested") and not res.get("auto"):
            v = by.setdefault(ai["sop"], {"version": ai["sop"], "confirmed": 0, "agreed": 0})
            v["confirmed"] += 1
            v["agreed"] += bool(res.get("agreed"))
    return [dict(v, agreement=_pct(v["agreed"], v["confirmed"])) for v in sorted(by.values(), key=lambda v: (len(v["version"]), v["version"]))]


def totals(scans):
    """The counts the pages' headlines use."""
    hits = list(_hits(scans))
    confirmed = [h for _s, h in hits if (h.get("resolution") or {}).get("ai_suggested") and not h["resolution"].get("auto")]
    return {"uploads": len(scans), "read": sum(1 for s in scans if s.get("read")), "hits": len(hits),
            "decided": sum(1 for _s, h in hits if (h.get("ai") or {}).get("status") == "done"),
            "confirmed": len(confirmed), "agreed": sum(1 for h in confirmed if h["resolution"].get("agreed")),
            "auto_closed": sum(1 for s in scans if (s.get("closed") or {}).get("auto")),
            "auto_cleared": sum(1 for _s, h in hits if (h.get("resolution") or {}).get("auto"))}
