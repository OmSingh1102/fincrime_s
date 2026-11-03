"""Mock in-house CMS / internal blacklist screening.

The seed writes the first screening run; this produces the second one the
tool performs after Data Confirming, on the confirmed name. The result is
whatever the first run found — the demo has no live list to consult."""


def rescreen_rows(case, at):
    first_hit = any(r["result"] == "Hit" for r in case["screening"]["rows"] if r["platform"] == "In-house CMS")
    name = case["client"]["legal_name"]
    return [
        {"platform": "In-house CMS", "user_name": name, "screen_time": at,
         "list": "Law Enforcement/Special Interest Categories/Regulatory Enforcement" if first_hit else "Sanctions/PEP/Adverse Media",
         "result": "Hit" if first_hit else "No Match", "alert_id": case["screening"]["alert_case_id"] if first_hit else ""},
        {"platform": "Internal_blacklist", "user_name": name, "screen_time": at,
         "list": "Internal_blacklist", "result": "No Match", "alert_id": ""},
    ]
