"""Mock identity-verification vendor payload — the "Details" behind a vendor
row. The names are text labels; nothing here calls a vendor. The values are
what the vendor is said to have extracted, marked "(vendor)" so nobody takes
them for the console's own OCR (documents/ocr, on uploaded images)."""


def details(case):
    vc = case["vendor_check"]
    ex = vc["extracted"]
    return {
        "vendor": vc["vendor"],
        "result": vc["result"],
        "checked_at": vc["checked_at"],
        "checks": [
            ("Document type", ex.get("id_type", "")),
            ("Document number", ex.get("id_number", "")),
            ("Name (vendor)", ex.get("name", "")),
            ("Date of birth (vendor)", ex.get("dob", "")),
            ("Nationality (vendor)", ex.get("nationality", "")),
            ("Expiry (vendor)", ex.get("expiry", "")),
            ("Face match", "Pass" if vc["result"] == "Approved" else "Not assessed"),
            ("Liveness", "Pass" if vc["result"] == "Approved" else "Not assessed"),
            ("Tamper check", "Pass" if vc["result"] == "Approved" else "Review"),
            ("Vendor reason", vc.get("vendor_reason") or "-"),
        ],
    }
