# FinCrime KYC/KYB Operations Hub — demo

A Straive demo of a unified back-office case-management hub for individual and
business KYC/KYB, built to be shown to any client. It reproduces the client's as-is screens — the internal case
manager and a simulated World-Check console — driven by the SOP's L1 → L2 →
L3 workflow, and adds the "Unified Back-Office Case Management" layer the
SOP proposes (§5.2): automated skill-based routing, case on-demand with
capacity, a live tier pipeline with hand-off lag, tracker views that replace
the spreadsheets, an investigation file per escalation, a global audit log,
QC sampling with a scorecard, and a four-eyes Checker 2 on business approvals.

Screening runs in two workflows. **Auto-verified**: no match on any list, or
every watchlist record cleared by the *AI screening rules*, so the screening
case closes itself and a Individuals case completes with no analyst step.
**Human-in-the-loop**: at least one record the rules could not clear, so an
analyst handles only what is left. The console shows *Auto-verified* only on a
case that is closed with nothing more asked of the customer: one the rules
cleared that still waits on the customer's reply to an RFI or a resubmission
shows **Waiting on customer** (badge, list filter, analytics) until it closes.
The "AI" is a deterministic rule book, not
a model — date-of-birth mismatch, gender mismatch, an optional nationality
rule and a sanctions guard, each switchable by the team lead on the AI
Screening page — because a compliance team must be able to read and audit
why a record was cleared.

**The case book.** By default (`CONSOLE_CASES=screening`) the console starts
with no case at all: each ID uploaded on the Screening cases page becomes
one, screened against World-Check (below), and the reconciliation log's
1,000 rows are used only as the stand-in World-Check. That book keeps its own
database, `fincrime_screening.db`, so the work stored on the 1,000 cases is
untouched. `CONSOLE_CASES=file` opens the console on the client's
reconciliation log, a 1,000-row file
(`fincrime/filebook/data/kyc_wc_recon_log.csv`): every row is an open onboarding
case at L1 Data Confirming with its World-Check hit, carrying the file's own
case ids, passport numbers, names as the CRM captured them (swapped,
nicknamed or garbled: the confirming task) and World-Check records.
Individuals are Individuals cases (943); companies are companies on the Businesses tenant with
their UBO (57). The AI screening rules clear 592 hits at intake, every one
recorded in the file as a false positive, and leave 408 for analysts, among
them every exact or day/month-transposed date of birth and every company.
Each case page ends with what the file's analyst recorded, for comparison.
`CONSOLE_CASES=demo` brings back the nineteen scripted scenarios (the
walkthrough further down, and what the test suite runs on).

**The case list opens on the open cases.** Sign-in lands on the case list,
and with no filter set it lists only the cases still open (human-in-the-loop
and those waiting on the customer), never a Verified or Rejected one; sort,
page size and Priority Review keep that view. Any filter (a case id, a name,
Status = Verified, Workflow = Auto-verified) searches the whole book, and
Clear goes back to the open cases.

