"""The reconciliation log becomes the case book.

read_rows() opens the CSV (UTF-8, no BOM, en dashes in six headers) and keys
every row by a normalised header; build_case() turns one row into the case
record the console's file book (fincrime/data/recon_book.py) is built from.
The analyst's historical outcome is parsed into `history`: what the file says
was done, kept beside the case for comparison only.
"""
import csv
import os

from fincrime.filebook import parse

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOG_PATH = os.path.join(DATA_DIR, "kyc_wc_recon_log.csv")

REQUIRED = (
    "kyc_case_id", "customer_name", "customer_type", "customer_provided_dob", "customer_registered_address",
    "customer_risk_rating", "world_check_uid", "world_check_entity_subject_name", "world_check_listed_dob",
    "world_check_listed_address_jurisdiction", "watchlist_category", "match_score", "matched_attributes",
    "discrepancy_analysis_dob_address", "world_check_narrative_trigger_details", "case_analyst", "review_date",
    "disposition_status", "compliance_action_regulatory_notes", "passport_issuing_country", "passport_no",
    "name_passport_data_page_viz", "name_passport_mrz_line_1", "secondary_id_type", "name_secondary_id",
    "address_passport", "address_secondary_id", "proof_of_address_type", "address_proof_of_address",
    "name_variance_type", "address_variance_type", "pain_point_detail", "normalisation_resolution_applied",
    "screening_impact", "match_score_after_normalisation", "analyst_handling_time_mins",
)

def read_rows(path=LOG_PATH):
    """Every row, keyed by the normalised header. Raises if a column the
    build needs is missing, so a wrong file fails loudly at boot."""
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        keys = {parse.header_key(h): h for h in (reader.fieldnames or [])}
        missing = [k for k in REQUIRED if k not in keys]
        if missing:
            raise ValueError(f"{os.path.basename(path)} lacks columns: {', '.join(missing)}")
        rows = []
        for raw in reader:
            rows.append({parse.header_key(h): (v or "").strip() for h, v in raw.items() if h is not None})
    return rows


def build_case(row, seq):
    ctype = parse.customer_type(row["customer_type"])
    country = parse.country_code(row["passport_issuing_country"])
    wc = parse.wc_name(row["world_check_entity_subject_name"])
    dispo = parse.disposition(row["disposition_status"])
    secondary = parse.secondary_type(row["secondary_id_type"])
    poa = parse.poa_type(row["proof_of_address_type"])
    return {
        "id": row["kyc_case_id"], "seq": seq, "country": country, "country_name": parse.COUNTRY_NAMES.get(country, country),
        "raw": dict(row),
        "customer": {"name": row["customer_name"], "type": ctype["type"], "corporate": ctype["corporate"],
                     "nri": ctype["nri"], "ubo": ctype["ubo"], "dob": parse.customer_dob(row["customer_provided_dob"]),
                     "address": row["customer_registered_address"], "risk": row["customer_risk_rating"].title()},
        "wc": {"uid": row["world_check_uid"], "name_raw": row["world_check_entity_subject_name"],
               "surname": wc["surname"], "given": wc["given"], "aka": wc["aka"], "native": wc["native"],
               "mononym": wc["mononym"], "dob": parse.wc_dob(row["world_check_listed_dob"]),
               "location": parse.wc_location(row["world_check_listed_address_jurisdiction"]),
               "category": row["watchlist_category"], "group": parse.wc_group(row["watchlist_category"]),
               "score": parse.percent(row["match_score"]), "matched_attributes": row["matched_attributes"],
               "narrative": row["world_check_narrative_trigger_details"]},
        "documents": {
            "passport": {"number": row["passport_no"], "country": country,
                         "viz": parse.viz(row["name_passport_data_page_viz"]),
                         "mrz": parse.mrz1(row["name_passport_mrz_line_1"]),
                         "address": parse.doc_address(row["address_passport"])},
            "secondary": dict(secondary, name=row["name_secondary_id"],
                              address=parse.doc_address(row["address_secondary_id"])),
            "poa": dict(poa, address=row["address_proof_of_address"]),
        },
        "history": {"analyst": row["case_analyst"], "review_date": row["review_date"], "disposition": dispo["disposition"],
                    "outcome": dispo["outcome"], "action": dispo["action"], "corrected": dispo["corrected"],
                    "notes": row["compliance_action_regulatory_notes"], "discrepancy": row["discrepancy_analysis_dob_address"],
                    "name_variance": row["name_variance_type"], "address_variance": row["address_variance_type"],
                    "pain_point": row["pain_point_detail"], "resolution": row["normalisation_resolution_applied"],
                    "impact": row["screening_impact"], "score_after": parse.percent(row["match_score_after_normalisation"]),
                    "minutes": parse.minutes(row["analyst_handling_time_mins"])},
        "synth": {}, "files": [], "identity": {}, "screening": {}, "trail": [],
        "status": "", "group": "", "queue": "", "priority": "", "sla_hours": 0, "due_at": "",
        "decision": None, "ai": {}, "audit": [],
    }


def load_cases(path=LOG_PATH):
    return [build_case(row, i + 1) for i, row in enumerate(read_rows(path))]
