"""QC sampling (SOP 2.1: QA / QC / Team Lead performs periodic random
sampling of closed L1 and escalation cases, keeps scorecards, coaches).

The day's sample is deterministic: the pool is the tenant's closed cases
that have not been reviewed, plus the ones reviewed *today*, sorted by id and
drawn with a generator seeded by the date — so the sample stays the same all
day as the reviewer works through it, and the same on every machine.
"""
import math
import random
from collections import Counter

from fincrime.data import vocab
from fincrime.utils import dates
from fincrime.utils.text import display_name


def _today():
    return dates.now_str()[:10]


def pool(cases, tenant, day=None):
    day = day or _today()
    return sorted((c for c in cases if c["tenant"] == tenant and c["status"] in vocab.CLOSED_STATUSES
                   and c.get("closed_by") != vocab.SYSTEM_USER          # an auto-verified case has no closer to coach
                   and (not c.get("qc_at") or c["qc_at"][:10] == day)), key=lambda c: c["id"])


def sample(cases, tenant, day=None):
    day = day or _today()
    candidates = pool(cases, tenant, day)
    if not candidates:
        return []
    size = min(len(candidates), max(vocab.QC_SAMPLE_MIN, math.ceil(vocab.QC_SAMPLE_RATE * len(candidates))))
    rng = random.Random(f"qc-{tenant}-{day}")
    picked = rng.sample(candidates, size)
    # Stable order all day: by close time, whether or not a row is reviewed yet.
    picked.sort(key=lambda c: (c.get("closed_at", ""), c["id"]))
    return picked


def pending(cases, tenant, day=None):
    return [c for c in sample(cases, tenant, day) if not c.get("qc_result")]


def scorecard(cases, tenant):
    """Per closing analyst: how many of their closures were sampled, passed,
    failed, and the reasons the failures cited."""
    rows = {}
    for c in cases:
        if c["tenant"] != tenant or not c.get("qc_result"):
            continue
        who = c.get("closed_by") or c.get("previous_reviewer") or "system"
        r = rows.setdefault(who, {"email": who, "name": display_name(who) if "@" in who else who,
                                  "sampled": 0, "passed": 0, "failed": 0, "reasons": Counter()})
        r["sampled"] += 1
        if c["qc_result"] == "Pass":
            r["passed"] += 1
        else:
            r["failed"] += 1
            r["reasons"][c.get("qc_reason", "")] += 1
    out = []
    for r in rows.values():
        r["pass_rate"] = round(100 * r["passed"] / r["sampled"]) if r["sampled"] else 0
        r["top_reasons"] = [k for k, _ in r["reasons"].most_common(2) if k]
        out.append(r)
    out.sort(key=lambda r: (-r["sampled"], r["name"]))
    return out


def feedback_for(cases, email):
    """Failed QC reviews the closing analyst has not acknowledged yet."""
    return [c for c in cases if c.get("qc_result") == "Fail" and not c.get("qc_ack") and c.get("closed_by") == email]