**The AI Agent** (`fincrime/engines/ai_agent.py`) does Data Confirming on
every individual's case. It starts on its own when the server takes its
first request. Claude (`claude-opus-4-8` through Straive's LLM Foundry,
`LLM_FOUNDRY_KEY`) reads every document on the case from its image, rendered
to PNG with resvg: the passport's data page and back page, the secondary ID
and the proof of address. Fixed checks, not the model, compare that read
with the case: readable, name (the file book's name rules: nicknames, word
order, initials, a garbled CRM encoding; not the English first name a
naturalised citizen goes by, which only the naturalisation certificate
settles), date of birth, nationality, ID
number, expiry, the vendor's result, an endorsement on the passport's back
page, the secondary ID (readable, the same person as the passport or the
record, the same date of birth; in a surname the passport does not print it
needs the marriage certificate, even when the record keeps both surnames;
its address is shown, not checked), and the
proof of address (readable, holder's name, address, age, type, country).
Every check passes: the agent confirms the data through the analysts' own
path, and corrects the record's postal code or old place names to the proof
of address's (never a code another document prints differently). A case whose World-Check alerts the AI screening rules cleared is then
Verified with nobody touching it; a case with an alert left goes to Data
Screening, where an analyst clears the alert, with the agent's **watchlist
finding** beside each open record (*likely true match*: the passport's name
is the listed name and its date of birth the listed one, or the same with
day and month swapped; *possible match*; *likely a different person*). The
finding shows on the case, the screening case and the case list's filter;
it decides nothing. Some failures the agent settles itself, when every
other check passed, no alert is left and nobody holds the case:

- *The record's own slip, where the documents agree with one another*: the
  agent corrects the record, writes the change to the change log and the
  audit, confirms, and the screening re-runs on the corrected record.
  Claude's verdict is asked again on it. This covers a record that kept the
  father's name and dropped the holder's own (PRAKASH MORE for the
  passport's MORE, MANASI PRAKASH), a surname no document prints beside the
  holder's given name (a married name written on the form), an English first
  name no document prints when the secondary ID is the naturalisation
  certificate (KEVIN NGUYEN for the passport's NGUYEN, KHOA QUOC: the record
  takes the legal name and keeps the typed one as the *preferred name*, as
  the file's analysts did), and a PO Box where the proof of address and the
  secondary ID give the same home.
- *What only the customer can give*: the agent asks for it through the
  analysts' own steps. Resubmit POA covers a proof of address older than
  six months. An RFI covers the marriage certificate that links the
  passport's name to the record's or the secondary ID's (or the
  naturalisation certificate, when the case does not already hold it), or
  which of two homes is the
  main one. A case waiting on the agent's RFI leaves the queue until
  someone takes it. The list's *AI Agent* filter shows these as *Asked the
  customer*, and a request approves nothing.

Anything blurred, cut off,
expired, missing or different, a declined ID, or a model call that fails:
the case stays with an analyst, labelled human-in-the-loop, and its page
shows the reasons and what the agent read. An unreadable or covered ID stays
with a person rather than being asked for again (what it hides may be the
point), and so does a spelling the documents themselves disagree on.
Companies, and cases a person already holds or has worked,
are the analysts'. Two cases at a time (the gateway refuses more concurrent
calls), about 940 cases and three to four hours for the file; each read is
stored, so a restart does not read a case twice (a case read before the
agent read every document, that nobody has touched, starts again and is read
again). A team lead can pause and resume it on the AI Screening page.
Auto-verification also refuses an expired or vendor-declined ID now,
whoever confirmed the data.

**Bad photos of the ID.** The planted capture defects copy what real
uploads that needed a person looked like: a phone lying on the passport,
a page shot at an angle with a phone over its corner, a cracked and worn
card, a page cut off by a shot taken too close, a dark blurred shot. They
are drawn as phone photos on a cloth (`fincrime/filebook/documents.py`), and
each hides at least a date or the number, so the fixed checks fail even if
the model names nothing. The ID vendor passes the first three, as it did on
the real uploads; only the AI Agent's look at the picture stops them. Claude
names the problem (covered, angled, damaged, cut off, blurred, too dark) and
says whether all four edges and the MRZ are in shot (a slight tilt alone
stops nothing: good photos have one); the case goes to a
person with a suggested Resubmit ID reason (for example *Please resubmit a
clear, good quality valid proof of identity and ensure all information on
the document is visible*), which the Resubmit dialog arrives filled in
with. The analyst still decides and clicks OK. A stored case whose ID image
was redrawn since it was read starts again from the file, so only those
are read a second time.

**Upload cases** (the button on the case list, `/kyc/upload`) adds new
onboarding cases from a CSV in the reconciliation log's layout; the columns
that record what an analyst decided may be left out. Each row goes through
intake like the file's: the rules screen it, an auto-verified one is read by
the AI Agent at once, the rest go to analysts. Existing case ids are
skipped, up to 500 rows a file. *Download a sample* gives five new cases
(two the agent should verify, a CRM surname spelt another way, an exact
date-of-birth hit, a company). Uploads are stored and rebuilt on restart
until Reset demo.

**Cases with their real images.** The same form takes an optional ZIP of
the customers' ID images beside the CSV, each named after its case
(`KYC-2026-5001_passport.jpg`, `_passport_back`, `_secondary`, `_poa`;
JPEG, PNG or WebP). Each image is checked and cleaned like one uploaded on
the case, and put in its place on the new case; the AI Agent then reads the
images themselves instead of drawings. The columns a document's image is
drawn from (the passport's printed and MRZ names, the secondary ID's name,
the three document addresses) become optional: a row that leaves them all
empty is a case whose images come as files, and it waits ("Awaiting
Documents", not the agent's yet) until its passport and proof of address
are in, from the ZIP or with Upload images on the case; an empty slot shows
"Awaiting upload". The document types stay optional columns that name the
slots (a proof of address of no named type is "other", for an analyst to
set). The ZIP's declared sizes are checked before anything is read; a file
that is wrongly named, for another case, a second image for the same
place or too big is listed in the notification, not attached. The Upload
page offers the five sample cases three ways: with the columns the images
are drawn from, without them, and their images as a ZIP of JPEGs (drawn,
so fictional), which together walk through the image-first upload.
Removing an upload deletes its images.

**Real document images** (`fincrime/documents/uploads.py`,
`fincrime/engines/case_documents.py`). The case page's Documents tab has
**Upload images**: the customer's own photos or scans of the passport's
data page and back page, the secondary ID and the proof of address (JPEG,
PNG or WebP, up to 10 MB each). Each one is opened with Pillow (a renamed
file or an iPhone HEIC is refused with the reason), turned upright from its
EXIF orientation, shrunk to 2,576 pixels on its long side and saved again
as JPEG without its EXIF block (camera, GPS), then takes the drawn image's
place in its slot. The proof of address takes the type and country chosen
in the dialog and an issue date, typed in or read off the document by the
AI Agent. The ID vendor never saw the new image, so its result reads "Not
run". A case waiting for a resubmission is back in Data Confirming, and a
case in Data Confirming sets the agent's earlier read aside: the agent
reads the real images (Claude gets the photo itself, not a drawing) when
the case is its to take. Uploading images is not working the case, so a
case with an open alert stays the agent's. The files live in `uploads/`
beside the state database (`FCC_UPLOAD_DIR` overrides), which `.gitignore`
keeps out of the repository; the image route serves them only to
analysts of the case's tenant, never cached. They survive a restart, and
Reset demo deletes them. **These are real people's identity documents**:
they stay on this machine, but the AI Agent sends them to Straive's LLM
Foundry to be read, so use this only with the client's approval. A
Render deploy's disk is wiped on every deploy and restart.

**OCR on uploaded images** (`fincrime/documents/ocr.py`). When the agent
reads a case with uploaded images, Tesseract, an OCR engine running on this
machine (no image leaves it for this step), reads them too: each image's
printed text, and the passport's machine-readable zone (MRZ). The MRZ is
parsed under ICAO 9303 (document number, nationality, date of birth, sex,
expiry, names) with every check digit tested, after the lower part of the
page is straightened (a photo is rarely level) and read a few ways, each
check-digit field taken from a reading its own check digit confirms. Once
the check digits add up, the check **Passport MRZ read by OCR** holds
Claude's read to it: a different number, date of birth, expiry or
nationality sends the case to a person (one of the two misread the page,
or the page disagrees with itself); a difference only in characters OCR
mistakes for one another (O and 0, S and 5) is shown, not failed; an MRZ OCR
cannot read is information only. The AI Agent tab shows the MRZ lines, each
field with its check digit, and the text OCR read. Drawn documents are not
OCR'd, and the decided cases are not read again. Install Tesseract with
`winget install UB-Mannheim.TesseractOCR` (or unpack that installer into
`%LOCALAPPDATA%\Programs\Tesseract-OCR`, which needs no admin rights; on
Linux `apt install tesseract-ocr`); `TESSERACT_CMD` points at it anywhere
else. Without it the check says OCR is not available and blocks nothing.
The ID vendor pop-up's extracted values are labelled "(vendor)": they are
the mock vendor's, not this OCR.

**SOP rules, changed at runtime** (`fincrime/engines/sop.py`, the **SOP
rules** page in the left menu, `/sop`). The parameters the verification
checks take from the SOP are settings, not code: the proof of address's
age limit (183 days, "6 months") and the cards it does not apply to
(Aadhaar), the accepted proof-of-address types, the sanctioned or
prohibited countries, the L1 SLA (48 hours from ingestion) and the EDD SLA
(14 days from escalation), and whether OCR must read an uploaded passport's
MRZ. Everyone can read them; the team lead (QC) changes one at a time, each
change checked (30–730 days, 1–240 hours, 1–60 days, known countries and
types, at least one accepted type) and kept in the page's history (who,
when, from what to what), through a restart, until Reset demo. Every check
reads them when it runs, so a change applies at once, with no restart: the
Data Confirm dialog's proof-of-address checks, the AI Agent's checks, the
Resubmit POA reasons, Claude's policy text and every open case's SLA
deadline. The open cases the agent decided under other values and nobody
has touched since are checked again on its stored read, with no new look at
the images: one waiting for the new proof of address it asked for comes
back and verifies when the one it holds now passes (a longer limit), and
one with a person is confirmed when it passes or has its reasons brought
up to date. Closed cases stay closed. At the shipped values every label and
text reads as before ("Issued within the last 6 months").

**Architecture** (`/architecture`, Insights in the left menu). One page for
the presentation, on the Screening cases flow as it runs: upload, image
preparation, the vision model's read of the ID, the checks code makes on
the read, the World-Check search (the stand-in, or World-Check One once its
account is on Settings), the AI's decision on each hit under the SOP, and
the analyst's alert outcome; which steps are a model's judgement, which are
code and which are people; the services each step stands in for and the
existing solution that takes it over by API (a specialist ID-document API,
an ID-verification provider, World-Check One, the client's onboarding
system); the models in use (from Settings) with what the Model evaluation
page has measured on the uploads so far.

**Screening cases** (`/scan`, the home page of the default book; `fincrime/documents/id_scan.py`,
`fincrime/watchlist.py`, `fincrime/id_screening.py`, `fincrime/adapters/world_check.py`).
Start from an ID document, with no CSV: every uploaded ID becomes a case.
1. *Read.* The image is checked and cleaned (upright, resized, EXIF and GPS
   removed), then a vision model reads it (Claude Opus 4.8 by default; Sonnet
   5.5, Gemini 2.5 Pro and Flash, GPT-5 and GPT-5 mini, through the LLM
   Foundry gateway): every printed field, the MRZ lines, what spoils the
   photo. No OCR engine of our own runs. Checks computed by code follow:
   photo quality, the main fields, expiry, age, the MRZ check digits of the
   model's transcription (its runs of < counted out to 44), the printed
   fields against the zone, a document that is not an ID.
2. *Search.* The name, date of birth, sex and nationality go to World-Check.
   Without a licence that is the **stand-in World-Check**: the World-Check
   side of the reconciliation log's 1,000 rows (UID, listed name and aliases,
   listed date of birth without the analysts' note beside it, places, list,
   why listed), searched by name the way World-Check's name search works
   (word order, diacritics, hyphens, initials and a missing middle name set
   aside), with the date of birth and country reported beside each hit and
   never used to drop one. With a World-Check One API key, secret and group
   entered on **Settings**, the same step calls World-Check One instead
   (signed with HMAC-SHA256 as LSEG documents; written from the public
   description and not yet run against the service: the host and paths are
   settings, and *Test connection* calls the groups list).
