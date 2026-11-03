"""Time helpers.

Timestamps are carried as strings in FMT ("2026-09-16 18:43:07") everywhere —
in the seed, in the records the state store serialises, in the templates — so
nothing has to remember whether a value is a datetime or a string.

The seed anchors every timestamp to BOOT rather than to a fixed calendar date,
so a case created "20 hours ago" is 20 hours old whichever day the demo is
given, and the 48-hour SLA chips read sensibly instead of all showing breached.
Adapted from the ChargeShield demo's datetime_helpers.
"""
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"
BOOT = datetime.now().replace(microsecond=0)

_PARSE_FORMATS = (FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", DATE_FMT, "%d-%b-%Y", "%d-%b-%Y %H:%M")


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def now_str() -> str:
    return fmt(now())


def fmt(dt) -> str:
    return dt.strftime(FMT) if isinstance(dt, datetime) else ""


def parse(value):
    """A datetime for any of the formats this app writes, else None."""
    if isinstance(value, datetime):
        return value
    text = (value or "").strip()
    if not text:
        return None
    for candidate in _PARSE_FORMATS:
        try:
            return datetime.strptime(text, candidate)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def hours_ago(hours: float, base: datetime = None) -> str:
    return fmt((base or BOOT) - timedelta(hours=hours))


def days_ago(days: float, base: datetime = None) -> str:
    return fmt((base or BOOT) - timedelta(days=days))


def add_hours(stamp: str, hours: float) -> str:
    dt = parse(stamp)
    return fmt(dt + timedelta(hours=hours)) if dt else ""


def add_days(stamp: str, days: float) -> str:
    return add_hours(stamp, days * 24)


def add_minutes(stamp: str, minutes: float) -> str:
    dt = parse(stamp)
    return fmt(dt + timedelta(minutes=minutes)) if dt else ""


def date_only(stamp: str) -> str:
    return (stamp or "")[:10]


def hours_until(stamp: str, base: datetime = None) -> float:
    """Signed hours from `base` (default now) until `stamp`; negative = past."""
    dt = parse(stamp)
    if not dt:
        return 0.0
    return (dt - (base or now())).total_seconds() / 3600.0


def wc_date(stamp: str) -> str:
    """'16-Sep-2026' — the date shape the watchlist console prints."""
    dt = parse(stamp)
    return dt.strftime("%d-%b-%Y") if dt else ""


def wc_datetime(stamp: str) -> str:
    dt = parse(stamp)
    return dt.strftime("%d-%b-%Y %H:%M") if dt else ""


def age_years(dob: str, at: datetime = None) -> int:
    born = parse(dob)
    if not born:
        return 0
    ref = at or now()
    return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


def years_between(a: str, b: str) -> float:
    da, db = parse(a), parse(b)
    if not da or not db:
        return 0.0
    return abs((da - db).days) / 365.25
