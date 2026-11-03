"""The watchlist case export the EDD Review attaches — a real PDF built with
reportlab, the same way the ChargeShield demo renders its evidence packet.

reportlab ships only the Vera fonts, which have no Cyrillic, so Arial is
registered from the Windows font folder when it is there; otherwise
Helvetica is used and any non-Latin text is transliterated.
"""
import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from fincrime.data import vocab
from fincrime.utils import dates
from fincrime.utils.text import translit

_FONT = {"regular": "Helvetica", "bold": "Helvetica-Bold", "unicode": False}


def _register_fonts():
    if _FONT.get("done"):
        return
    _FONT["done"] = True
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        reg, bold = os.path.join(fonts, "arial.ttf"), os.path.join(fonts, "arialbd.ttf")
        if os.path.exists(reg):
            pdfmetrics.registerFont(TTFont("DemoSans", reg))
            _FONT["regular"] = "DemoSans"
            _FONT["unicode"] = True
            if os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("DemoSans-Bold", bold))
                _FONT["bold"] = "DemoSans-Bold"
            else:
                _FONT["bold"] = "DemoSans"
    except Exception:  # pragma: no cover — fall back to Helvetica
        pass


def _t(text):
    text = str(text if text is not None else "")
    if not _FONT["unicode"]:
        text = translit(text)
    return escape(text)


def render_wc_export(wc, alert=None, kyc=None, exported_by=""):
    _register_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Watchlist case export - {wc['primary_name']}", author="FinCrime Operations Hub (demo)")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontName=_FONT["regular"], fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=10, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=body, fontName=_FONT["bold"], fontSize=15, leading=19, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=body, fontName=_FONT["bold"], fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=3,
                        textColor=colors.HexColor("#1d4ed8"))
    grid = TableStyle([("FONTNAME", (0, 0), (-1, -1), _FONT["regular"]), ("FONTSIZE", (0, 0), (-1, -1), 8),
                       ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9ced6")),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef8")),
                       ("FONTNAME", (0, 0), (-1, 0), _FONT["bold"]), ("VALIGN", (0, 0), (-1, -1), "TOP")])

    story = [Paragraph(_t("Watchlist screening case export"), h1),
             Paragraph(_t("Simulated World-Check console · FinCrime Operations Hub demo · every record is fictional"), small),
             Spacer(1, 6)]
    cd = wc["case_details"]
    head = [["Primary name", wc["primary_name"], "Case ID", wc["id"]],
            ["Gender", wc["gender"], "Date of birth", wc.get("dob", "")],
            ["Group", wc["group"], "Assignee", wc["assignee"]],
            ["Business line", cd.get("BusinessLine", ""), "Event", cd.get("EventName", "")],
            ["KYC status", cd.get("KYCStatus", ""), "User ID", cd.get("UserID", "")],
            ["Exported", dates.now_str(), "Exported by", exported_by]]
    t = Table([[Paragraph(_t(c), body) for c in row] for row in head], colWidths=[28 * mm, 62 * mm, 28 * mm, 60 * mm])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), _FONT["regular"]), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9ced6")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f4f7")), ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f2f4f7"))]))
    story += [t, Paragraph(_t("Matches"), h2)]

    f = {}
    for m in wc["matches"]:
        f[m["resolution"]["status"]] = f.get(m["resolution"]["status"], 0) + 1
    story.append(Paragraph(_t(" · ".join(f"{k}: {v}" for k, v in f.items())), body))
    rows = [["#", "Name", "Types", "Strength", "DOB", "Citizenship", "Resolution", "Risk", "Reason", "By"]]
    for i, m in enumerate(wc["matches"], start=1):
        r = m["resolution"]
        rows.append([str(i), m["name"], " ".join(m["types"]), f"{int(m['strength'] * 100)}%", ", ".join(m["dobs"]),
                     m["citizenship"], r["status"], r["risk"], r["reason"], r["by"]])
    if len(rows) > 61:
        rows = rows[:61] + [["…", f"{len(wc['matches']) - 60} further auto-resolved matches omitted", "", "", "", "", "", "", "", ""]]
    t = Table([[Paragraph(_t(c), small) for c in row] for row in rows],
              colWidths=[7 * mm, 34 * mm, 14 * mm, 13 * mm, 20 * mm, 22 * mm, 20 * mm, 12 * mm, 22 * mm, 14 * mm], repeatRows=1)
    t.setStyle(grid)
    story.append(t)

    for m in wc["matches"]:
        if m["resolution"]["by"] == vocab.SYSTEM_USER:
            continue
        kd, fi = m["key_data"], m["further_info"]
        story += [Paragraph(_t(f"Record {m['wc_id']} — {m['name']}"), h2)]
        detail = [["Category", kd.get("category", "")], ["Sub-category", kd.get("sub_category", "")],
                  ["PEP status", kd.get("pep_status", "")], ["Special interest", ", ".join(kd.get("special_interest", []))],
                  ["Position", kd.get("position", "")], ["Location", "; ".join(", ".join(v for v in loc.values() if v) for loc in kd.get("locations") or [kd.get("location", {})])],
                  ["Biography", fi.get("biography", "")], ["Identification", fi.get("identification", "")],
                  ["Reports", " | ".join(f"{d}: {x}" for d, x in fi.get("reports", []))],
                  ["Sources", " | ".join(u for _d, u in m.get("sources", []))],
                  ["Resolution", f"{m['resolution']['status']} / {m['resolution']['risk']} / {m['resolution']['reason']} — {m['resolution']['comment']} ({m['resolution']['by']}, {m['resolution']['at']})"]]
        t = Table([[Paragraph(_t(a), body), Paragraph(_t(b), body)] for a, b in detail], colWidths=[30 * mm, 148 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c9ced6")), ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f4f7")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(t)

    if alert and alert.get("case_outcome"):
        co = alert["case_outcome"]
        story += [Paragraph(_t("Screening case outcome"), h2), Paragraph(_t(f"{co['value']} — {co['comment']} ({co['by']}, {co['at']})"), body)]
    story += [Spacer(1, 10), Paragraph(_t("This export was generated by a demonstration system. The records above are synthetic and "
                                          "do not describe any real person or organisation."), small)]
    doc.build(story)
    return buf.getvalue()