3. *Decide.* For each hit, a model decides as the Model evaluation's decision
   model does: the **screening SOP** (the files' v1–v3, then any version
   saved on the Screening SOP page) as instructions, the client's variance
   reference as the playbook, and the resolved cases of the log most like
   this one with what the analysts decided (never the case behind the very
   record that was hit). It answers clear / true match / ask the customer /
   escalate, with its reasons, the date-of-birth finding and the name and
   address discrepancy, shown as a suggestion (false positive, possible match,
   true match). The calls run on worker threads; the page refreshes itself
   when they answer. No screening rule is in code.
4. *Confirm.* The case opens in the console's case layout: the sidebar
   (status, Human-in-the-loop while a hit waits, Review, the progress from
   Submitted through ID Verification, Data Screening and AI Review to
   Completed, each with its time) and the Overview, Documents, Screening and
   Decisions tabs. The Screening tab's screening result opens (*Details*) the
   name-screening page: Basic and Screening Information, and List Screening
   with an alert row per hit (the AI assessment, the alert outcome; a row
   opens to the AI's reasons, submitted against matched data, and the
   record). An analyst gives each alert its outcome with *Change Alert
   Outcome* (one or several ticked; true positive, potential match, false
   positive, with reason, risk and comment; the AI's suggestion prefilled,
   and whether they agreed kept); *AI assessment* asks the AI again. The case
   closes when every hit has an outcome, or with no hit when the analyst
   confirms there is none. Every value on these pages is the upload's, the
   model's read, the hits' or an analyst's: the old book's vendor, merchant
   and business-line fields are left out, not filled.
   **Open in World-Check** (the case, or *Open in WC* on a hit, in a new tab)
   shows the case in the simulated World-Check console the 1,000-case book
   uses: the matches list with its status and type filters, each record's
   comparison data, key data, biography, aliases and sources (a stand-in hit
   is the file's record, built as that book builds it), the audit and the
   export PDF. Resolving a match there (true positive, potential match, false
   positive, with risk, reason and comment) confirms the hit on the case, and
   a hit confirmed on the case shows as resolved there. The AI's suggestion
   is shown there too: an *AI suggestion* column in the matches list, and on
   each record its verdict, confidence, date-of-birth finding and reasons,
   with *Ask the AI again* while the match is unresolved.
**Services connected by API** (`fincrime/adapters/`; each one's account on
Settings, nothing of it in the code):
- *The intake API* (`POST /api/v1/cases`, `GET /api/v1/cases/<id>`,
  `GET /api/v1/cases?reference=`): the client's onboarding system sends an
  ID image (multipart `image`, or JSON `image_base64`, with its own
  `reference` for the customer) and reads the case back as JSON: the read,
  the World-Check hits with the AI's suggestion and each outcome,
  `auto_verified`, the ID verification, the outcome once closed. Each system
  has its own key (`X-API-Key`, or `Authorization: Bearer`), created by the
  QC lead on Settings, shown once, kept as its SHA-256 only, revocable. The
  case is made exactly as an upload is (`_new_scan`). Working.
- *OCR (Tesseract)* (`documents/ocr.id_read`): the upload page has two
  buttons, *Read with AI* (the model chosen above them) and *Read with OCR*
  (Tesseract on this machine, free, enabled when it is installed). OCR's
  fields come from a passport's machine-readable zone only (a field whose
  check digit does not add up is marked unsure): on its own OCR cannot tell
  which printed line is the name or which date the birth date, so an ID
  without a zone gets no field and goes to an analyst. The page's text OCR
  read is shown on the case. OCR does not judge the photo, so its *Photo*
  check warns ("Not judged") and an OCR case is never auto-verified (Azure's
  read is marked the same way). Nothing else reads an OCR upload: when
  Tesseract fails the case says why. Model evaluation lists it as a reader.
