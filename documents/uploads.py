"""Real document images uploaded in the console: checked, cleaned and kept
on this machine, one file per document slot of a case.

Every image is opened with Pillow, so a renamed file or a format the model
cannot take is refused. It is turned upright from its EXIF orientation,
flattened to RGB, shrunk to at most MAX_EDGE pixels on its long side (what
Claude reads at most) and saved again as JPEG, which drops the EXIF block,
GPS position included. The files live in base(): FCC_UPLOAD_DIR, else an
uploads/ folder beside the state database (so the tests' temporary
database gets its own). .gitignore keeps uploads/ out of the repository:
these are real people's identity documents."""
import hashlib
import io
import os
import re
import shutil

from PIL import Image, ImageOps, UnidentifiedImageError

from fincrime.data.state_store import StateStore
from fincrime.engines.errors import WorkflowError

FORMATS = ("JPEG", "PNG", "WEBP")
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000         # a 7000 x 7000 scan; anything larger is refused before it is decoded
MAX_EDGE = 2576                 # the longest side Claude reads at full detail
MEDIA_TYPE = "image/jpeg"

# Where a document image can go on a case, in the order the agent reads them.
SLOTS = (("passport", "Passport data page"), ("passport_back", "Passport back page"),
         ("secondary", "Secondary ID"), ("poa", "Proof of address"))
SLOT_LABELS = dict(SLOTS)


def base():
    return os.environ.get("FCC_UPLOAD_DIR") or os.path.join(os.path.dirname(os.path.abspath(StateStore.path)), "uploads")


def _heic(data):
    return data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


def prepare(data, label="The image"):
    """(JPEG bytes, info) for an uploaded image, or a WorkflowError that says
    what is wrong with it, naming the slot (`label`)."""
    if not data:
        raise WorkflowError(f"{label}: the file is empty.")
    if len(data) > MAX_BYTES:
        raise WorkflowError(f"{label}: the file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    refusal = (f"{label}: not an image this tool can read. Upload a JPEG, PNG or WebP photo or scan"
               + (" (an iPhone HEIC photo: export it as JPEG first)." if _heic(data) else "."))
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt, (width, height) = probe.format, probe.size
            if width * height > MAX_PIXELS:
                raise WorkflowError(f"{label}: the image is {width} x {height} pixels; the limit is {MAX_PIXELS // 1_000_000} million.")
            probe.verify()
        if fmt not in FORMATS:
            raise WorkflowError(refusal)
        with Image.open(io.BytesIO(data)) as im:
            upright = ImageOps.exif_transpose(im)
            flat = _flatten(upright)
    except Image.DecompressionBombError:
        raise WorkflowError(f"{label}: the image is too large to open safely.") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise WorkflowError(refusal) from None
    flat.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    flat.save(out, "JPEG", quality=90, optimize=True)       # no exif= argument: the EXIF block is not written
    jpeg = out.getvalue()
    return jpeg, {"media_type": MEDIA_TYPE, "width": flat.width, "height": flat.height,
                  "sha256": hashlib.sha256(jpeg).hexdigest(), "format": fmt}


def _flatten(im):
    """RGB on white: a transparent PNG would otherwise turn black in a JPEG."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        page = Image.new("RGB", rgba.size, "white")
        page.paste(rgba, mask=rgba.getchannel("A"))
        return page
    return im.convert("RGB")


def _safe(part):
    return re.sub(r"[^A-Za-z0-9._-]", "_", part or "").strip("._") or "case"


def _inside(rel):
    root = os.path.realpath(base())
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep):
        raise WorkflowError("That document file is not in the upload folder.")
    return full


def store(case_id, doc_id, jpeg, previous=""):
    """Write one prepared image for a case's document row; returns its path
    relative to base(). The file the row pointed to before is removed."""
    rel = f"{_safe(case_id)}/{_safe(doc_id)}-{hashlib.sha256(jpeg).hexdigest()[:8]}.jpg"
    full = _inside(rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".part"
    with open(tmp, "wb") as fh:
        fh.write(jpeg)
    os.replace(tmp, full)
    if previous and previous != rel:
        try:
            os.remove(_inside(previous))
        except (OSError, WorkflowError):
            pass
    return rel


def load(rel):
    with open(_inside(rel), "rb") as fh:
        return fh.read()


def remove_case(case_id):
    shutil.rmtree(os.path.join(base(), _safe(case_id)), ignore_errors=True)


def remove_all():
    """Every uploaded image (the demo reset puts every case back on its seed)."""
    shutil.rmtree(base(), ignore_errors=True)
