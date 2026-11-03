"""Fresh copies of the seed for boot and for reset.

SEED_VERSION is stamped in the state store at boot, together with the case
book. Bump it whenever a builder's output shape changes — a stored record
from an older shape would be missing keys the templates read, and clearing
the store is the honest answer.

Three case books (CONSOLE_CASES in the environment picks one; app.py reads it):
"screening", the default, starts with no case: each ID uploaded on Scan ID
becomes one, screened against the World-Check records of the reconciliation
log (fincrime/watchlist.py) or World-Check One itself once a key is entered;
"file" is the 1,000 cases of the reconciliation log
(fincrime/data/recon_book.py); "demo" is the nineteen scripted scenarios and
the filler rows the test suite and the original walkthrough use.
"""
from fincrime.data import vocab
from fincrime.data.seed import assemble

SEED_VERSION = "2026-10-11.1"
BOOKS = ("screening", "file", "demo")
DEFAULT_BOOK = "screening"


def book_from(value):
    """The case book a setting names; anything unknown is the default."""
    value = (value or "").strip().lower()
    return value if value in BOOKS else DEFAULT_BOOK


def load_seed(book=DEFAULT_BOOK):
    """(analysts, kyc_cases, alert_cases, wc_cases) as brand-new objects."""
    return assemble(book_from(book))


def index_by_id(records):
    return {r["id"]: r for r in records}


def relabel(*record_lists):
    """Records stored before the console went client-neutral carry the old
    display names (tags, sources, business lines, watchlist groups, the
    tenant in an analyst's role label). Map them in place on load, so stored
    work survives without a reseed. Returns how many values changed."""
    changed = 0

    def fix(value, key=""):
        nonlocal changed
        if isinstance(value, str):
            # "Agency" is a common word: only the tag of that name is renamed
            new = vocab.RELABEL.get(value) if (value != "Agency" or key == "tags") else None
            if new is None and ("TT Live" in value or "TT Shop" in value):
                new = value.replace("TT Live", vocab.TENANTS["ttlive"]["label"]).replace("TT Shop", vocab.TENANTS["ttshop"]["label"])
            if new is not None and new != value:
                changed += 1
                return new
            return value
        if isinstance(value, list):
            return [fix(v, key) for v in value]
        if isinstance(value, dict):
            for k in list(value):
                value[k] = fix(value[k], k)
            return value
        return value

    for records in record_lists:
        for r in records:
            fix(r)
    return changed