- *Azure AI Document Intelligence* (`adapters/azure_docintel.py`): once its
  endpoint and key are on Settings, it can be chosen as the reader (on
  Settings, or per upload); its prebuilt ID-document read is mapped to Scan
  ID's, so the same checks run on it, and a field under the confidence
  floor is marked unsure. When Azure cannot read the ID the AI model does,
  and the case says so. *Test Azure* lists the resource's models. Written
  from Microsoft's REST reference (v4, 2024-11-30); not yet run against the
  service.
- *Didit ID verification* (`adapters/didit.py`): once its API key is on
  Settings, every uploaded ID (or the case's *Verify with Didit*) goes to
  Didit's ID Verification API in the background: Approved or Declined, the
  warnings (screen capture, printed copy, portrait changed, covered fields,
  MRZ, expiry), the photo quality, shown as the case's *ID vendor check*. A
  document Didit does not approve (or could not check) is never closed by
  auto-verify. Face match and liveness need a selfie and are not called.
  Written from Didit's API reference (`/v3/id-verification/`); not yet run
  against the service. A sandbox key answers with a fixed Approved sample
  ("Sandbox User", SANDBOX-0001) without looking at the image: the case says
  *Sandbox: not a real check*, shows it free, and nothing closes on it. Nor
  does an approval whose name, date of birth or document number differs from
  the ID's (`id_screening.idv_differences`; a middle name left out is the
  same name): the case lists the differences and stays with an analyst.
