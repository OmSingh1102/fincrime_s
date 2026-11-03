"""Real document images arriving on a case: which document row each image
fills, and what the case does when they land. The route (app.py) reads the
files; documents/uploads.py checks, cleans and stores them.

An uploaded image replaces the drawn one in its slot (the row keeps its id,
so every link to it still works) and carries `upload`: where the file is,
who sent it and when. The ID vendor never saw the new image, so its result
becomes "Not run". A case waiting for a resubmission is back in Data
Confirming. A case in Data Confirming sets the agent's earlier read aside
(ai_agent_history), so the AI Agent reads the new images when the case is
its to take."""
import io
import re
import zipfile

from fincrime.data import vocab
from fincrime.documents import uploads
from fincrime.engines import workflow
from fincrime.engines.errors import WorkflowError
from fincrime.utils import dates

UPLOADED = "Documents uploaded"
AWAITING = "Awaiting Documents"         # the pending reason of a case waiting for its images (recon_book)
RECEIVED = "Documents received"
PASSIVE_ACTIONS = (UPLOADED, RECEIVED)      # a person sent images; they did not work the case (ai_agent.eligible)
HISTORY_KEPT = 5

_NEW_ROW = {
    "passport": {"name": "Passport", "category": "Passport", "kind": "passport", "side": "front"},
    "passport_back": {"name": "Passport", "category": "Passport", "kind": "passport", "side": "back"},
    "secondary": {"name": "Secondary ID", "category": "NationalID", "kind": "national_id", "side": "front"},
    "poa": {"name": "Proof of address", "category": "ProofOfAddress", "kind": "utility_bill", "side": "page 1"},
}


def slot_row(case, slot):
    """The document row a slot fills, or None."""
    docs = case.get("documents", [])
    if slot == "passport":
        return next((d for d in docs if d.get("category") == "Passport" and d.get("side") != "back"), None)
    if slot == "passport_back":
        return next((d for d in docs if d.get("category") == "Passport" and d.get("side") == "back"), None)
    if slot == "secondary":
        return next((d for d in docs if d.get("category") not in ("Passport", "ProofOfAddress")), None)
    return next((d for d in docs if d.get("category") == "ProofOfAddress"), None)


def has_uploads(case):
    return any(d.get("upload") for d in case.get("documents", []))


def slot_title(doc):
    """What a document row holds, in the words of the upload slots."""
    if doc.get("category") == "Passport":
        return uploads.SLOT_LABELS["passport_back" if doc.get("side") == "back" else "passport"]
    if doc.get("category") == "ProofOfAddress":
        return uploads.SLOT_LABELS["poa"]
    return doc.get("name") or uploads.SLOT_LABELS["secondary"]


def complete(case):
    """Has a case waiting for its images the ones the AI Agent needs: the
    passport's data page and, where the case asks for one, the proof of
    address? (The back page and the secondary ID are read when they come.)"""
    passport, poa = slot_row(case, "passport"), slot_row(case, "poa")
    return bool(passport and passport.get("upload")) and (not case.get("poa_required") or bool(poa and poa.get("upload")))


def poa_details(form, case, strict=True):
    """The proof of address's type, issue date and country from the upload
    form, checked. The date may be left empty: the agent then takes the one
    it reads off the document. Not `strict` (an image from a ZIP, with no
    form), an unknown type is "other", which the type check then refuses."""
    row = slot_row(case, "poa") or {}
    kind = (form.get("poa_kind") or row.get("kind") or "").strip()
    if kind not in vocab.ACCEPTED_POA_TYPES and kind != "other":
        if strict:
            raise WorkflowError("Choose the proof of address's type.")
        kind = "other"
    issued = (form.get("poa_issued_on") or "").strip()
    if issued and dates.parse(issued) is None:
        raise WorkflowError("The proof of address's issue date must be a date (YYYY-MM-DD).")
    country = (form.get("poa_country") or row.get("country") or case["client"].get("nationality") or "").strip().upper()
    if country not in vocab.COUNTRY_NAMES:
        raise WorkflowError("Choose the country that issued the proof of address.")
    return {"kind": kind, "issued_on": issued[:10], "country": country}


