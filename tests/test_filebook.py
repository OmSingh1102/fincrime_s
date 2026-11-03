"""The file book's engine under the console: the book, the parsers, the
normalisation rules and the documents. No Flask here.

Run:  .venv\\Scripts\\python.exe -m unittest -v tests.test_filebook
"""
import copy
import html
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fincrime.engines.errors import WorkflowError  # noqa: E402
from fincrime.filebook import addresses, documents, loader, names, parse, similarity  # noqa: E402

_BOOK = None


def book():
    """One parsed, synthesised book shared by the tests (deep-copied where mutated), each case with
    the canonical name the console's recon_book.view works out for its proof of address."""
    global _BOOK
    if _BOOK is None:
        cases = loader.load_cases()
        for c in cases:
            documents.synthesise(c)
            pp = c["documents"]["passport"]
            c["identity"] = {"name": names.analyse(c["country"], c["customer"]["name"], pp["viz"], pp["mrz"],
                                                   c["documents"]["secondary"]["name"], c["customer"]["corporate"],
                                                   c["customer"]["ubo"])}
        _BOOK = cases
    return _BOOK


def by_id(cid, fresh=False):
    c = next(c for c in book() if c["id"] == cid)
    return copy.deepcopy(c) if fresh else c


def bad_check_digits(line):
    """Which of an MRZ line 2's check digits do not hold."""
    cd = documents._check_digit
    return [name for name, field, digit in (("document number", line[0:9], line[9]), ("date of birth", line[13:19], line[19]),
                                            ("expiry", line[21:27], line[27])) if cd(field) != digit]


def poa_drawn(c):
    """When the case's proof of address was last redrawn, "" when never."""
    if c["documents"]["poa"]["kind"] == "aadhaar":
        return documents.AADHAAR_NAMED
    return documents.POA_REDRAWN if documents.poa_wraps(c) else ""