**Auto-verify** (`id_screening.auto_verify`; *Auto-verify* on Settings, on by
default). Once the AI has answered on every hit, the AI screening rule
closes the safe cases and leaves the rest for an analyst: a case with no
World-Check hit closes as *No match*; a hit is cleared as a false positive
when the AI said false positive at the confidence set (90% by default), the
record's date of birth differs from the ID's, and the record's category is
not one of the lists that always go to an analyst (Sanctions, PEP); the case
closes when every hit is cleared. Nothing is closed while an ID check warns
or fails. A share of the cases the rule would close (5% by default, fixed by
the case ID) is left for an analyst as a QC sample. All of it is on
Settings. The case list has *Needs an analyst*, *Auto-verified* and *Closed
by an analyst*; the case and the name-screening page say what the rule
cleared and why; Model evaluation counts the rule's clears apart and does
not score them (no analyst gave them an answer).
Saving a new SOP version (the QC lead) sends every open case's unconfirmed
hits to the model again under it. Settings (the QC lead) holds the models,
the examples per hit, the stand-in's name-score floor and the hits kept, and
the World-Check One account; the secret is never shown back. Download gives
a case as JSON; Delete removes it and its image (real IDs: `uploads/scans/`,
gitignored).

**Model evaluation** (`/model-evaluation`, Insights; `fincrime/upload_eval.py`).
The Screening cases flow's two models, measured on the IDs uploaded in the
console as they are read and decided: no sample data and no separate run;
the page is built from the cases each time. An upload has no answer key, so
the reading model is measured by what the code's checks find on its reads
(MRZ check digits adding up, printed fields agreeing with the zone, photos
it called spoiled, fields it was unsure of, seconds and cost a read), and
the deciding model against the analysts: each alert outcome given after the
AI's suggestion is the answer it is scored on (agreement, the hits it
suggested clearing that the analyst did not clear, a confusion matrix of
the analyst's outcome against its suggestion, seconds and cost a hit), and
the agreement under each SOP version. The models marked *in use* are
Settings'; choosing another reader on the upload form, or another decider
on Settings, adds its row. The client's 1,000 records are used only as the
stand-in World-Check. (`fincrime/decision_model.py` builds the decider's
instructions; its own evaluation of the old case facts, and
`fincrime/benchmark.py`'s of the old four-document read, still run from the
command line and are not shown.)

The console's names and colours are client-neutral: the two onboarding
lines are **Individuals** and **Businesses**, customer tiers are Tier 1–4,
the top tabs are Monitoring / Reports / Administration and the accent is a
petrol teal. Internally the tenants keep their old keys (`ttlive`, `ttshop`)
because behaviour hangs on them; `vocab.TENANTS` holds every label, and
records stored under the old names are relabelled on load
(`loader.relabel`, `vocab.RELABEL`).

Built the same way as the ChargeShield demo: one Flask `app.py`, Jinja
templates, a single stylesheet, a SQLite state store, no JS framework.

All people, companies, documents and screening records are fictional. The
identity documents are drawn as SVGs from the seed data and carry a
"SPECIMEN" watermark, except images an analyst uploads (above). Vendor
names appear as text labels only.

Links that open the World-Check console, from the in-house CMS case, its
Alert Outcome dialog and the Audit Log, open it in a new tab marked with a
small arrow-box icon, so the screening case stays where the analyst left it.
Every other link stays in the same tab.

## Run

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:8010 — sign in with any analyst profile and the demo
password shown on the card (`1234`). Always run through the project `.venv`:
the Anaconda Python on this machine has a broken `_sqlite3` and the plain
Python 3.13 has no Flask.

The startup line names the console's case book. Set `CONSOLE_CASES=file`
(in the environment or `.env`) for the reconciliation log's 1,000 cases, or
`CONSOLE_CASES=demo` for the scripted scenarios, instead of ID screening;
switching between those two clears the console's stored state (the screening
book has a database of its own).

## Deploy — a link anyone can open

**Permanent, free (Render).** Push the repo to GitHub, then in Render choose
*New → Blueprint*, connect the repo and create: `render.yaml` sets it up
(one gunicorn worker, debug off, a generated secret key). The free plan
sleeps after 15 minutes idle, so the first visit after a pause takes about
a minute, and its disk is wiped on every deploy or restart, which only
brings the seeded case book back. `Procfile` covers Railway and similar
hosts the same way. Keep the repository private: the demo copies the
client's internal screens.

**Right now, from this laptop.** Run a production copy (debug off, its own
state file) and open a Cloudflare quick tunnel to it. No account needed;
the link lives while both commands run, and changes on every restart.

```
set DEBUG=0 & set SECRET_KEY=<any long random string> & set FCC_STATE_DB=%TEMP%\fcc_public.db
.venv\Scripts\python.exe -m waitress --listen=127.0.0.1:8020 --threads=8 app:app
cloudflared tunnel --url http://127.0.0.1:8020
```