def attach(case, images, actor, poa=None):
    """Put prepared images (slot -> (jpeg, info, original file name)) on the
    case; `poa` is poa_details() when a proof of address is among them.
    Returns the labels of the slots filled."""
    _require_open(case)
    now = dates.now_str()
    filled = []
    for slot, _label in uploads.SLOTS:
        if slot not in images:
            continue
        jpeg, info, original = images[slot]
        row = slot_row(case, slot)
        if row is None:
            row = dict(_NEW_ROW[slot], id=_next_id(case))
            case["documents"].append(row)
        previous = (row.get("upload") or {}).get("path", "")
        path = uploads.store(case["id"], row["id"], jpeg, previous=previous)
        row.pop("hub_file", None)
        row.pop("awaiting", None)
        row["quality"] = "ok"
        row["upload"] = {"path": path, "media_type": info["media_type"], "sha256": info["sha256"],
                         "width": info["width"], "height": info["height"], "original_name": _display_name(original),
                         "by": actor, "at": now}
        if slot == "poa" and poa:
            row.update(kind=poa["kind"], country=poa["country"], issued_on=poa["issued_on"],
                       name="Proof of address — " + vocab.ACCEPTED_POA_TYPES.get(poa["kind"], "other document"))
            row["issued_on_source"] = "upload" if poa["issued_on"] else ""
        filled.append(uploads.SLOT_LABELS[slot])
    if not filled:
        raise WorkflowError("Choose at least one image to upload.")
    if "passport" in images:
        case["vendor_check"].update(result="Not run", reason="", checked_at="", extracted={},
                                    vendor_reason="The ID image was uploaded in the console; no ID vendor checked it.")
    names = ", ".join(n for n in (_display_name(images[s][2]) for s, _ in uploads.SLOTS if s in images) if n)
    workflow._audit(case, actor, UPLOADED, f"{', '.join(filled)} uploaded as image(s)" + (f" ({names})" if names else "") + ".")
    case["documents_uploaded_at"] = now
    if case["status"].startswith("Resubmit"):
        case["status"] = "DataConfirming"
        case["pending_reason"] = ""
        workflow._audit(case, actor, RECEIVED, "The customer's new documents arrived; the case is back in Data Confirming.")
    if case.get("awaiting_documents") and complete(case):
        case.pop("awaiting_documents")
        if case.get("pending_reason") == AWAITING:
            case["pending_reason"] = ""
        workflow._audit(case, actor, RECEIVED, "The documents the case waited for are in: the passport"
                        + (" and the proof of address" if case.get("poa_required") else "") + ".")
    if case["status"] == "DataConfirming" and case.get("ai_agent"):
        history = case.setdefault("ai_agent_history", [])
        history.append(case.pop("ai_agent"))
        del history[:-HISTORY_KEPT]
    return filled


def date_from_read(case, got):
    """An uploaded proof of address whose issue date nobody typed takes the
    one Claude read off it, so the proof-of-address checks have a date."""
    rows = workflow.poa_docs(case)
    row = rows[0] if rows else None
    read = ((got or {}).get("poa") or {}).get("issued_on", "")
    if row and row.get("upload") and not row.get("issued_on") and dates.parse(read):
        row["issued_on"] = read[:10]
        row["issued_on_source"] = "read"


def _require_open(case):
    if case["status"] in vocab.CLOSED_STATUSES:
        raise WorkflowError("The case is closed; documents can only be added to an open case.")
    if case.get("agency"):
        raise WorkflowError("Image upload takes an individual's documents (passport, secondary ID, proof of address).")


def _next_id(case):
    nums = [int(d["id"][1:]) for d in case.get("documents", []) if str(d.get("id", ""))[1:].isdigit()]
    return f"d{max(nums, default=0) + 1}"


def _display_name(name):
    """A file name as the analyst's computer gave it, shown only: no path,
    printable, short."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in name if ch.isprintable())[:120]


# ── images in a ZIP beside an uploaded CSV ──────────────────────────────────
ZIP_NAME = re.compile(r"^(?P<case>.+?)_(?P<slot>passport_back|passport|secondary|poa)\.(?:jpe?g|png|webp)$", re.IGNORECASE)
ZIP_MAX_FILES = 2100                    # four images for each of 500 cases, and some to spare
ZIP_MAX_TOTAL = 600 * 1024 * 1024       # uncompressed, checked before a byte is read


def read_zip(data):
    """({CASE ID: {slot: (bytes, file name)}}, [what was skipped and why])
    from a ZIP of images named <case id>_passport.jpg, _passport_back,
    _secondary, _poa (.jpg, .png or .webp; folders ignored). The sizes the
    archive declares are checked before anything is read, and no entry is
    read past the per-image limit, so a ZIP bomb stops at the door."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise WorkflowError("The images file is not a ZIP archive.") from None
    found, skipped = {}, []
    with archive:
        entries = [e for e in archive.infolist() if not e.is_dir()]
        if len(entries) > ZIP_MAX_FILES:
            raise WorkflowError(f"The ZIP holds {len(entries)} files; one upload takes at most {ZIP_MAX_FILES}.")
        if sum(e.file_size for e in entries) > ZIP_MAX_TOTAL:
            raise WorkflowError(f"The ZIP unpacks to more than {ZIP_MAX_TOTAL // (1024 * 1024)} MB; split it.")
        for e in entries:
            name = e.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if e.filename.startswith("__MACOSX/") or name.startswith(".") or name.lower() == "thumbs.db":
                continue
            m = ZIP_NAME.match(name)
            if not m:
                skipped.append(f"{name}: not named <case id>_passport, _passport_back, _secondary or _poa")
                continue
            if e.flag_bits & 0x1:
                skipped.append(f"{name}: encrypted")
                continue
            if e.file_size > uploads.MAX_BYTES:
                skipped.append(f"{name}: larger than {uploads.MAX_BYTES // (1024 * 1024)} MB")
                continue
            try:
                with archive.open(e) as fh:
                    blob = fh.read(uploads.MAX_BYTES + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                skipped.append(f"{name}: could not be read ({exc})")
                continue
            if len(blob) > uploads.MAX_BYTES:
                skipped.append(f"{name}: larger than {uploads.MAX_BYTES // (1024 * 1024)} MB")
                continue
            slots = found.setdefault(m["case"].upper(), {})
            if m["slot"].lower() in slots:
                skipped.append(f"{name}: a second image for the same place; the first is kept")
                continue
            slots[m["slot"].lower()] = (blob, name)
    return found, skipped