class LoaderTests(unittest.TestCase):

    def test_book_loads_every_case(self):
        cases = book()
        self.assertEqual(len(cases), 1000)
        self.assertEqual(len({c["id"] for c in cases}), 1000)
        self.assertEqual(cases[0]["id"], "KYC-2026-3011")
        self.assertEqual(cases[-1]["id"], "KYC-2026-4010")
        countries = {k: sum(1 for c in cases if c["country"] == k) for k in ("IN", "VN", "US")}
        self.assertEqual(countries, {"IN": 340, "VN": 330, "US": 330})
        kinds = {}
        for c in cases:
            kinds[c["wc"]["dob"]["kind"]] = kinds.get(c["wc"]["dob"]["kind"], 0) + 1
        self.assertEqual(kinds, {"full": 758, "year": 162, "none": 41, "range": 39})
        self.assertEqual(sum(c["customer"]["corporate"] for c in cases), 57)
        self.assertEqual(sum(c["customer"]["nri"] for c in cases), 31)

    def test_headers_with_en_dashes_are_found(self):
        self.assertEqual(parse.header_key("Name – Passport Data Page (VIZ)"), "name_passport_data_page_viz")
        self.assertEqual(parse.header_key("Name – Passport MRZ (Line 1)"), "name_passport_mrz_line_1")
        row = loader.read_rows()[0]
        for key in loader.REQUIRED:
            self.assertIn(key, row)

    def test_missing_column_fails_loudly(self):
        path = os.path.join(tempfile.mkdtemp(prefix="filebook-test-"), "bad.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("KYC Case ID,Customer Name\nKYC-1,X\n")
        with self.assertRaises(ValueError):
            loader.read_rows(path)

class ParserTests(unittest.TestCase):

    def test_viz_both_shapes_and_blank_surname(self):
        self.assertEqual(parse.viz("Surname: UPADHYAY | Given Name(s): ASHISH")["surname"], "UPADHYAY")
        v = parse.viz("Surname: VŨ | Given Names: CÔNG KHÁNH")
        self.assertEqual((v["surname"], v["given"]), ("VŨ", "CÔNG KHÁNH"))
        v = parse.viz("Surname: (blank) | Given Name(s): PRABHU")
        self.assertTrue(v["blank_surname"])
        self.assertEqual(v["given"], "PRABHU")

    def test_mrz_line_one(self):
        m = parse.mrz1("P<IND<<PRABHU<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<")
        self.assertTrue(m["mononym"])
        self.assertEqual(m["secondary"], ["PRABHU"])
        m = parse.mrz1("P<USAMENDOZA<MORALES<<SOFIA<TERESA<<<<<<<<<<")
        self.assertEqual(m["primary_tokens"], ["MENDOZA", "MORALES"])
        self.assertEqual(m["secondary"], ["SOFIA", "TERESA"])
        self.assertEqual(m["issuer"], "USA")
        self.assertFalse(m["truncated"])

    def test_world_check_name_shapes(self):
        n = parse.wc_name("QURESHI, Farheen (AKA: FARHEEN KURESHI)")
        self.assertEqual((n["surname"], n["given"], n["aka"]), ("QURESHI", "Farheen", ["FARHEEN KURESHI"]))
        n = parse.wc_name("SANGEETHA (single name) (AKA: SANGEETHA K)")
        self.assertTrue(n["mononym"])
        self.assertEqual(n["surname"], "SANGEETHA")
        self.assertEqual(n["aka"], ["SANGEETHA K"])
        n = parse.wc_name("NGUYEN, Thanh Ha (Nguyễn Thanh Hạ)")
        self.assertEqual(n["native"], "Nguyễn Thanh Hạ")

    def test_world_check_dob_seven_shapes(self):
        self.assertEqual(parse.wc_dob("1962-06-29 (Exact Match)")["kind"], "full")
        self.assertEqual(parse.wc_dob("1985-06-09 (Day/Month transposed)")["date"], "1985-06-09")
        self.assertEqual(parse.wc_dob("2000-09-17 (34 yrs diff)")["year"], 2000)
        self.assertEqual(parse.wc_dob("1961 (Year only)")["kind"], "year")
        self.assertEqual(parse.wc_dob("2001 (Year only, 13 yrs diff)")["year"], 2001)
        r = parse.wc_dob("1987–1989 (Approx. range)")
        self.assertEqual((r["kind"], r["year"], r["year_to"]), ("range", 1987, 1989))
        self.assertEqual(parse.wc_dob("Not listed")["kind"], "none")

    def test_customer_type_and_addresses(self):
        t = parse.customer_type("Corporate (UBO: Vignesh R)")
        self.assertTrue(t["corporate"])
        self.assertEqual(t["ubo"], "Vignesh R")
        self.assertTrue(parse.customer_type("Individual (NRI)")["nri"])
        self.assertEqual(parse.customer_dob("UBO DOB: 1965-07-20"), "1965-07-20")
        a = parse.doc_address("Not printed on US passport (Place of birth: ILLINOIS, U.S.A.)")
        self.assertFalse(a["printed"])
        self.assertEqual(a["pob"], "ILLINOIS, U.S.A.")
        self.assertTrue(parse.doc_address("FLAT NO 202 B WING, BIHAR, INDIA")["printed"])
        self.assertEqual(parse.secondary_type("Driver's License (TX)")["us_state"], "TX")
        loc = parse.wc_location("Hai Phong (formerly Hai Duong), Vietnam")
        self.assertEqual((loc["city"], loc["former"], loc["country"]), ("Hai Phong", "Hai Duong", "Vietnam"))
        self.assertEqual(parse.wc_location("Seattle, WA, USA; Bellevue, WA, USA")["region"], "WA")
        # a record lists each place linked to the person: the first stays the main one
        loc = parse.wc_location("Singapore; Dhaka, Bangladesh")
        self.assertEqual(loc["country"], "Singapore")
        self.assertEqual([(p["city"], p["country"]) for p in loc["places"]], [("", "Singapore"), ("Dhaka", "Bangladesh")])

class NameTests(unittest.TestCase):

    def analyse(self, cid):
        c = by_id(cid)
        p = c["documents"]["passport"]
        return names.analyse(c["country"], c["customer"]["name"], p["viz"], p["mrz"], c["documents"]["secondary"]["name"],
                             c["customer"]["corporate"], c["customer"]["ubo"])

    def test_the_holders_sex_from_the_given_names(self):
        v = names.sex_from_given
        self.assertEqual((v("JENNIFER KAY", "US"), v("RAJESH KANTILAL", "IN"), v("KIRAN", "IN")), ("F", "M", ""))
        self.assertEqual((v("THỊ THU TRANG", "VN"), v("VĂN HIẾU", "VN"), v("QUỐC BẢO", "VN")), ("F", "M", "M"))
        self.assertEqual((v("MINH HOA", "VN"), v("THANH AN", "VN")), ("F", ""))       # the personal name first; both-sex names stay open
        self.assertEqual((v("NAGA SAI SRI LAKSHMI", "IN"), v("VENKATA NAGA SRINIVAS", "IN")), ("F", "M"))   # past the Telugu prefixes

    def test_mojibake_repair(self):
        self.assertEqual(names.repair_mojibake("VÅ© CÃ´ng KhÃ¡nh"), ("Vũ Công Khánh", True))
        self.assertEqual(names.repair_mojibake("Upadhyay Ashish"), ("Upadhyay Ashish", None))
        text, ok = names.repair_mojibake("Pháº¡m Ngá»c Tuáº¥n")
        self.assertFalse(ok)
        # the bytes the text lost on the way come back as one marker each
        self.assertEqual(names.mojibake_lost("Phan Ngá»c Äáº¡t"), "Phan Ng\ufffdc \ufffdạt")
        self.assertEqual(names.mojibake_lost("VÅ© CÃ´ng KhÃ¡nh"), "")                          # nothing lost
        self.assertEqual(similarity.fold("Ðinh Thị Khánh Linh"), "DINH THI KHANH LINH")

    def test_examples_from_the_log(self):
        expectations = {
            "KYC-2026-3012": ("ASHISH UPADHYAY", "Given name / surname fields swapped"),
            "KYC-2026-3146": ("VŨ CÔNG KHÁNH", "Diacritics stripped / encoding corruption"),
            "KYC-2026-3015": ("JEFFREY DEAN WILLIAMS", "Nickname / preferred name in CRM"),
            "KYC-2026-3034": ("JACOB HENRY MILLER III", "Generational suffix (Jr./Sr./III)"),
            "KYC-2026-3028": ("ĐINH THỊ KHÁNH LINH", "Đ (D-stroke) transliteration"),
            "KYC-2026-3117": ("PRABHU", "Mononym – blank surname on passport"),
            "KYC-2026-3065": ("MOHAMMED RAFIQ PATHAN", "Honorific or abbreviation in name"),
            "KYC-2026-3058": ("SOFIA TERESA MENDOZA MORALES", "Compound / hyphenated surname"),
            "KYC-2026-3032": ("BALAJI KRISHNAN", "Patronymic initial vs expanded surname"),
            "KYC-2026-3017": ("NGUYỄN VĂN MINH KHANG", "Middle name dropped / compressed"),
            "KYC-2026-3069": ("PATRICIA MARIE ROBERTS", "Married vs maiden surname"),
            "KYC-2026-3021": ("FARHEEN QURESHI", "Transliteration / spelling variant"),
            "KYC-2026-3052": ("MANASI PRAKASH MORE", "Father's name as middle name"),
            "KYC-2026-3025": ("KHOA QUOC NGUYEN", "Naturalised citizen – reordered / anglicised name"),
            "KYC-2026-3023": ("NGUYỄN KHÁNH LINH", "Western married surname on form"),
            "KYC-2026-3018": ("TRẦN THỊ NGỌC HÂN", "Family-name-first order reversed"),
            "KYC-2026-3031": ("BRANDON SCOTT VAN BUREN", "Apostrophe / prefix surname tokenisation"),
        }
        for cid, (canonical, label) in expectations.items():
            r = self.analyse(cid)
            self.assertEqual(r["canonical"], canonical, cid)
            self.assertIn(label, r["variance_detected"], cid)

    def test_both_surnames_are_screened_for_a_married_name(self):
        r = self.analyse("KYC-2026-3069")
        self.assertEqual(r["screen_surnames"], [["ROBERTS"], ["WILSON"]])
        self.assertIn("surname_conflict", r["flags"])
        self.assertEqual(len([f for f in r["flags"] if f == "surname_conflict"]), 1)

    def test_corporate_case_screens_the_ubo(self):
        r = self.analyse("KYC-2026-3029")
        self.assertEqual(r["canonical"], "VIGNESH RAMASAMY")
        self.assertIn("corporate_ubo", r["flags"])
        self.assertTrue(any(a["source"] == "CRM (entity)" for a in r["aka"]))

    def test_variance_detection_agrees_with_most_historical_labels(self):
        hit = tot = 0
        for c in book():
            h = c["history"]["name_variance"]
            if h.startswith("None"):
                continue
            tot += 1
            if h in c["identity"]["name"]["variance_detected"]:
                hit += 1
        self.assertGreaterEqual(hit / tot, 0.9)

    def test_similarity_helpers(self):
        self.assertGreaterEqual(similarity.jaro_winkler("CHAUDHARY", "CHOUDHARY"), 0.9)
        self.assertEqual(similarity.phonetic_key("KURESHI"), similarity.phonetic_key("QURESHI"))
        self.assertEqual(names.surname_score(["VAN", "BUREN"], ["VANBUREN"]), 1.0)
        self.assertEqual(names.surname_score(["MENDOZA", "MORALES"], ["MENDOZA"]), 0.8)
        self.assertEqual(names.given_match("JEFF", "JEFFREY"), 0.95)
        self.assertEqual(names.given_match("K", "KRISHNAN"), 0.6)


class AddressTests(unittest.TestCase):

    def test_usps_and_zip(self):
        a = addresses.normalise("1328 Barton Springs Rd #6E, Austin, TX 78701", "US")
        b = addresses.normalise("1328 Barton Springs Road, Apartment 6E, Austin, TX 78701-3170", "US")
        self.assertEqual(addresses.compare(a, b), "same")
        self.assertIn("zip4_trimmed", b["flags"])
        self.assertEqual((a["unit"], a["house"], a["city_key"]), ("6E", "1328", "AUSTIN"))

    def test_po_box_and_military(self):
        a = addresses.normalise("PO Box 1445, Dallas, TX 75206", "US")
        self.assertTrue(a["po_box"])
        self.assertTrue(addresses.normalise("Unit 2050 Box 4190, APO AP 96278", "US")["military"])
        # an APO/FPO address has no street: the same unit or PSC and box is the same home, another box is not
        for apo in ("Unit 2050 Box 4190, APO AP 96278", "PSC 473 Box 51, FPO AP 96349"):
            self.assertEqual(addresses.compare(addresses.normalise(apo, "US"),
                                               addresses.normalise(apo + ", United States", "US")), "same")
        self.assertNotEqual(addresses.compare(addresses.normalise("Unit 2050 Box 4190, APO AP 96278", "US"),
                                              addresses.normalise("Unit 2050 Box 4191, APO AP 96278", "US")), "same")
        # the ZIP ends the address, before the country: a five-digit unit or house number is not it
        self.assertEqual(addresses.normalise("Unit 15324 Box 77, APO AP 96205, United States", "US")["postal5"], "96205")
        self.assertEqual(addresses.normalise("12345 Main St, Austin, TX 78701, USA", "US")["postal5"], "78701")

    def test_a_trailing_country_is_neither_city_nor_street(self):
        a = addresses.normalise("6246 East Palmetto Park Road, Boca Raton, FL 33432, United States", "US")
        self.assertEqual((a["city"], a["street"]), ("BOCA RATON", ["PALMETTO", "PARK"]))
        # so a neighbourhood still resolves to its USPS city when the country follows
        a = addresses.normalise("294 Main St Unit 590, Queens, NY 11355", "US")
        b = addresses.normalise("294 Main Street, Unit 590, Flushing, NY 11355, United States", "US")
        self.assertEqual(addresses.compare(a, b), "same")

    def test_neighbourhood_legacy_and_district_names(self):
        a = addresses.normalise("525 N 45th St, Ballard, WA 98103", "US")
        b = addresses.normalise("525 North 45th Street, Seattle, WA 98103", "US")
        self.assertEqual(addresses.compare(a, b), "same")
        self.assertIn("neighbourhood_alias", a["flags"])
        a = addresses.normalise("House No. 14, Usman Road, T. Nagar, Madras, Tamil Nadu 600017", "IN")
        b = addresses.normalise("S/O: Krishnan, House No. 14, Usman Road, T. Nagar, Chennai, Tamil Nadu - 600017", "IN")
        self.assertEqual(addresses.compare(a, b), "same")
        self.assertIn("legacy_city", a["flags"])
        self.assertIn("co_prefix_stripped", b["flags"])
        a = addresses.normalise("House No. 14, Sector 62 Road, Sector 62, Noida, Uttar Pradesh 201309", "IN")
        b = addresses.normalise("C/O: Mohammed Hussain, House No. 14, Sector 62 Road, Sector 62, Gautam Buddha Nagar, Uttar Pradesh - 201309", "IN")
        self.assertEqual(addresses.compare(a, b), "same")

    def test_pin_transposition_is_near(self):
        a = addresses.normalise("Flat No. 502, Shree Ganesh Apartments, Palm Beach Road, Vashi, Navi Mumbai, Maharashtra 400730", "IN")
        b = addresses.normalise("Flat 502, Shree Ganesh Apartments, Palm Beach Road, Vashi, Navi Mumbai - 400703", "IN")
        self.assertEqual(addresses._postal_state(a, b), "near")
        self.assertEqual(addresses.compare(a, b), "same")

    def test_vietnamese_abbreviations_alleys_and_mergers(self):
        a = addresses.normalise("174 Dien Bien Phu, Phuong 25, Quan Binh Thanh, TP. Ho Chi Minh", "VN")
        b = addresses.normalise("174 Điện Biên Phủ, Phường 25, TP. Hồ Chí Minh", "VN")
        self.assertEqual(addresses.compare(a, b), "same")
        self.assertIn("pre2025_unit", a["flags"])
        a = addresses.normalise("So 12, Hem 35/41, Duong Hai Ba Trung, Phuong Tan An, Quan Ninh Kieu, TP. Can Tho", "VN")
        b = addresses.normalise("35/41/12 Hai Bà Trưng, Phường Tân An, Quận Ninh Kiều, TP. Cần Thơ", "VN")
        self.assertEqual(addresses.compare(a, b), "same")
        a = addresses.normalise("14 Nguyen Van Tiet, Phuong Lai Thieu, TP. Thuan An, Tinh Binh Duong", "VN")
        b = addresses.normalise("14 Nguyễn Văn Tiết, Phường Lái Thiêu, TP. Hồ Chí Minh", "VN")
        self.assertEqual(addresses.compare(a, b), "same")
        a = addresses.normalise("261 Dien Bien Phu Street, Ward 25, Binh Thanh District, Ho Chi Minh City", "VN")
        b = addresses.normalise("261 Điện Biên Phủ, P.25, Q. Bình Thạnh, TP.HCM", "VN")
        self.assertEqual(addresses.compare(a, b), "same")

    def test_documents_explain_a_disagreement(self):
        r = addresses.analyse(by_id("KYC-2026-3013"))
        self.assertTrue(r["resolved"])
        self.assertIn("stale_dl", r["explained"])
        self.assertFalse(r["conflict"])
        r = addresses.analyse(by_id("KYC-2026-3018"))
        self.assertIn("temporary_residence", r["explained"])
        r = addresses.analyse(by_id("KYC-2026-3021"))
        self.assertIn("overseas", r["explained"])
        r = addresses.analyse(by_id("KYC-2026-3034"))
        self.assertIn("po_box_registered", r["flags"])

    def test_most_of_the_book_reconciles(self):
        resolved = sum(1 for c in book() if addresses.analyse(c)["resolved"])
        self.assertGreaterEqual(resolved, 950)


class DocumentTests(unittest.TestCase):

    def test_defects_are_deterministic_and_rare(self):
        cases = book()
        again = loader.load_cases()
        for c in again:
            documents.synthesise(c)
        self.assertEqual([c["synth"] for c in cases], [c["synth"] for c in again])
        capture = sum(1 for c in cases if c["synth"]["capture"] != "none")
        self.assertTrue(50 <= capture <= 110, capture)
        self.assertTrue(10 <= sum(1 for c in cases if c["synth"]["tampered"]) <= 35)
        self.assertTrue(all(len(c["files"]) == 4 for c in cases))

    def test_check_digits_fail_only_when_tampered(self):
        clean = next(c for c in book() if not c["synth"]["tampered"])
        tampered = next(c for c in book() if c["synth"]["tampered"])
        self.assertEqual(bad_check_digits(documents.mrz_line2(clean)), [])
        line = documents.mrz_line2(tampered)
        self.assertEqual(bad_check_digits(line), ["date of birth"])
        self.assertEqual(len(line), 44)

    def test_renders_every_upload(self):
        c = by_id("KYC-2026-3146")
        for f in c["files"]:
            svg = documents.render(c, f["id"])
            self.assertTrue(svg.startswith("<svg"), f["id"])
            self.assertIn("SPECIMEN", svg)
        svg = documents.render(c, "passport")
        self.assertIn("P&lt;VNMVU&lt;&lt;CONG&lt;KHANH", svg)
        self.assertIn("CÔNG KHÁNH", svg)
        flat = next(c for c in book() if c["synth"]["capture"] == "flat_scan")
        self.assertIn('feColorMatrix type="saturate"', documents.render(flat, "passport"))
        self.assertIn('data-capture="flat_scan"', documents.render(flat, "passport"))
        self.assertIsNone(documents.render(c, "nope"))

    def test_photo_defects_are_drawn(self):
        # the failures real uploads show: something on the page, a shot at an
        # angle, a cracked card, cut off, blurred - each drawn as a phone
        # photo on a cloth and stamped with the drawing date
        for capture in ("obstructed", "angled", "damaged", "partial", "blur"):
            c = next(c for c in book() if c["synth"]["capture"] == capture)
            svg = documents.render(c, "passport")
            self.assertIn(f'data-defect="{capture}"', svg)
            self.assertIn('fill="url(#cloth)"', svg)
            # the latest redraw of its images: this photo, or its proof of address since
            self.assertEqual(c["synth"]["drawn"], max(documents.REDRAWN, poa_drawn(c)))
        clean = next(c for c in book() if c["synth"]["capture"] == "none" and not c["synth"]["tampered"])
        self.assertEqual(clean["synth"]["drawn"], poa_drawn(clean))
        self.assertIn('viewBox="-28 -28', documents.render(clean, "passport"))        # all four corners in shot
        self.assertNotIn("data-defect", documents.render(clean, "passport"))

    def test_a_long_address_wraps_clear_of_the_statement_date(self):
        # a long proof-of-address address used to run over a statement's date
        # (and off the edge of an Aadhaar card); it wraps in its own column now,
        # and the case is stamped so a read of the old drawing is redone
        def lines(svg, x):
            return [html.unescape(t) for t in re.findall(rf'<text x="{x}" y="[\d.]+"[^>]*font-weight="600"[^>]*>([^<]*)</text>', svg)]

        bills = [c for c in book() if c["documents"]["poa"]["kind"] != "aadhaar"]
        long = next(c for c in bills if documents.poa_wraps(c))
        address = documents.svg_cards.service_address(long["documents"]["poa"]["address"], long["country"])
        drawn = lines(documents.render(long, "poa"), 40)
        self.assertGreater(len(drawn), 1)
        self.assertEqual(" ".join(drawn), address)
        self.assertTrue(all(documents.svg_cards.text_width(t, 12) <= documents.svg_cards.BILL_ADDRESS_WIDTH for t in drawn))
        self.assertLess(40 + documents.svg_cards.BILL_ADDRESS_WIDTH, 380)                  # the date column starts at 380
        self.assertIn("STATEMENT DATE", documents.render(long, "poa"))
        self.assertEqual(long["synth"]["drawn"], documents.POA_REDRAWN)
        # an address that fits draws exactly as before, and the case is not restamped
        short = next(c for c in bills if not documents.poa_wraps(c) and c["synth"]["capture"] not in documents.PHOTO_DEFECTS)
        self.assertEqual(lines(documents.render(short, "poa"), 40),
                         [documents.svg_cards.service_address(short["documents"]["poa"]["address"], short["country"])])
        self.assertEqual(short["synth"]["drawn"], "")
        # an Aadhaar keeps its address on the card, in at most two lines above the issuing authority
        card = next(c for c in book() if c["documents"]["poa"]["kind"] == "aadhaar" and documents.poa_wraps(c))
        drawn = lines(documents.render(card, "poa"), 24)[:2]
        self.assertEqual(" ".join(drawn), card["documents"]["poa"]["address"])
        self.assertTrue(all(24 + documents.svg_cards.text_width(t, 12) <= 616 for t in drawn))

    def test_an_aadhaar_proof_of_address_prints_its_holders_name(self):
        # its address side alone named only a relative (S/O, C/O); the holder's
        # name is printed above the address now, and the case is stamped so a
        # read of the old drawing is redone
        card = by_id("KYC-2026-3056")                                       # S/O: Rakesh Tiwari
        svg = html.unescape(documents.render(card, "poa"))
        self.assertIn(f'>{card["identity"]["name"]["canonical"]}</text>', svg)
        self.assertLess(svg.index(">Name</text>"), svg.index(">Address / Adresse</text>"))
        self.assertIn(">S/O: Rakesh Tiwari,", svg)
        self.assertEqual(card["synth"]["drawn"], documents.AADHAAR_NAMED)
        # a card's back drawn without the holder is as it was: the address first
        adapter = documents._adapter(card, card["customer"]["name"], card["documents"]["poa"]["address"])
        plain = documents.svg_cards.national_id_svg(adapter, {"id": "poa", "kind": "utility_bill", "side": "back", "quality": "ok",
                                                              "country": "IN"}, title="AADHAAR")
        self.assertNotIn(">Name</text>", plain)
        self.assertRegex(plain, r'<text x="24" y="84"[^>]*>Address / Adresse</text>')

    def test_the_passport_mrz_carries_the_holders_sex(self):
        # position 21 of line 2 is the sex the data page prints; no check digit covers it
        for cid, sex in (("KYC-2026-3140", "F"), ("KYC-2026-3127", "M")):
            line = documents.mrz_line2(by_id(cid))
            self.assertEqual((len(line), line[20]), (44, sex))
            self.assertEqual(bad_check_digits(line), [])

    def test_a_secondary_id_prints_its_name_and_address_as_the_file_has_them(self):
        def text(cid):
            return html.unescape(" | ".join(re.findall(r">([^<>]+)</text>", documents.render(by_id(cid), "secondary"))))
        vn = text("KYC-2026-3011")                                            # CCCD: one full name, family name first
        self.assertIn("Full name | NGUYỄN NGỌC PHƯƠNG", vn)
        self.assertNotIn("Surname", vn)
        self.assertIn("Address | 174 Điện Biên Phủ, Phường 25, Quận Bình Thạnh, TP. Hồ Chí Minh", vn)
        self.assertIn("Name | MANDAVA NAGA SAI SRI LAKSHMI", text("KYC-2026-3039"))   # surname first, as written
        self.assertIn("Name | BALAJI K", text("KYC-2026-3032"))                       # an initial, as written
        pan = text("KYC-2026-3012")                                           # a PAN card prints no address
        self.assertNotIn("Address", pan)
        self.assertIn("Name | ASHISH UPADHYAY", pan)
        # a US licence prints a surname's particles with it: DE LUCA, ANDREW R (not LUCA, ANDREW R DE)
        de_luca = text("KYC-2026-3119")
        self.assertIn("1 | DE LUCA | 2 | ANDREW R", de_luca)
        self.assertIn("1 | MAC KENZIE | 2 | THOMAS D", text("KYC-2026-3189"))
        self.assertIn("1 | WALLACE | 2 | MARGARET M", text("KYC-2026-3276"))       # no particle: the last word
        # its header names the state of its own address, any of the fifty and DC (a Miami licence is Florida's)
        miami = text("KYC-2026-3931")
        self.assertIn("FLORIDA", miami)
        self.assertNotIn("OHIO", miami)
        from fincrime.documents import svg_cards
        self.assertEqual([svg_cards.us_state(a) for a in ("179 SE 82ND AVE, PORTLAND, OR 97266", "1 Main St, Washington, DC 20001-1234",
                                                          "Columbus, OH", "")],
                         ["OREGON", "DISTRICT OF COLUMBIA", "OHIO", "OHIO"])

    def test_the_drawn_sex_follows_what_the_file_says(self):
        # a relationship on the address, an honorific, a married-name change, then the names
        self.assertEqual(by_id("KYC-2026-3140")["synth"]["sex"], "F")                    # Jennifer Smith
        self.assertEqual(by_id("KYC-2026-3127")["synth"]["sex"], "M")                    # Ken Sullivan
        self.assertEqual(by_id("KYC-2026-3604")["synth"]["sex"], "F")                    # D/O on the Aadhaar
        case = by_id("KYC-2026-3140", fresh=True)
        case["documents"]["poa"]["address"] = "S/O: Ramesh, 12 Main Road"
        self.assertEqual(documents.holder_sex(case), "M")
        case["documents"]["poa"]["address"], case["customer"]["name"] = "12 Main Road", "Smt. Jennifer Smith"
        case["documents"]["passport"]["viz"]["given"] = "KIRAN"
        self.assertEqual(documents.holder_sex(case), "F")
        case["customer"]["name"] = "Kiran Smith"
        self.assertEqual(documents.holder_sex(case), "")                                # nothing says: the coin toss stays
        case["raw"] = {"name_variance_type": "Married vs maiden surname"}
        self.assertEqual(documents.holder_sex(case), "F")
        self.assertRegex(documents.render(by_id("KYC-2026-3140"), "passport"), r"Sex / Sexe</text><text[^>]*>F</text>")

if __name__ == "__main__":
    unittest.main()