Either way the sign-in card shows the demo password. Set
`STRAIVE_DEMO_PASSWORD` (see `.env.example`) to hide it behind a password of
your own.

**Model assessment (a real model).** Set `LLM_FOUNDRY_KEY` (and, if
needed, `LLM_FOUNDRY_URL` and `LLM_MODEL`; see `.env.example`) and every
screening case gains an **AI assessment** button: Claude, reached through
Straive's LLM Foundry gateway, reads the client record and the watchlist
record side by side and writes a verdict, a confidence, its reasons and the
data it found missing on the ticked alerts (or every pending one), with an
audit row on each case. It is advisory: it resolves nothing, never runs at
intake, and the AI Screening page shows its status and a switch. On Render
the blueprint asks for the key; leave it blank to run without the feature.
The case data is fictional, so nothing sensitive leaves the demo, unless an
analyst uploads real document images (see **Real document images**).

## Tests

```
.venv\Scripts\python.exe -W error::ResourceWarning -m unittest -v
```

191 tests: `tests/test_upload.py` (the sample, an upload through intake, the
agent started, duplicates and a wrong file refused, uploads surviving a
restart), `tests/test_documents.py` (real document images on a case, all
made by Pillow in memory: the slot replaced, the photo turned upright and
its EXIF gone, served only to the case's tenant, read by the agent in
place of the drawing, a resubmission back in Data Confirming, bad files and
closed or held cases refused, kept through a restart, removed by the
reset; a CSV without the document columns waiting for its images, a CSV with a ZIP of them, the ZIP's checks), `tests/test_ocr.py` (the MRZ parser on the ICAO 9303 specimen, readings pooled, the agent's OCR check agreeing, differing, look-alike, unreadable or required, no Tesseract, and one real Tesseract read of a drawn passport, skipped without it; OCR as Scan ID's reader: the fields from the zone, an unsure field where a check digit fails, no field without a zone), `tests/test_sop.py` (the SOP rules: the shipped values read as before, only the team lead changes one, kept through a restart and undone by the reset, the checks and the SLA following a change at once, and a case waiting for a new proof of address verified by a longer limit), `tests/test_architecture.py` (the old document benchmark's scoring: the right read scores full marks, a misread and a missed spoiled photo cost marks, cost at list price), `tests/test_id_scan.py` (Scan ID: a good passport passes, an expired one, a misread number and a minor are caught, a miscounted MRZ filler run counted out, no OCR engine run beside the model, a document that is not an ID; the upload, result, image, JSON and delete pages with the model stood in for, a file that is not an image refused), `tests/test_screening.py` (screening an uploaded ID: the stand-in watchlist is the file's 1,000 records without the analysts' date notes, name scores across word order, diacritics, initials and middle names, the date of birth and country reported but never dropping a hit; the deciding model never shown the case behind the record hit, its answer kept to the four outcomes; SOP versions; confirming every hit closes the case; World-Check One's request signing and result mapping and a refusal said plainly; the screening book's empty start and menu, an upload to a closed case, no hit confirmed and closed, a new SOP version sending open hits to the model again, the settings keeping the secret out of the page and switching the search to World-Check One, the reset deleting the cases only, the Model evaluation and Architecture pages following the uploads and the analysts' outcomes, auto-verify and its rule, the intake API with its keys, Didit holding back a declined document, Azure reading with the AI model standing in, Read with AI or with OCR), `tests/test_services.py` (the Azure and Didit adapters: the requests they make, their answers read, their refusals said plainly), `tests/test_decision_model.py` (the decision model: the analysts' answers never shown to it, the fixed stratified split, examples from the training cases and never the case itself, the SOP's change note kept out of the prompt, the rules and the learned tree, the scoring and its confusion matrices), `tests/test_ai_agent.py` (the AI Agent with the model call stood
in for: the clean path to Verified, a held alert to Data Screening, every
reason a case goes to a person, an old proof of address asked for again and
what is never asked for, the record's slips corrected (the father's name, a
married surname on the form, an anglicised first name beside the
naturalisation certificate, a PO Box) and the RFIs (a second surname beside
the passport's among them), the bad ID photos and the suggested resubmit
reason, the filled-in Resubmit dialog, a redrawn case read again, a case
confirmed under older name or address rules, or asked for a certificate it
holds, decided again on its stored read, a postal code another document disputes, a gateway
error, a rate-limit or dropped-connection retry, the name rules, the worker
on an auto-verified case, Pause/Resume for the team lead),
`tests/test_console_book.py` (the console on the reconciliation
log: the book's shape, the rules at intake against what the file records,
the documents, Data Confirm on cleared, held, garbled-name, old-POA and
company cases, the paged lists, routing and search), `tests/test_filebook.py`
(the engine under the file book: the parsers, the name and address rules
on example rows from the log, the documents),
plus the console's 47 end-to-end tests on the Flask test client, run on the
scripted book: the golden path, resubmit,
escalate, segregation of duties, L2/EDD decisions, Skopenow, Businesses risk
recalculation, routing by skill and capacity, case on-demand, immediate
hand-offs, capacity overrides, trackers and CSV, the investigation-file ZIP,
the global audit log, QC sampling, Checker 2, role enforcement at EDD, POA
checks, fraud typology, World-Check links and the screening Details
button opening in a new tab, the document viewer's controls, the greyed Review button's reasons, refused
actions staying on the page with the error as a notification, the AI
screening rules (the rule book, what it did at intake, auto-verification on
Data Confirm, businesses keeping the human close, rule switches that persist),
the workflow labels across the screens, one outcome written to several
ticked alerts at once, the case list and screening queue filtered and paged
on the server, a routing run that passes over a full tier, a day/month-swapped
date of birth held for an analyst, and the model assessment (with the model
stood in for; the suite never calls the gateway).

