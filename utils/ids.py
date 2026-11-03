"""Deterministic identifiers for the seed.

Every id is derived from a short human key ("mercer", "sidorov-alert") through
sha256, so the seed produces the same ids on every boot and every machine.
That matters twice: the state store keys stored records by id, so a reseed
must land on the same ids or every stored action is orphaned; and the demo
script can quote a case id that will still be there tomorrow.

Shapes follow the systems being modelled — a 19-digit KYC case id, a 19-digit
client UID, a 32-hex screening case id, a 7-digit watchlist record id — but
the digits are hashes, never anything copied from a real system.
"""
import hashlib

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _h(key: str, salt: str) -> int:
    return int(hashlib.sha256(f"{salt}:{key}".encode("utf-8")).hexdigest(), 16)


def _digits(key: str, salt: str, n: int) -> str:
    return str(_h(key, salt) % (10 ** n)).zfill(n)


def _base36(value: int, n: int) -> str:
    out = []
    while value and len(out) < n:
        value, rem = divmod(value, 36)
        out.append(_B36[rem])
    return "".join(out).ljust(n, "0")[:n]


def kyc_case_id(key: str) -> str:
    """19 digits, '7686' prefix, like the KYC case ids in the case list."""
    return "7686" + _digits(key, "kyc", 15)


def client_id(key: str) -> str:
    """19 digits, '7680' prefix — the platform user / merchant UID."""
    return "7680" + _digits(key, "client", 15)


def alert_case_id(key: str) -> str:
    """32 hex characters — shared by the name-screening case and its WC case."""
    return hashlib.sha256(f"alert:{key}".encode("utf-8")).hexdigest()[:32]


def alert_id(key: str) -> str:
    """25 base-36 characters, the shape of a screening alert id."""
    return _base36(_h(key, "alertid"), 25)


def wc_id(key: str) -> str:
    """7-digit watchlist record id."""
    return str(1_000_000 + _h(key, "wc") % 9_000_000)


def request_id(key: str) -> str:
    return _digits(key, "req", 20)


def mcf_id(key: str) -> str:
    return "CaseID-" + _digits(key, "mcf", 18)


def doc_id(n: int) -> str:
    return f"d{n}"


def id_number(key: str, kind: str) -> str:
    """A plausible document number for the synthetic card of `kind`."""
    h = _h(key, "idnum")
    if kind == "Passport":
        return "P" + str(h % 10 ** 8).zfill(8)
    if kind == "DrivingLicense":
        d = str(h % 10 ** 9).zfill(9)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    if kind == "TradeLicense":
        return "TL-" + str(h % 10 ** 6).zfill(6)
    return str(h % 10 ** 10).zfill(10)
