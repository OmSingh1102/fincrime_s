"""The hand-written scenarios (nineteen).

Each one exists to demonstrate a branch of the SOP. All people, companies,
documents and watchlist records are fictional; adverse-media text and source
links are invented and point at reserved example domains.
"""
from fincrime.data.seed import (AMARA, JON, LEAH, MARIO, NISHA, RYAN, agency_info, common_name_matches, doc, event,
                                handoff, kyc_case, pair_screening, resolved, std_docs, wc_match)
from fincrime.utils import dates

_pair = pair_screening       # the screening case + watchlist case + intake rules for one KYC case


def build():
    kyc, alerts, wc = [], [], []

    # 1 ── Daniel Mercer: the golden path ───────────────────────────────────
    mercer = kyc_case(
        key="mercer", name="Daniel Mercer", nationality="US", dob="1988-02-11", id_type="DrivingLicense",
        issuing="US", us_person=True, tags=["Tier 1", "Tier 3"], poa_required=True,
        manual_review_reason=["Namescreen hit - WC or CMS"], created_hours_ago=20, vendor="jumio_ios_webview",
        docs=std_docs("driving_licence", poa=True), address="1842 Maple Ridge Drive, Columbus, OH 43215",
        country="United States", screening_hit=True,
    )
    # Six records list a year of birth that is not his: the DOB rule clears
    # them at intake. The seventh lists no date of birth at all, so the rules
    # leave it to the analyst — the golden path is the human-in-the-loop one.
    mercer_matches = [
        wc_match(key="mercer-1", name="Daniel MERCER", strength=0.93, types=("LE", "SIC"), dobs=["1973", "1972"],
                 citizenship="United States", country="United States", category="POST CONVICTION",
                 special_interest=["Fraud"], entered="2003-07-31", updated="2022-06-08"),
        wc_match(key="mercer-2", name="Daniel MERCER", strength=0.93, types=("LE", "SIC"), dobs=["1970", "1969"],
                 citizenship="United States", country="United States", category="POST CONVICTION",
                 special_interest=["Violent Crime"], entered="2023-05-29", updated="2023-05-29"),
        wc_match(key="mercer-3", name="Daniel MERCER", strength=0.93, types=("LE", "SIC"), dobs=["16-Aug-1960"],
                 citizenship="United States", country="United States", category="POST CONVICTION",
                 special_interest=["Fraud"], entered="2005-10-25", updated="2022-08-19"),
        wc_match(key="mercer-4", name="Daniel MERCER", strength=0.93, types=("SIC",), dobs=["1962", "1963"],
                 citizenship="United Kingdom", country="United Kingdom", category="POST CONVICTION",
                 special_interest=["Conspiracy or Collusion"], entered="2025-12-26", updated="2025-12-26"),
        wc_match(key="mercer-5", name="Daniel MERCER", strength=0.93, types=("LE", "SIC"), dobs=["1973", "1972"],
                 citizenship="United States", country="United States", category="POST CONVICTION",
                 special_interest=["Fraud"], entered="2010-11-24", updated="2022-06-09"),
        wc_match(key="mercer-6", name="Daniel Robert MERCER", alias="MERCER, Daniel", strength=0.9, types=("SIC", "RE"),
                 dobs=["18-Feb-1958"], citizenship="United Kingdom", country="United Kingdom", category="REGULATORY",
                 special_interest=["Fraud"], entered="2022-08-03", updated="2022-08-03"),
        wc_match(
            key="mercer-7", name="Daniel MERCER", strength=0.86, types=("SIC", "OB"), dobs=[],
            citizenship="United Kingdom", country="United Kingdom", category="POST CONVICTION",
            special_interest=["Conspiracy or Collusion", "Illegal Possession or Sale", "Narcotics Trafficking"],
            location={"city": "Wolverhampton", "region": "West Midlands", "country": "United Kingdom (GBR)"},
            bio="Member of a narcotics supply network operating across the West Midlands.",
            reports=[("Apr 2023", "Sentenced to 7 years and 4 months imprisonment for conspiracy to supply class A drugs "
                                  "and possession with intent to supply (12 kg, est. GBP 480,000) (Feb 2022).")],
            sources=[("08-May-2023", "https://news.example/west-midlands/crime/four-jailed-after-drug-supply-network-dismantled"),
                     ("08-May-2023", "https://regional-news.example/black-country/drug-dealers-sentenced"),
                     ("08-May-2023", "https://police.example/west-midlands/news/2023/may/four-jailed-cocaine-supply")],
            entered="2023-05-11", updated="2023-05-11",
        ),
    ]
    a, w = _pair(mercer, "mercer", mercer_matches)
    kyc.append(mercer); alerts.append(a); wc.append(w)

    # 2 ── Farah Rahman: one PEP match, resolved as a false positive ────────
    rahman = kyc_case(
        key="rahman", name="Farah Rahman", nationality="SG", dob="1996-12-09", id_type="NationalID", gender="F",
        tags=["Tier 3"], created_hours_ago=14, status="DataScreening", confirmed_hours_after=0.9,
        l1=[JON], previous_reviewer=JON, routed_to=JON, screening_hit=True, address="12 Tanjong Pagar Road, Singapore 088444",
        country="Singapore",
    )
    rahman_matches = [wc_match(
        key="rahman-1", name="Farah Nur RAHMAN", alias="Farah Nur RAHMAN", strength=0.974, types=("PEP",), gender="Female",
        citizenship="Malaysia", country="Malaysia", category="LEGAL", sub_category="PEP Sub-National Government",
        pep_status="Active", position="Member of the Judiciary",
        location={"city": "Kuala Lumpur", "region": "Kuala Lumpur", "country": "Malaysia (MYS)"},
        bio="Judge of the Sessions Court, Kuala Lumpur (reported Nov 2015 - ).",
        identification="National ID No. 741205-14-XXXX",
        sources=[("20-Nov-2015", "https://gazette.example/my/judicial-appointments-2015.pdf"),
                 ("02-Mar-2021", "https://courts.example/my/sessions-court/judges")],
        entered="2015-11-20", updated="2021-03-02",
    )]
    a, w = _pair(rahman, "rahman", rahman_matches)
    kyc.append(rahman); alerts.append(a); wc.append(w)

    # 3 ── Anatoly Sidorov: sanctioned-country passport, clean POA, OFAC hit ─
    sidorov = kyc_case(
        key="sidorov", name="Anatoly Petrovich Sidorov", native_name="Анатолий Петрович Сидоров",
        nationality="RU", dob="1975-04-12", id_type="Passport", issuing="RU", tags=["Tier 4"], poa_required=True,
        manual_review_reason=["POA Collected", "Namescreen hit - WC or CMS", "Namescreen hit - Sanction country"],
        created_hours_ago=9, docs=std_docs("passport", poa=True),
        address="45 Griva Digeni Avenue, Office 302, Nicosia 1080, Cyprus", country="Cyprus", poa_country="CY", screening_hit=True,
    )
    sidorov_matches = [wc_match(
        key="sidorov-1", name="SIDOROV, Anatoly Petrovich", alias="Anatolii SIDOROV", strength=0.98, types=("SAN",),
        dobs=["12-Apr-1975"], citizenship="Russian Federation", country="Russian Federation; Cyprus",
        category="SANCTIONS", sub_category="Consolidated sanctions list", special_interest=["Sanctions - Designated"],
        location={"city": "Moscow", "region": "Moscow", "country": "Russian Federation (RUS)"},
        bio="Designated for providing dual-use technology and financing to state defence entities.",
        identification="Passport No. 75 XXXXXXX (RUS)",
        reports=[("Jun 2024", "Added to the consolidated sanctions list; assets subject to freeze.")],
        aliases=["Anatoli SIDOROFF", "SIDOROV Anatolij"],
        sources=[("14-Jun-2024", "https://sanctions.example/consolidated/entry/4410982"),
                 ("14-Jun-2024", "https://gazette.example/eu/official-journal/2024-06")],
        entered="2024-06-14", updated="2025-02-20",
    )]
    a, w = _pair(sidorov, "sidorov", sidorov_matches)
    kyc.append(sidorov); alerts.append(a); wc.append(w)

    # 4 ── Tara Nguyen: cropped ID → Resubmit ──────────────────────────────
    kyc.append(kyc_case(
        key="nguyen", name="Tara Nguyen", nationality="VN", dob="1995-07-14", id_type="NationalID", gender="F",
        tags=["Tier 3"], manual_review_reason=["Vendor rejected"], created_hours_ago=6,
        vendor_result="Declined", vendor_reason="Document edges not visible.",
        docs=[doc("national_id", "front", "cropped"), doc("national_id", "back")],
        address="88 Nguyen Hue Boulevard, District 1, Ho Chi Minh City", country="Vietnam",
    ))

    # 5 ── Ariana Solis: happy path, nothing to resolve ────────────────────
    kyc.append(kyc_case(
        key="solis", name="Ariana Solis", nationality="MX", dob="1994-08-21", id_type="Passport", gender="F",
        tags=["Tier 2"], manual_review_reason=["Manual referral"], created_hours_ago=3,
        address="Av. Insurgentes Sur 1602, Ciudad de Mexico", country="Mexico",
    ))

    # 6 ── Ahmed Hassan: 212 matches on a common name ──────────────────────
    hassan = kyc_case(
        key="hassan", name="Ahmed Hassan", nationality="PK", dob="2005-07-26", id_type="NationalID",
        tags=["Tier 4"], created_hours_ago=30, status="DataScreening", confirmed_hours_after=1.0,
        l1=[MARIO], previous_reviewer=MARIO, routed_to=MARIO, screening_hit=True, address="Block 7, Clifton, Karachi", country="Pakistan",
    )
    hassan_matches = common_name_matches(first="Ahmed", surname="Hassan", client_year=2005, n=212, seed="hassan", keep_unresolved=3)
    top = hassan_matches[-3]
    top.update(name="Ahmed Bilal HASSAN", strength=0.91, types=["SIC", "OB"], dobs=["2005"])
    top["key_data"].update(category="POST CONVICTION",
                           special_interest=["Terror Related", "Violent Crime", "Conspiracy or Collusion", "Crimes Against the State"],
                           location={"city": "Karachi", "region": "Sindh", "country": "Pakistan (PAK)"})
    top["further_info"].update(biography="Convicted in connection with an organised violent-crime group (Karachi, 2024).",
                               reports=[["Sep 2024", "Sentenced to 12 years imprisonment on conspiracy and weapons charges (Aug 2024)."]])
    top["sources"] = [["11-Sep-2024", "https://news.example/pk/karachi/court-sentences-twelve-in-gang-case"]]
    a, w = _pair(hassan, "hassan", hassan_matches)
    kyc.append(hassan); alerts.append(a); wc.append(w)

    # 7 ── Michael James Taylor: the auto-verified path ─────────────────────
    # A wanted-notice namesake born 23 years earlier and a female namesake
    # born the same year: the DOB rule clears one, the gender rule the other,
    # and the screening case closes itself at intake. Once Amara confirms his
    # data nothing is left for an analyst and the case verifies on its own.
    taylor = kyc_case(
        key="taylor", name="Michael James Taylor", nationality="GB", dob="1991-08-15", id_type="Passport",
        tags=["Tier 1"], created_hours_ago=11, status="DataConfirming", routed_to=AMARA, screening_hit=True,
        address="142 High Street, Kensington, London W8 7RG", country="United Kingdom",
    )
    taylor_matches = [wc_match(
        key="taylor-1", name="TAYLOR, Michael James", strength=0.81, types=("LE",), dobs=["10-Feb-1968"],
        citizenship="Canada", country="Canada", category="LAW ENFORCEMENT", sub_category="Wanted - international notice",
        special_interest=["Fraud", "Computer Misuse"], location={"city": "Toronto", "region": "Ontario", "country": "Canada (CAN)"},
        bio="Subject of an international wanted notice for wire fraud and computer misuse offences (issued 2019).",
        sources=[("03-Feb-2019", "https://notices.example/wanted/red/2019-0921")], entered="2019-02-03", updated="2024-01-15",
    ), wc_match(
        key="taylor-2", name="TAYLOR, Michele", alias="Michele J. TAYLOR", strength=0.74, types=("LE",), gender="Female", dobs=["1991"],
        citizenship="United Kingdom", country="United Kingdom", category="LAW ENFORCEMENT", sub_category="Wanted - domestic notice",
        special_interest=["Fraud"], location={"city": "Leeds", "region": "West Yorkshire", "country": "United Kingdom (GBR)"},
        bio="Sought in connection with a benefit-fraud investigation (2021).",
        sources=[("14-Sep-2021", "https://notices.example/wanted/domestic/2021-3377")], entered="2021-09-14", updated="2023-03-02",
    )]
    a, w = _pair(taylor, "taylor", taylor_matches)
    kyc.append(taylor); alerts.append(a); wc.append(w)

    # 8 ── Carlos Eduardo Mendoza: confirmed PEP, waiting at L2 ────────────
    mendoza = kyc_case(
        key="mendoza", name="Carlos Eduardo Mendoza", nationality="BR", dob="1969-11-03", id_type="NationalID",
        tags=["Tier 3", "Priority Review"], created_hours_ago=40, status="DataEscalatingL2", confirmed_hours_after=1.1,
        escalated_hours_after=2.4, l1=[AMARA], previous_reviewer=AMARA, pending_reason="Pending L2", case_review_level="L2",
        screening_hit=True, address="Avenida Atlantica 2100, Apt 801, Copacabana, Rio de Janeiro", country="Brazil",
    )
    esc_at = mendoza["escalated_at"]
    mendoza_matches = [resolved(wc_match(
        key="mendoza-1", name="MENDOZA, Carlos Eduardo", strength=1.0, types=("PEP",), dobs=["03-Nov-1969"],
        citizenship="Brazil", country="Brazil", category="PEP", sub_category="PEP National Government", pep_status="Active",
        position="Deputy Minister of Energy & Infrastructure",
        location={"city": "Brasilia", "region": "Distrito Federal", "country": "Brazil (BRA)"},
        bio="Deputy Minister of Energy & Infrastructure (reported Jan 2023 - ). Public official with oversight of state-owned enterprise contracting.",
        identification="National ID No. 412.XXX.XXX-09",
        sources=[("12-Jan-2023", "https://gazette.example/br/diario-oficial/2023-01-12"), ("30-Jun-2025", "https://ministry.example/br/energia/gabinete")],
        entered="2023-01-12", updated="2025-06-30",
    ), "True positive", "High", "Confirmed by identification", AMARA, dates.add_minutes(esc_at, -20),
        "Name, DOB and national ID all match the PEP record.")]
    a, w = _pair(mendoza, "mendoza", mendoza_matches, status="Closed", assigned_to=AMARA,
                 handled_at=dates.add_minutes(esc_at, -40),
                 case_outcome={"value": "Escalate - True Positive", "rationale": "Confirmed true positive",
                               "comment": "Confirmed PEP (national government). Escalating for EDD and senior approval.",
                               "by": AMARA, "at": dates.add_minutes(esc_at, -5)})
    w["assignee"] = AMARA
    kyc.append(mendoza); alerts.append(a); wc.append(w)

    # 9 ── Tariq Al-Mansoor: law-enforcement hit, L2 to reject ─────────────
    almansoor = kyc_case(
        key="almansoor", name="Tariq Mahmoud Al-Mansoor", nationality="AE", dob="1981-10-18", id_type="NationalID",
        tags=["Tier 1"], created_hours_ago=26, status="DataEscalatingL2", confirmed_hours_after=0.8,
        escalated_hours_after=1.9, l1=[JON], previous_reviewer=JON, pending_reason="Pending L2", case_review_level="L2",
        screening_hit=True, address="Villa 88, Al Barsha 2, Dubai", country="United Arab Emirates",
        routed_to=NISHA, routed_hours_after=1.9,
        handoffs=[handoff("L1", "L2", dates.add_hours(dates.hours_ago(26), 1.9), NISHA, 0.0, "hub")],
    )
    esc_at = almansoor["escalated_at"]
    almansoor_matches = [resolved(wc_match(
        key="almansoor-1", name="AL-MANSOOR, Tariq Mahmoud", strength=0.97, types=("LE", "SIC"), dobs=["18-Oct-1981"],
        citizenship="United Arab Emirates", country="United Arab Emirates; Qatar", category="LAW ENFORCEMENT",
        special_interest=["Fraud", "Identity Theft"], location={"city": "Dubai", "region": "Dubai", "country": "United Arab Emirates (ARE)"},
        bio="Wanted by a national financial-crime authority in connection with an investment fraud scheme and identity-fraud operations.",
        identification="Passport No. AXXXXXXX (ARE)",
        sources=[("22-Aug-2025", "https://authority.example/ae/wanted/2025-118")], entered="2025-08-22", updated="2026-01-10",
    ), "True positive", "High", "Confirmed by identification", JON, dates.add_minutes(esc_at, -15),
        "Exact match on name, DOB and residential area; passport number matches.")]
    a, w = _pair(almansoor, "almansoor", almansoor_matches, status="Closed", assigned_to=JON,
                 handled_at=dates.add_minutes(esc_at, -30),
                 case_outcome={"value": "Reject - True Positive", "rationale": "Confirmed true positive",
                               "comment": "Law-enforcement true positive. Escalating for account block and FIU notification.",
                               "by": JON, "at": dates.add_minutes(esc_at, -3)})
    w["assignee"] = JON
    kyc.append(almansoor); alerts.append(a); wc.append(w)

    # 10 ── Sofia Petrova: US person, adverse media, Skopenow path ─────────
    petrova = kyc_case(
        key="petrova", name="Sofia Petrova", nationality="US", dob="1990-01-19", id_type="Passport", gender="F",
        us_person=True, tags=["Tier 3"], created_hours_ago=16, status="DataScreening", confirmed_hours_after=0.6,
        l1=[AMARA], previous_reviewer=AMARA, routed_to=AMARA, screening_hit=True, address="410 Pine Street, Apt 5B, Seattle, WA 98101",
        country="United States",
    )
    petrova_matches = [wc_match(
        key="petrova-1", name="Sofia PETROVA", strength=0.88, types=("SIC",), gender="Female", dobs=["1990"],
        citizenship="United States", country="United States; Bulgaria", category="ADVERSE MEDIA",
        special_interest=["Fraud"], location={"city": "Seattle", "region": "Washington", "country": "United States (USA)"},
        bio="Named in press reporting on a disputed online investment programme (2024); no charges reported.",
        sources=[("19-Mar-2024", "https://news.example/us/business/investors-question-online-programme")],
        entered="2024-03-19", updated="2024-03-19",
    )]
    a, w = _pair(petrova, "petrova", petrova_matches)
    kyc.append(petrova); alerts.append(a); wc.append(w)

    # 11 ── Nadia Yusuf: low-score transliteration variant that IS the person
    yusuf = kyc_case(
        key="yusuf", name="Nadia Yusuf", nationality="AE", dob="1992-10-02", id_type="Passport", gender="F",
        tags=["Tier 4"], created_hours_ago=8, status="DataScreening", confirmed_hours_after=0.5,
        l1=[JON], previous_reviewer=JON, routed_to=JON, screening_hit=True, address="Marina Walk, Tower 3, Dubai Marina", country="United Arab Emirates",
    )
    yusuf_matches = [wc_match(
        key="yusuf-1", name="Nadiya YUSSUF", alias="Nadia YUSUF", strength=0.72, types=("SIC", "RE"), gender="Female",
        dobs=["02-Oct-1992"], citizenship="United Arab Emirates", country="United Arab Emirates", category="REGULATORY",
        special_interest=["Trade-based money laundering", "Customs fraud"],
        location={"city": "Dubai", "region": "Dubai", "country": "United Arab Emirates (ARE)"},
        bio="Subject of investigative reporting on customs-duty fraud and trade-based money laundering through a Dubai trading house (2023).",
        aliases=["Nadia YUSUF", "Nadiyah YOUSEF"],
        sources=[("05-Oct-2023", "https://investigations.example/gulf/trade-misinvoicing-network")], entered="2023-10-05", updated="2025-04-11",
    )]
    a, w = _pair(yusuf, "yusuf", yusuf_matches)
    kyc.append(yusuf); alerts.append(a); wc.append(w)

    # 12 ── Milo Stein: 96% and same DOB, different person ─────────────────
    stein = kyc_case(
        key="stein", name="Milo Stein", nationality="DE", dob="1989-05-18", id_type="Passport",
        tags=["Tier 2"], created_hours_ago=5, status="DataScreening", confirmed_hours_after=0.4,
        l1=[MARIO], previous_reviewer=MARIO, routed_to=MARIO, screening_hit=True, address="Leopoldstrasse 27, 80802 Munich", country="Germany",
    )
    stein_matches = [wc_match(
        key="stein-1", name="Milo STEIN", strength=0.96, types=("LE",), dobs=["18-May-1989"], pob="Graz",
        citizenship="Austria", country="Austria", category="LAW ENFORCEMENT", special_interest=["Fraud"],
        location={"city": "Graz", "region": "Styria", "country": "Austria (AUT)"},
        bio="Convicted of insurance fraud (Graz Regional Court, 2019).",
        sources=[("14-Nov-2019", "https://courts.example/at/graz/judgments/2019-1422")], entered="2019-11-14", updated="2019-11-14",
    )]
    a, w = _pair(stein, "stein", stein_matches)
    kyc.append(stein); alerts.append(a); wc.append(w)

    # 13 ── Closed history ─────────────────────────────────────────────────
    gomez = kyc_case(
        key="gomez", name="Jose Luis Rodriguez Gomez", nationality="ES", dob="1985-12-04", id_type="NationalID",
        tags=["Tier 3"], created_hours_ago=76, status="Verified", confirmed_hours_after=1.3, closed_hours_after=2.6,
        l1=[AMARA], previous_reviewer=AMARA, screening_hit=True, address="Calle de Alcala 85, 4 Dcha, 28009 Madrid", country="Spain",
        qc={"reviewer": LEAH, "result": "Pass", "reason": "Decision correct and evidenced", "comment": "DOB gap documented, export attached, single-line rationale."},
    )
    g_at = dates.add_hours(gomez["created_at"], 2.2)
    gomez_matches = [resolved(wc_match(
        key="gomez-1", name="RODRIGUEZ GOMEZ, Jose Luis", strength=0.85, types=("SAN",), dobs=["19-May-1958"],
        citizenship="Colombia", country="Colombia", category="SANCTIONS", sub_category="Financial sanctions - narcotics",
        location={"city": "Bogota", "region": "Cundinamarca", "country": "Colombia (COL)"},
        bio="Designated for financial facilitation of narcotics trafficking organisations.",
        sources=[("02-Feb-2018", "https://sanctions.example/list/entry/2018-0312")], entered="2018-02-02", updated="2023-09-01",
    ), "False positive", "Low", "Date of Birth mismatch", AMARA, g_at, "27-year DOB gap; Spanish national ID verified.")]
    a, w = _pair(gomez, "gomez", gomez_matches, status="Closed", assigned_to=AMARA, handled_at=dates.add_minutes(g_at, -10),
                 case_outcome={"value": "Approve - False Positive", "rationale": "All alerts resolved as false positive",
                               "comment": "DOB and country mismatch. Spanish DNI and proof of address verified.", "by": AMARA,
                               "at": dates.add_minutes(g_at, 4)})
    kyc.append(gomez); alerts.append(a); wc.append(w)

    alhassan = kyc_case(
        key="alhassan", name="Fatima Al-Hassan", nationality="JO", dob="1995-02-14", id_type="NationalID", gender="F",
        tags=["Tier 2"], created_hours_ago=52, status="Verified", confirmed_hours_after=0.9, closed_hours_after=1.8,
        l1=[JON], previous_reviewer=JON, screening_hit=True, address="Building 12, Rainbow Street, Amman 11118", country="Jordan",
        qc={"reviewer": LEAH, "result": "Pass", "reason": "Decision correct and evidenced", "comment": "Distinct person established; employer letter referenced."},
    )
    f_at = dates.add_hours(alhassan["created_at"], 1.5)
    alhassan_matches = [resolved(wc_match(
        key="alhassan-1", name="AL-HASSAN, Fatima Zahra", strength=0.84, types=("PEP",), gender="Female", dobs=["09-Jul-1962"],
        citizenship="Jordan", country="Jordan", category="PEP", sub_category="PEP Legislative", pep_status="Inactive",
        position="Former Member of Parliament", location={"city": "Zarqa", "region": "Zarqa", "country": "Jordan (JOR)"},
        bio="Former Member of Parliament and parliamentary committee chair (2013 - 2020).",
        sources=[("05-Feb-2013", "https://parliament.example/jo/members/17th")], entered="2013-02-05", updated="2020-11-30",
    ), "False positive", "Low", "Date of Birth mismatch", JON, f_at, "33-year DOB gap; customer is a software engineer, not the former MP.")]
    a, w = _pair(alhassan, "alhassan", alhassan_matches, status="Closed", assigned_to=JON, handled_at=dates.add_minutes(f_at, -8),
                 case_outcome={"value": "Approve - False Positive", "rationale": "All alerts resolved as false positive",
                               "comment": "National ID verified; employer letter on file. Distinct person.", "by": JON,
                               "at": dates.add_minutes(f_at, 3)})
    kyc.append(alhassan); alerts.append(a); wc.append(w)

    rostova = kyc_case(
        key="rostova", name="Elena Rostova", nationality="CH", dob="1988-03-30", id_type="ResidencePermit", gender="F",
        tags=["Tier 1", "Priority Review"], created_hours_ago=120, status="Verified", confirmed_hours_after=1.0,
        escalated_hours_after=2.0, closed_hours_after=70.0, l1=[AMARA], l2=[NISHA], edd=[RYAN], previous_reviewer=RYAN,
        screening_hit=True, address="74 Quai de Cologny, 1223 Cologny, Geneva", country="Switzerland",
        docs=std_docs("residence_permit", poa=True), poa_required=True,
        handoffs=[handoff("L1", "L2", dates.add_hours(dates.hours_ago(120), 2.0), NISHA, 5.0, "bot"),
                  handoff("L2", "EDD", dates.hours_ago(120 - 30), RYAN, 3.5, "bot")],
        qc={"reviewer": LEAH, "result": "Pass", "reason": "Decision correct and evidenced", "comment": "Source-of-wealth file complete; senior approval on record."},
        extra_audit=[event(dates.hours_ago(120 - 30), NISHA, "L2 decision", "Escalate to EDD — source of wealth review required for PEP relative."),
                     event(dates.hours_ago(120 - 69), RYAN, "EDD completed", "Source of wealth established via inheritance deeds and audited holding-company dividends. Senior approval recorded.")],
    )
    r_at = dates.add_hours(rostova["created_at"], 1.7)
    rostova_matches = [resolved(wc_match(
        key="rostova-1", name="ROSTOVA, Elena", alias="Elena ROSTOVA (RCA)", strength=0.95, types=("PEP", "RCA"), gender="Female",
        dobs=["30-Mar-1988"], citizenship="Switzerland", country="Switzerland; Austria", category="PEP",
        sub_category="Relative or close associate", pep_status="Active", position="Spouse of a Vice Minister",
        location={"city": "Geneva", "region": "Geneva", "country": "Switzerland (CHE)"},
        bio="Spouse of a senior government official; holds significant offshore real-estate assets.",
        sources=[("18-Jul-2022", "https://registry.example/ch/property/cologny-2022")], entered="2022-07-18", updated="2025-05-05",
    ), "True positive", "High", "Confirmed by identification", AMARA, r_at, "Exact match on spouse profile of the PEP.")]
    a, w = _pair(rostova, "rostova", rostova_matches, status="Closed", assigned_to=AMARA, handled_at=dates.add_minutes(r_at, -12),
                 case_outcome={"value": "Escalate - True Positive", "rationale": "Confirmed true positive",
                               "comment": "RCA confirmed. Escalating for EDD with source-of-wealth review.", "by": AMARA,
                               "at": dates.add_minutes(r_at, 6)})
    kyc.append(rostova); alerts.append(a); wc.append(w)

    # 14 ── Vanguard Tech Supplies FZE: business Checker 1, export-control hit
    vanguard = kyc_case(
        key="vanguard", name="Vanguard Tech Supplies FZE", nationality="AE", dob="", tenant="ttshop", status="DataScreening",
        id_type="TradeLicense", gender="", tags=["Introducer"], poa_required=True, manual_review_reason=["Namescreen hit - WC or CMS"],
        created_hours_ago=18, confirmed_hours_after=1.4, l1=[JON], previous_reviewer=JON, vendor="shufti_pro_android_webview",
        docs=[doc("trade_licence", "page 1"), doc("passport", "front"), doc("passport", "back"), doc("utility_bill", "page 1")],
        address="Building 4W, Office 508, Dubai Airport Free Zone, Dubai", country="United Arab Emirates", screening_hit=True,
        risk_rating="Medium",
        agency=agency_info(company="Vanguard Tech Supplies FZE", registration_number="AE-FZ-3021901", incorporation_country="United Arab Emirates",
                           address="Building 4W, Office 508, Dubai Airport Free Zone, Dubai", representative="Omar Haddad",
                           rep_email="o.haddad@vanguard-tech.example", rep_phone="+971 4 XXX XXXX",
                           ubos=[("Omar Haddad", 60, "AE"), ("Leila Farouk", 40, "AE")], onboarding_time=dates.hours_ago(18)),
    )
    vanguard_matches = [wc_match(
        key="vanguard-1", name="VANGUARD TECH SUPPLIES FZE", strength=0.96, types=("RE", "SAN"), gender="",
        citizenship="United Arab Emirates", country="United Arab Emirates", category="REGULATORY", sub_category="Export control - entity list",
        special_interest=["Export Control"], location={"city": "Dubai", "region": "Dubai Airport Free Zone", "country": "United Arab Emirates (ARE)"},
        bio="Added to an export-control entity list for unauthorised transshipment of dual-use microelectronics to restricted end users (2024).",
        identification="Commercial Reg. No. AE-FZ-3021901",
        sources=[("30-Oct-2024", "https://export-control.example/entity-list/additions-2024-10")], entered="2024-10-30", updated="2024-10-30",
    )]
    a, w = _pair(vanguard, "vanguard", vanguard_matches)
    kyc.append(vanguard); alerts.append(a); wc.append(w)

    # 15 ── Apex Minerals Trading Corp: adverse media on a 35% UBO ─────────
    apex = kyc_case(
        key="apex", name="Apex Minerals Trading Corp", nationality="SG", dob="", tenant="ttshop", status="DataScreening",
        id_type="TradeLicense", gender="", tags=["Priority Review"], poa_required=True, created_hours_ago=22, confirmed_hours_after=1.1,
        l1=[MARIO], previous_reviewer=MARIO, vendor="jumio_android_webview",
        docs=[doc("trade_licence", "page 1"), doc("passport", "front"), doc("bank_statement", "page 1")],
        address="Suite 1201, Tower 2, Marina Bay Financial Centre, Singapore 018982", country="Singapore", screening_hit=True,
        risk_rating="Medium",
        agency=agency_info(company="Apex Minerals Trading Corp", registration_number="SG-2019821-A", incorporation_country="Singapore",
                           address="Suite 1201, Tower 2, Marina Bay Financial Centre, Singapore 018982", representative="Mei Lin Tan",
                           ubos=[("Mei Lin Tan", 65, "SG"), ("Viktor Kogan", 35, "RU")], onboarding_time=dates.hours_ago(22)),
    )
    apex_matches = [wc_match(
        key="apex-1", name="KOGAN, Viktor", strength=0.91, types=("SIC",), dobs=["21-Jun-1964"],
        citizenship="Russian Federation", country="Singapore; Switzerland; Russian Federation", category="ADVERSE MEDIA",
        special_interest=["Trade-based money laundering", "Customs fraud"],
        location={"city": "Singapore", "region": "", "country": "Singapore (SGP)"},
        bio="Subject of international investigative reporting on trade-based money laundering and customs-duty fraud (2023 - ).",
        sources=[("12-Sep-2023", "https://investigations.example/commodities/minerals-invoicing-scheme")], entered="2023-09-12", updated="2025-08-01",
    )]
    a, w = _pair(apex, "apex", apex_matches)
    kyc.append(apex); alerts.append(a); wc.append(w)

    # 16 ── Oceanic Logistics & Energy: 52% owned by a sanctioned parent ───
    oceanic = kyc_case(
        key="oceanic", name="Oceanic Logistics & Energy Pte Ltd", nationality="SG", dob="", tenant="ttshop", status="DataEscalatingL2",
        id_type="TradeLicense", gender="", tags=["Introducer", "Priority Review"], poa_required=True, created_hours_ago=44,
        confirmed_hours_after=1.0, escalated_hours_after=2.8, l1=[JON], l2=[NISHA], previous_reviewer=NISHA, pending_reason="Pending EDD",
        case_review_level="L3", vendor="shufti_pro_android_webview", routed_to=RYAN, routed_hours_after=32.8,
        handoffs=[handoff("L1", "L2", dates.add_hours(dates.hours_ago(44), 2.8), NISHA, 4.2, "bot"),
                  handoff("L2", "EDD", dates.add_hours(dates.hours_ago(44), 32.8), RYAN, 0.0, "hub")],
        extra_audit=[event(dates.add_hours(dates.hours_ago(44), 32.8), NISHA, "Escalated to EDD",
                           "Sanctions exposure via the majority parent — EDD to establish beneficial ownership and take legal advisory.")],
        docs=[doc("trade_licence", "page 1"), doc("passport", "front"), doc("utility_bill", "page 1")],
        address="8 Shenton Way, #24-01, Singapore 068811", country="Singapore", screening_hit=True, risk_rating="High",
        agency=agency_info(company="Oceanic Logistics & Energy Pte Ltd", registration_number="SG-2018-0910-L", incorporation_country="Singapore",
                           address="8 Shenton Way, #24-01, Singapore 068811", representative="Wei Chen",
                           ubos=[("Nordvek Energy Holdings", 52, "RU"), ("Wei Chen", 48, "SG")], onboarding_time=dates.hours_ago(44)),
    )
    esc_at = oceanic["escalated_at"]
    oceanic_matches = [resolved(wc_match(
        key="oceanic-1", name="NORDVEK ENERGY HOLDINGS", strength=0.88, types=("SAN",), gender="",
        citizenship="Russian Federation", country="Russian Federation", category="SANCTIONS", sub_category="Sectoral sanctions - Directive 2",
        location={"city": "Moscow", "region": "Moscow", "country": "Russian Federation (RUS)"},
        bio="Parent entity subject to sectoral sanctions. Entities owned 50% or more are treated as sanctioned under the ownership rule.",
        identification="Registration No. 1027700XXXXXX",
        sources=[("12-Sep-2014", "https://sanctions.example/sectoral/directive-2/entry/2014-0093")], entered="2014-09-12", updated="2024-02-23",
    ), "True positive", "High", "Confirmed by identification", JON, dates.add_minutes(esc_at, -25),
        "Shareholding shows 52% ownership by the listed parent — 50% rule applies.")]
    a, w = _pair(oceanic, "oceanic", oceanic_matches, status="Closed", assigned_to=JON, handled_at=dates.add_minutes(esc_at, -45),
                 case_outcome={"value": "Reject - True Positive", "rationale": "Confirmed true positive",
                               "comment": "Derivative sanctions exposure via majority parent. Escalating to L2 for freeze and legal advisory.",
                               "by": JON, "at": dates.add_minutes(esc_at, -4)})
    w["assignee"] = JON
    kyc.append(oceanic); alerts.append(a); wc.append(w)

    # 17 ── Beatriz Camargo: proof of address eight months old → Resubmit POA
    kyc.append(kyc_case(
        key="camargo", name="Beatriz Camargo", nationality="BR", dob="1997-03-09", id_type="Passport", gender="F",
        tags=["Tier 3"], poa_required=True, manual_review_reason=["POA Collected"], created_hours_ago=4,
        docs=std_docs("passport", poa=True, poa_issued_days_ago=245),
        address="Rua Oscar Freire 1200, Jardins, Sao Paulo 01426-001", country="Brazil",
    ))

    # 18 ── Dmitri Volkov: sanctioned-country passport AND a same-country POA → must escalate
    kyc.append(kyc_case(
        key="volkov", name="Dmitri Volkov", native_name="Дмитрий Волков", nationality="RU", dob="1983-06-21",
        id_type="Passport", issuing="RU", tags=["Tier 4"], poa_required=True,
        manual_review_reason=["POA Collected", "High-risk country"], created_hours_ago=7,
        docs=std_docs("passport", poa=True, poa_country="RU"),
        address="Leninsky Prospekt 42, kv. 17, Moscow 119334", country="Russian Federation",
    ))

    # 19 ── Kwame Mensah: forged passport, escalated as Fraud ID, rejected at L2 (fraud tracker row)
    kyc.append(kyc_case(
        key="mensah", name="Kwame Mensah", nationality="GH", dob="1990-08-14", id_type="Passport", issuing="GH",
        tags=["Tier 2"], manual_review_reason=["Vendor rejected"], created_hours_ago=70, status="Rejected", confirmed=False,
        vendor_result="Declined", vendor_reason="Security-feature check failed: data-page font inconsistent with the specimen.",
        escalated_hours_after=1.5, closed_hours_after=9.0, l1=[JON], l2=[NISHA], previous_reviewer=NISHA,
        escalate_reason="Fraud ID / AI-generated document", fraud_typology="Forged or altered document",
        handoffs=[handoff("L1", "L2", dates.add_hours(dates.hours_ago(70), 1.5), NISHA, 3.0, "bot")],
        docs=[doc("passport", "front"), doc("passport", "back")],
        address="14 Ring Road East, Accra", country="Ghana",
        extra_audit=[event(dates.add_hours(dates.hours_ago(70), 9.0), NISHA, "L2 decision: Reject",
                           "Forged data page confirmed against the specimen library; account blocked, fraud tracker updated.")],
    ))

    return kyc, alerts, wc