## Analyst profiles

| Profile | Roles | Tenants | Skill book | Capacity |
|---|---|---|---|---|
| Amara Iyer | L1 | Individuals | latin, cyrillic | 6 |
| Jon Miranda | L1 | Individuals, Businesses | latin, arabic, KYB | 6 |
| Mario Kim | L1 + L2 | both | latin, KYB | 5 |
| Nisha Rao | L2 | both | latin, cyrillic, arabic, KYB | 4 |
| Ryan Lee | EDD (L3 / PIC) | both | all scripts, KYB | 3 |
| Leah Fernandez | QC / team lead | both | — (takes no cases) | 0 |

The header's analyst switcher jumps between profiles without signing out.
**Change Status** (moving a case by hand) is for L2 and QC; the moved case
still needs the stage's level to Review — Data Confirming is an L1 step —
and a greyed Review button says so in text beside it. Mario holds both L1
and L2.

## A demo script on the reconciliation log (the default book)

1. **Amara** → *Due Diligence*: 943 Individuals cases, 100 a page. The filters,
   the sort and the page run on the server and live in the URL; *Review
   workflow* = Human-in-the-loop leaves the 351 the rules could not clear.
2. *My Work* → **KYC-2026-3011** (Nguyen Ngoc Phuong). The header says
   *Auto-verified*: World-Check record WC-554473 lists a 1967 birth date
   against her 2001. Preview the passport, the CCCD and the CT07 residence
   confirmation: the file's printed names, MRZ line and addresses. The
   *Recorded Outcome* section at the foot shows what the file's analyst
   decided (False Positive - Cleared, M. Chen). Reserve → Review → OK → the
   case verifies itself, signed by System User.
3. **KYC-2026-3146**: the CRM captured the name garbled; the vendor read
   VŨ CÔNG KHÁNH off the MRZ. Correct the name in Data Confirm: the Change
   Log keeps the edit and the case verifies itself.
4. **KYC-2026-3012** (Upadhyay Ashish): an exact date of birth on an
   adverse-media record, so the rules hold it. Confirm → the screening case
   → World-Check record WC-675768 → resolve; the file recorded *True
   Positive - Application Rejected*.
5. **Jon**: **KYC-2026-3046**, a record listing the client's date with day
   and month swapped, is held (the file recorded 14 of its 22 such cases as
   true matches). **KYC-2026-3037**: the proof of address is 223 days old,
   so Data Confirm refuses it and Resubmit POA is the move. Tenant Businesses →
   **KYC-2026-3029** Ramasamy Agro Exports Pvt Ltd: the certificate of
   incorporation and the UBO's passport; a business goes through Checker 1
   and Checker 2.
6. **Mario**: **KYC-2026-3118** is a flat scan the vendor declined (Resubmit
   ID); **KYC-2026-3040**'s passport fails its MRZ check digit (escalate as a
   fraud ID); **KYC-2026-3043** lists a year of birth only and is held.
7. *AI Agent*: a few minutes after the server starts, **KYC-2026-3011** is
   Verified by itself; its page shows the AI Agent card with every check
   passed. A bad photo of the passport stays in Data Confirming with the
   reasons and a suggested Resubmit ID reason: **KYC-2026-3088** has a phone
   lying on it, **KYC-2026-3027** is cracked and worn, **KYC-2026-3832** is
   shot at an angle, **KYC-2026-3217** is cut off, **KYC-2026-3198** is
   blurred. *AI Screening* shows the agent's
   progress; *Due Diligence* → filter *AI Agent* shows what it handled.
8. *AI Screening* → *Against the reconciliation log*: 592 cleared by the
   rules, 592 recorded as false positives; the 351 left for analysts hold
   every true match.
9. **Nisha** → *Routing*: 935 unassigned, the 100 most urgent with the
   router's pick; **Route selected now** fills the free slots and stops.
10. **Reset demo** reloads the file book.

The document images are drawn from the file's data (it carries none); the
capture defects on about 9% of cases are planted, and the case page says
so. They are synthetic drawings of the kinds of failure real
uploads show; no real customer's document is in the repository.

## A demo script on the scripted book (`CONSOLE_CASES=demo`)

0. The case list's filter panel folds behind the arrow at its top right and
   stays folded through live refreshes; the buttons row keeps working and a
   folded panel says how many filters are active.
1. **Amara** → *Operations Hub › My Work* → **Get next case**: the router picks
   the most urgent case her skill book allows and opens it, reserved. The
   header shows the routing chip and the reason.
2. Open **Dmitri Volkov** (Russian passport, Russian proof of address) →
   *Review*: the Data Confirm modal shows the POA checks; two fail, OK stays
   off, **Escalate** preselects the sanctioned-country reason. Escalate with
   a fraud typology → the case is handed to L2 at once; the flash names who.
   The document panel here, and the **Preview** dialog on the case page,
   zoom with the mouse wheel, pan by drag, and have zoom, fit and rotate
   buttons.
3. Open **Michael James Taylor** for the auto-verified workflow: the header
   already says *Auto-verified* — the AI screening rules cleared his two
   namesakes at intake (a 23-year date-of-birth gap, a female record) and
   the screening case closed itself. Reserve → confirm the three fields →
   the case is Verified with no analyst step, signed by System User, the
   export attached. Then open **Daniel Mercer** for the human-in-the-loop
   golden path: six of seven records cleared by the rules, one record with
   no listed date of birth left for Amara → Reserve → confirm → screening
   case → World-Check resolve + export → alert and case outcome → EDD
   Review → Verified.
4. **Nisha** → *My Work* shows the hand-off; *Pipeline* shows the L2 column
   and the hand-off feed with lag 0 s against the bot-era history.
5. **Jon**, tenant Businesses → a business in Data Screening → Recalculate Risk
   Rating → EDD Review "No match - Approve" → the business waits for
   **Checker 2**; Jon cannot sign it off. **Nisha** → L2 / EDD Queue → the
   Checker 2 row → Review → Approve → Verified.
6. *Trackers*: the Productivity / Time, Fraud and EDD / Businesses Escalation
   trackers, filled by the system, with CSV. Open an escalated case's
   **Investigation File** and download the ZIP.
7. **Leah** → *QC Sampling* → fail one sampled case with a reason → the
   closing analyst's My Work shows the feedback to acknowledge. *Routing*:
   lower an L2's capacity to 1, escalate another case, watch it wait for
   capacity, raise it, **Route selected now**. *AI Screening*: switch the
   nationality rule on, then confirm Mercer's data as Amara — the seventh
   record clears and the case auto-verifies; the change sits in the history.
8. *Analytics*: tier pipeline, backlog ageing, shift progress, hand-off lag,
   quality scorecard, auto-verified vs human-in-the-loop, rules fired.
   *Audit Log*: one stream across every record; the rules sign their own.
9. **Reset demo** in the header brings the seeded case book back.

## Layout

| Path | What lives there |
|---|---|
| `app.py` | Flask app: sign-in, tenant switch, thin routes, state glue, the Operations Hub routes |
| `fincrime/data/` | vocabularies, seed builders, the 19 scenarios, the reconciliation-log book (`recon_book.py`), loader, SQLite state store |
| `fincrime/engines/` | KYC workflow state machine, AI screening rules, routing (skill book, capacity, hand-offs), work log, name-screening, World-Check logic, SLA, segregation, QC sampling, investigation file, PDF export |
| `fincrime/documents/` | synthetic ID / proof-of-address SVG cards |
| `fincrime/analytics/` | view-model builders: case list, queues, My Work, pipeline, trackers, audit log, routing console, AI screening, dashboard |
| `fincrime/adapters/` | mock IDV vendor, in-house CMS and Skopenow |
| `templates/` | flat Jinja templates; `_name.html` are partials/macros |
| `static/css/style.css` | the one stylesheet (light console + dark `body.wc-theme`) |
| `tests/` | end-to-end flow tests on the Flask test client |

## How the hub maps to the SOP

| SOP | Where |
|---|---|
| §2.1 QC sampling, scorecards, coaching | QC Sampling page, scorecard on Analytics, feedback on My Work |
| §3.1 SLAs 48 h / 14 d | SLA chips, breach KPIs, pipeline flags |
| §4.1.2 Data Confirming, POA checks, resubmit reasons | Data Confirm modal |
| §4.1.3 screening, WC export, Skopenow, escalation, fraud typology, EDD tracker, investigation folder | screening case, World-Check console, EDD Review, Escalate dialog, Trackers, Investigation File |
| §5.1 current automation: rule-based disposition of screening hits; the auto-verified and human-in-the-loop workflows | AI screening rules at intake and at the re-screen, auto-verification on Data Confirm, AI Screening page, workflow badge and filter |
| §4.2 Businesses Checker 1 / Checker 2, risk rating, Pending EDD | Agency panel, EDD Review, Checker 2 sign-off, L2 / EDD queue |
| §5.2.1 automated routing, no spreadsheets | routing engine on every hand-off, Routing console, Trackers |
| §5.2.2 dynamic capacity, case on-demand | My Work (Get next case / Request cases), capacity per analyst |
| §5.2.3 L1 → L2 → L3, real-time pipeline, no hand-off lag | Pipeline page, hand-off feed, lag metric |
| §5.2.4 live dashboards, audit logs | Analytics, Audit Log, live refresh on every page |

The state store keeps every action across restarts; a change to the seed's
shape bumps `SEED_VERSION` and clears it. Uploads are never stored — only
the file name is echoed into the audit trail.
