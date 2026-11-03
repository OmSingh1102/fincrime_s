"""End-to-end flow tests on the Flask test client.

The state store is pointed at a temp file BEFORE app.py is imported — the
import restores stored state, so importing first would read the real
fincrime_state.db beside app.py.

Run:  .venv\\Scripts\\python.exe -m unittest -v
"""
import html
import io
import os
import re
import sys
import tempfile
import unittest
import zipfile
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DB = os.path.join(tempfile.mkdtemp(prefix="fcc-test-"), "state.db")
os.environ["FCC_STATE_DB"] = _DB
os.environ["CONSOLE_CASES"] = "demo"      # the scripted book these tests are written against
from fincrime.data.state_store import StateStore  # noqa: E402
StateStore.path = _DB
import app as fcc  # noqa: E402

AMARA, JON, MARIO, NISHA, RYAN, LEAH = ("amara.iyer@straive.com", "jon.miranda@straive.com", "mario.kim@straive.com",
                                        "nisha.rao@straive.com", "ryan.lee@straive.com", "leah.fernandez@straive.com")


def flash_text(response):
    return " | ".join(re.findall(r'role="status"[^>]*>\s*<span[^>]*>[^<]*</span>\s*<span>([^<]+)</span>',
                                 response.data.decode("utf-8")))


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.found.append(dict(attrs))


def anchors(response):
    """Every <a> on the page as an attribute dict."""
    p = _Anchors()
    p.feed(response.data.decode("utf-8"))
    return p.found


def new_tab(a):
    return a.get("target") == "_blank" and "noopener" in (a.get("rel") or "")


class FlowTestCase(unittest.TestCase):

    def setUp(self):
        fcc.app.testing = True
        fcc.app.secret_key = "test"
        StateStore.clear()
        fcc._load_seed()
        self.c = fcc.app.test_client()
        self.K = {k["key"]: k for k in fcc.KYC_CASES if k.get("key")}

    # helpers ---------------------------------------------------------------
    def login(self, key):
        r = self.c.post("/login", data={"analyst": key, "password": "1234"})
        self.assertEqual(r.status_code, 302)

    def post(self, path, **data):
        return self.c.post(path, data=data, follow_redirects=True)

    def confirm(self, case, poa_ok="1"):
        self.post(f"/kyc/{case['id']}/reserve")
        c = case["client"]
        return self.post(f"/kyc/{case['id']}/data_confirm", name=c["legal_name"], name_ok="1", dob=c["dob"], dob_ok="1",
                         nationality=c["nationality"], nationality_ok="1", poa_ok=poa_ok, summary="Checked against the document.")

    def escalate(self, key, case, reason="Non-US person with potential match", comment="Unresolved records.", **extra):
        self.login(key)
        self.post(f"/kyc/{case['id']}/reserve")
        return self.post(f"/kyc/{case['id']}/escalate", reason=reason, comment=comment, **extra)

    # phases 0-7 ------------------------------------------------------------
    def test_login_and_gate(self):
        self.assertEqual(self.c.get("/kyc/list").status_code, 302)          # signed out → portal
        r = self.c.post("/login", data={"analyst": "amara.iyer", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.login("amara.iyer")
        self.assertEqual(self.c.get("/kyc/list").status_code, 200)
        self.assertEqual(self.c.get("/state/version").get_json()["v"], fcc.STATE_VERSION)

    def test_screens_are_client_neutral(self):
        """No name from the source tool on the console's screens, for either
        tenant; stored records with the old names are relabelled on load."""
        old = ("TikTok", "TT Live", "TT Shop", "Gifter", "Creator G", "US-TTP", "Utility Hub", "Live Standard KYC")
        pages = ["/", "/login"]
        self.login("mario.kim")
        m = self.K["mercer"]
        pages += ["/kyc/list", f"/kyc/detail/{m['id']}", f"/name_screening/case/{m['screening']['alert_case_id']}",
                  "/name_screening/queue", "/trackers", "/ai-screening", "/analytics", "/work", "/queue/l2"]
        for tenant in ("ttlive", "ttshop"):
            self.c.post("/tenant", data={"tenant": tenant, "next": "/kyc/list"})
            for path in pages:
                r = self.c.get(path)
                if path in ("/", "/login") and r.status_code == 302:         # signed in: sent on to the console
                    continue
                self.assertEqual(r.status_code, 200, path)
                body = r.data.decode("utf-8")
                for word in old:
                    self.assertNotIn(word, body, f"{word!r} on {path} ({tenant})")
        self.assertIn("Individuals", self.c.get("/kyc/list").data.decode("utf-8"))
        stored = {"tags": ["Gifter G6", "Agency"], "source": "Live Standard KYC", "note": "Agency",
                  "screening": {"wc_group": "TTLIVE_KYC_name_screening_prod"}, "role_label": "L1 · TT Live, TT Shop"}
        self.assertEqual(fcc.relabel([stored]), 5)
        self.assertEqual(stored, {"tags": ["Tier 1", "Introducer"], "source": "Standard KYC", "note": "Agency",
                                  "screening": {"wc_group": "RETAIL_KYC_name_screening_prod"},
                                  "role_label": "L1 · Individuals, Businesses"})

    def test_golden_path(self):
        self.login("amara.iyer")
        m = self.K["mercer"]
        aid = m["screening"]["alert_case_id"]
        alert, wc = fcc.ALERT_BY_ID[aid], fcc.WC_BY_ID[aid]
        audit_len = len(m["audit"])

        r = self.confirm(m)
        self.assertEqual(m["status"], "DataScreening", flash_text(r))
        self.assertTrue(m["verification_points"][2]["at"])
        self.assertIn(AMARA, m["reviewers_by_level"]["L1"])
        self.assertEqual(m["confirmations"]["poa"]["confirmed_by"], AMARA)
        self.assertGreater(len(m["audit"]), audit_len)
        audit_len = len(m["audit"])

        # The AI screening rules cleared six of the seven records at intake;
        # the seventh lists no date of birth, so it waits for Amara.
        self.assertEqual(sum(1 for a in alert["alerts"] if a["resolved_by"] == "System User"), 6)
        self.assertEqual(m["screening"]["workflow"], "human-in-the-loop")
        self.post(f"/name_screening/{aid}/handle")
        self.assertEqual(alert["status"], "Handling")
        unresolved = next(x for x in wc["matches"] if x["resolution"]["status"] == "Unresolved")
        r = self.post(f"/wc/{aid}/match/{unresolved['id']}/resolve", status="False positive", risk="Low",
                      reason="Location mismatch", comment="Different person: a West Midlands record; the client is a US resident with no UK footprint.")
        self.assertEqual(unresolved["resolution"]["by"], AMARA, flash_text(r))
        self.post(f"/wc/{aid}/export")
        self.assertEqual(len(wc["exports"]), 1)
        pdf = self.c.get(f"/wc/{aid}/export.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

        pending = next(a for a in alert["alerts"] if a["outcome"] == "Pending")
        r = self.post(f"/name_screening/{aid}/alert_outcome", alert_id=pending["id"], review_mode="Manual Review",
                      result_label="False Positive", rationale="Different Person - Public Records", comment="Not the same person.", risk_level="Low")
        self.assertEqual(pending["outcome"], "False Positive", flash_text(r))
        page = self.c.get(f"/name_screening/case/{aid}").data
        self.assertIn("Approve - False Positive · Recommended".encode("utf-8"), page)
        r = self.post(f"/name_screening/{aid}/case_outcome", outcome="Approve - False Positive", comment="All FP.")
        self.assertEqual(alert["status"], "Closed", flash_text(r))

        r = self.post(f"/kyc/{m['id']}/edd_review", decision="False positive", summary="FP-Approve.",
                      attachment=wc["exports"][0]["filename"])
        self.assertEqual(m["status"], "Verified", flash_text(r))
        self.assertEqual(m["previous_reviewer"], AMARA)
        self.assertEqual(m["closed_by"], AMARA)
        self.assertEqual(m["current_reviewer"], "")
        self.assertEqual(m["review_decisions"][4]["report"], wc["exports"][0]["filename"])
        self.assertGreater(len(m["audit"]), audit_len)
        # A closed case that was never escalated shows five verification points.
        self.assertEqual(len(fcc.workflow.verification_points(m)), 5)
        # The work log closed Amara's entry with the outcome.
        self.assertEqual(m["work"][-1]["analyst"], AMARA)
        self.assertTrue(m["work"][-1]["to"])

    def test_edd_review_refused_while_alerts_pending(self):
        self.login("amara.iyer")
        m = self.K["mercer"]
        self.confirm(m)
        r = self.post(f"/kyc/{m['id']}/edd_review", decision="False positive", summary="x", attachment="x.pdf")
        self.assertEqual(m["status"], "DataScreening")
        self.assertIn("pending", flash_text(r).lower())

    def test_mandatory_comment(self):
        self.login("amara.iyer")
        sd = self.K["sidorov"]
        self.post(f"/kyc/{sd['id']}/reserve")
        r = self.post(f"/kyc/{sd['id']}/escalate", reason="Other", comment="   ")
        self.assertEqual(sd["status"], "DataConfirming")
        self.assertIn("comment is required", flash_text(r).lower())
        aid = sd["screening"]["alert_case_id"]
        self.post(f"/name_screening/{aid}/handle")
        wc = fcc.WC_BY_ID[aid]
        mid = wc["matches"][0]["id"]
        self.post(f"/wc/{aid}/match/{mid}/resolve", status="True positive", risk="High", reason="Confirmed by identification", comment="")
        self.assertEqual(wc["matches"][0]["resolution"]["status"], "Unresolved")

    def test_confirm_flags_required(self):
        self.login("amara.iyer")
        m = self.K["mercer"]
        self.post(f"/kyc/{m['id']}/reserve")
        r = self.post(f"/kyc/{m['id']}/data_confirm", name=m["client"]["legal_name"], name_ok="1",
                      dob=m["client"]["dob"], dob_ok="0", nationality="US", nationality_ok="1", poa_ok="1")
        self.assertEqual(m["status"], "DataConfirming")
        self.assertIn("Birthday", flash_text(r))

    def test_resubmit(self):
        self.login("amara.iyer")
        n = self.K["nguyen"]
        self.post(f"/kyc/{n['id']}/reserve")
        self.post(f"/kyc/{n['id']}/resubmit", decision="Resubmit ID", id_reason="Cropped")
        self.assertEqual(n["status"], "ResubmitID")
        self.assertEqual(n["review_decisions"][3]["fail_reason"], "Cropped")
        self.assertEqual(n["current_reviewer"], "")

    def test_segregation(self):
        self.login("mario.kim")
        h = self.K["hassan"]
        self.post(f"/kyc/{h['id']}/reserve")
        self.post(f"/kyc/{h['id']}/escalate", reason="Non-US person with potential match", comment="Unresolved records.")
        self.assertEqual(h["status"], "DataEscalatingL2")
        queue = self.c.get("/queue/l2").data.decode("utf-8")
        self.assertNotIn(h["id"], queue)                                   # hidden from Mario at L2
        self.post(f"/kyc/{h['id']}/reserve")
        r = self.post(f"/kyc/{h['id']}/l2_decision", decision="Approve", comment="fine")
        self.assertEqual(h["status"], "DataEscalatingL2")
        self.assertIn("earlier level", flash_text(r))
        self.login("nisha.rao")
        self.assertIn(h["id"], self.c.get("/queue/l2").data.decode("utf-8"))  # visible to Nisha

    def test_l2_and_edd_decisions(self):
        md = self.K["mendoza"]
        self.login("nisha.rao")
        self.post(f"/kyc/{md['id']}/reserve")
        self.post(f"/kyc/{md['id']}/l2_decision", decision="Escalate to EDD", comment="SoW review.")
        self.assertEqual(md["pending_reason"], "Pending EDD")
        self.assertEqual(md["case_review_level"], "L3")
        self.login("ryan.lee")
        self.post(f"/kyc/{md['id']}/reserve")
        self.post(f"/kyc/{md['id']}/l2_decision", decision="Approve", comment="SoW verified.")
        self.assertEqual(md["status"], "Verified")
        self.assertIn(RYAN, md["reviewers_by_level"]["EDD"])
        self.assertEqual(md["closed_by"], RYAN)

    def test_skopenow_path(self):
        self.login("amara.iyer")
        p = self.K["petrova"]
        self.post(f"/kyc/{p['id']}/reserve")
        self.post(f"/kyc/{p['id']}/edd_review", decision="Request Skopenow report", summary="US person, medium risk.")
        self.assertEqual(p["pending_reason"], "Pending Skopenow")
        self.post(f"/kyc/{p['id']}/skopenow", outcome="Cleared", notes="All four comparisons distinct.")
        self.assertEqual(p["skopenow"]["report"]["sections"][0]["match"], "Consistent")
        self.assertIn("Seattle", p["skopenow"]["report"]["sections"][2]["text"])
        self.assertEqual(self.c.get(f"/kyc/{p['id']}/skopenow").status_code, 200)
        self.post(f"/kyc/{p['id']}/edd_review", decision="Approve after Skopenow", summary="Cleared.")
        self.assertEqual(p["status"], "Verified")

    def test_skopenow_only_for_us_persons(self):
        self.login("jon.miranda")
        y = self.K["yusuf"]
        self.post(f"/kyc/{y['id']}/reserve")
        r = self.post(f"/kyc/{y['id']}/edd_review", decision="Request Skopenow report", summary="x")
        self.assertEqual(y["status"], "DataScreening")
        self.assertIn("US persons", flash_text(r))

    def test_ttshop_recalculate_risk(self):
        self.login("jon.miranda")
        self.c.post("/tenant", data={"tenant": "ttshop", "next": "/kyc/list"})
        v = self.K["vanguard"]
        r = self.post(f"/kyc/{v['id']}/recalculate_risk")
        self.assertIn(v["risk_rating"], ("Medium", "High"), flash_text(r))
        listing = self.c.get("/kyc/list").data.decode("utf-8")
        self.assertIn(v["id"], listing)
        self.assertNotIn(self.K["mercer"]["id"], listing)                  # tenant scope

    def test_documents(self):
        self.login("amara.iyer")
        r = self.c.get(f"/documents/{self.K['sidorov']['id']}/d1.svg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/svg+xml")
        self.assertIn(b"SPECIMEN", r.data)
        self.assertIn("Анатолий".encode("utf-8"), r.data)
        cropped = self.c.get(f"/documents/{self.K['nguyen']['id']}/d1.svg").data
        self.assertNotIn(b'viewBox="0 0 640 400"', cropped)
        poa = self.c.get(f"/documents/{self.K['sidorov']['id']}/d3.svg").data.decode("utf-8")
        self.assertIn("Cyprus", poa)
        self.assertIn(self.K["sidorov"]["documents"][2]["issued_on"][:4], poa)

    def test_world_check_links_open_new_tab(self):
        """Links into the World-Check console open a new tab, and so does the
        Screening Result's Details button on the case page (the client asked
        for that one); the CMS's other links, the audit log and the rest keep
        the same tab."""
        def hrefs(page, *parts):
            return [a for a in page if any(x in (a.get("href") or "") for x in parts)]

        t = self.K["taylor"]
        aid = t["screening"]["alert_case_id"]
        self.login("amara.iyer")
        cms = anchors(self.c.get(f"/name_screening/case/{aid}"))
        to_wc = hrefs(cms, "/wc/")
        self.assertGreaterEqual(len(to_wc), 2)          # the console link and the per-match button
        self.assertTrue(all(new_tab(a) for a in to_wc), [a["href"] for a in to_wc if not new_tab(a)])
        popup = [a for a in cms if a.get("id") == "ao-wc-link"]   # its address is set by the page script
        self.assertTrue(popup and new_tab(popup[0]))
        others = [a.get("href") for a in cms if new_tab(a) and "/wc/" not in (a.get("href") or "") and a.get("id") != "ao-wc-link"]
        self.assertEqual(others, [])
        self.assertFalse(any(new_tab(a) for a in hrefs(cms, "/kyc/detail/")))

        case_page = anchors(self.c.get(f"/kyc/detail/{t['id']}"))
        details = hrefs(case_page, "/name_screening/case/")
        self.assertTrue(details and all(new_tab(a) for a in details))          # the Details buttons
        other = [a.get("href") for a in case_page if new_tab(a) and "/name_screening/case/" not in (a.get("href") or "")
                 and "/documents/" not in (a.get("href") or "") and a.get("href")]
        self.assertEqual(other, [])                                              # nothing else on the case page

        listing = hrefs(anchors(self.c.get("/kyc/list")), "/kyc/detail/")
        self.assertTrue(listing and all(new_tab(a) for a in listing))          # the case list's Details buttons

        audit = anchors(self.c.get("/audit"))
        self.assertTrue(hrefs(audit, "/wc/") and all(new_tab(a) for a in hrefs(audit, "/wc/")))
        self.assertFalse(any(new_tab(a) for a in hrefs(audit, "/name_screening/case/", "/kyc/detail/")))

        self.assertEqual([a.get("href") for a in anchors(self.c.get(f"/wc/{aid}")) if new_tab(a)], [])

    def test_an_alert_row_opens_to_its_world_check_record(self):
        """Each alert row on the screening case opens to the World-Check
        record behind it, as the real case has it: Smart Review Results (the
        rules' verdicts said out loud), Overview, Others. The first is open."""
        from fincrime.data.seed import wc_match
        from fincrime.engines import ai_screening as ai
        client = {"dob": "1988-02-11", "gender": "M", "nationality": "US"}
        every = {"dob_mismatch": True, "gender_mismatch": True, "nationality_mismatch": True}
        verdicts = lambda m, **kw: {r["label"]: r["verdict"] for r in ai.review_results(m, client, enabled=kw.get("en", every), guards={})}
        self.assertEqual(verdicts(wc_match(key="r1", name="X", dobs=["1962"], gender="Male"))["DOB mismatch"], "fires")
        same = verdicts(wc_match(key="r2", name="X", dobs=["11-Feb-1988"], gender="Female"))
        self.assertEqual((same["DOB mismatch"], same["Gender mismatch"]), ("vetoed", "vetoed"))   # the exact DOB stops every rule
        self.assertEqual(verdicts(wc_match(key="r3", name="X", dobs=["1988"], gender="Male"),
                                  en=dict(every, nationality_mismatch=False))["Nationality mismatch"], "off")

        self.login("amara.iyer")
        t = self.K["taylor"]
        aid = t["screening"]["alert_case_id"]
        page = self.c.get(f"/name_screening/case/{aid}").data.decode("utf-8")
        n = len(fcc.ALERT_BY_ID[aid]["alerts"])
        self.assertEqual((page.count('<tr class="alert-detail hidden"'), page.count('class="row-toggle"')), (n, n))
        for needle in ("Smart Review Results", "Overview", "Others", "Key Data", "Basic Info", "Record Update", "Date Details",
                       "Location Details", "Identification Numbers", "Aliases", "Further Information", "[BIOGRAPHY]", "[REPORTS]",
                       "Keywords", "AI screening rules", "Submitted against matched data"):
            self.assertIn(needle, page, needle)
        m = fcc.worldcheck.find_match(fcc.WC_BY_ID[aid], fcc.ALERT_BY_ID[aid]["alerts"][0]["match_id"])
        self.assertIn(html.escape(m["further_info"]["biography"]), page)
        self.assertEqual(page.count('class="open"'), 1)                              # the first alert starts open
        # Open in WC stays at the table's right edge, as the case list's Details does
        self.assertIn('<th class="sticky-right">World-Check</th>', page)
        self.assertEqual(page.count('<td class="sticky-right"><a class="btn btn-outline btn-xs nowrap" href="/wc/'), n)

    def test_ai_rules_evaluate(self):
        """The rule book on synthetic records: fires only on present,
        conflicting data; the exact date of birth vetoes everything."""
        from fincrime.data.seed import wc_match
        from fincrime.engines import ai_screening as ai
        client = {"dob": "1988-02-11", "gender": "M", "nationality": "US"}
        every = {"dob_mismatch": True, "gender_mismatch": True, "nationality_mismatch": True}

        def rec(**kw):
            kw.setdefault("gender", "Male")
            return wc_match(key="t-" + str(len(kw)), name="X", **kw)

        self.assertEqual(ai.evaluate(rec(dobs=["1962"]), client)[0], "dob_mismatch")
        self.assertEqual(ai.evaluate(rec(dobs=["16-Aug-1960"]), client)[0], "dob_mismatch")
        self.assertEqual(ai.evaluate(rec(dobs=["1988"]), client)[0], "")                 # same year, year only
        self.assertEqual(ai.evaluate(rec(dobs=["1973", "1988"]), client)[0], "")         # one alternative matches
        self.assertEqual(ai.evaluate(rec(dobs=["11-Feb-1988"], gender="Female", citizenship="Canada"), client, enabled=every)[0], "")
        self.assertEqual(ai.evaluate(rec(dobs=[]), client)[0], "")
        self.assertEqual(ai.evaluate(rec(dobs=["1962"]), {"dob": "", "gender": "", "nationality": "SG"})[0], "")
        self.assertEqual(ai.evaluate(rec(dobs=["1988"], gender="Female"), client)[0], "gender_mismatch")
        self.assertEqual(ai.evaluate(rec(dobs=[], gender="Female"), {"dob": "", "gender": "", "nationality": "SG"})[0], "")
        san = rec(dobs=["1962"], types=("SAN",))
        self.assertEqual(ai.evaluate(san, client, guards={"san_review": True})[0], "")
        self.assertEqual(ai.evaluate(san, client, guards={"san_review": False})[0], "dob_mismatch")
        self.assertEqual(ai.evaluate(rec(dobs=["1962"]), client, enabled={"dob_mismatch": False, "gender_mismatch": True})[0], "")
        self.assertEqual(ai.evaluate(rec(dobs=["1988"], citizenship="United Kingdom"), client, enabled=every)[0], "nationality_mismatch")
        self.assertEqual(ai.evaluate(rec(dobs=["1988"], citizenship="United States; Canada"), client, enabled=every)[0], "")
        done = rec(dobs=["1962"])
        done["resolution"].update(status="True positive", by=AMARA)
        self.assertEqual(ai.evaluate(done, client)[0], "")

    def test_ai_rules_hold_a_transposed_date_of_birth(self):
        """A listed date that is the client's own with day and month swapped
        is a keying slip, not a different birthday: never cleared."""
        from fincrime.engines import ai_screening as ai
        self.assertEqual(ai.dob_state("1985-09-06", ["1985-06-09"]), "unknown")
        self.assertEqual(ai.dob_state("1985-09-06", ["06-Sep-1985"]), "same")
        self.assertEqual(ai.dob_state("1985-09-06", ["1985-06-10"]), "conflict")         # not the swap
        self.assertEqual(ai.dob_state("1985-09-06", ["1986-06-09"]), "conflict")         # another year
        self.assertEqual(ai.dob_state("1985-09-06", ["1985-06-09", "1962-01-01"]), "unknown")
        self.assertEqual(ai.dob_state("1985-05-05", ["1985-05-05"]), "same")             # day == month
        from fincrime.data.seed import wc_match
        record = wc_match(key="t-swap", name="X", dobs=["1985-06-09"], gender="")
        self.assertEqual(ai.evaluate(record, {"dob": "1985-09-06", "gender": "F", "nationality": "IN"})[0], "")

    def test_seed_workflows(self):
        """What the rules did at intake, case by case."""
        m = self.K["mercer"]
        alert, wc = fcc.ALERT_BY_ID[m["screening"]["alert_case_id"]], fcc.WC_BY_ID[m["screening"]["alert_case_id"]]
        self.assertEqual(sum(1 for a in alert["alerts"] if a["resolved_by"] == "System User"), 6)
        self.assertEqual(sum(1 for a in alert["alerts"] if a["outcome"] == "Pending"), 1)
        self.assertEqual(m["screening"]["workflow"], "human-in-the-loop")
        ai = m["screening"]["ai"]
        self.assertEqual((ai["total"], ai["auto_resolved"], ai["for_review"], ai["rules"]), (7, 6, 1, {"dob_mismatch": 6}))
        self.assertEqual(sum(1 for e in wc["audit"] if e["action"] == "AI screening rules applied"), 1)
        self.assertEqual(m["review_decisions"][1]["by"], "System User")
        self.assertEqual(alert["status"], "Init")
        self.assertEqual(m["audit"], sorted(m["audit"], key=lambda e: e["at"]))

        hw = fcc.WC_BY_ID[self.K["hassan"]["screening"]["alert_case_id"]]
        statuses = [x["resolution"]["status"] for x in hw["matches"]]
        self.assertEqual((statuses.count("False positive"), statuses.count("Unresolved")), (209, 3))

        t = self.K["taylor"]
        ta, tw = fcc.ALERT_BY_ID[t["screening"]["alert_case_id"]], fcc.WC_BY_ID[t["screening"]["alert_case_id"]]
        self.assertEqual(t["status"], "DataConfirming")
        self.assertEqual(t["screening"]["workflow"], "auto-verified")
        self.assertEqual(ta["status"], "Closed")
        self.assertEqual(ta["case_outcome"]["by"], "System User")
        self.assertEqual(ta["case_outcome"]["attachment"], tw["exports"][0]["filename"])
        self.assertEqual(tw["exports"][0]["by"], "System User")
        self.assertEqual(sorted(a["smart_strategy"] for a in ta["alerts"]), ["DOB mismatch", "Gender mismatch"])
        self.assertTrue(t["screening"]["ai"]["auto_closed"])

        s = self.K["solis"]
        self.assertEqual(s["screening"]["workflow"], "auto-verified")
        self.assertEqual(s["screening"]["ai"]["total"], 0)
        self.assertEqual(s["review_decisions"][1]["by"], "System User")

        g = self.K["gomez"]
        self.assertEqual(fcc.WC_BY_ID[g["screening"]["alert_case_id"]]["matches"][0]["resolution"]["by"], AMARA)
        self.assertEqual(g["screening"]["workflow"], "human-in-the-loop")
        for key in ("vanguard", "apex", "sidorov", "rahman", "petrova"):
            rows = fcc.ALERT_BY_ID[self.K[key]["screening"]["alert_case_id"]]["alerts"]
            self.assertTrue(all(a["outcome"] == "Pending" for a in rows), key)

    def test_taylor_auto_verifies_on_confirm(self):
        """The auto-verified workflow end to end: the rules cleared both of
        Taylor's records at intake; confirming his data completes the case
        with no analyst step, and Amara keeps the credit."""
        self.login("amara.iyer")
        t = self.K["taylor"]
        aid = t["screening"]["alert_case_id"]
        wc = fcc.WC_BY_ID[aid]
        load_before = fcc.routing.load(AMARA, fcc.KYC_CASES)
        r = self.confirm(t)
        self.assertEqual(t["status"], "Verified", flash_text(r))
        self.assertIn("auto-verified", flash_text(r))
        self.assertEqual(t["closed_by"], "System User")
        self.assertEqual(t["previous_reviewer"], AMARA)
        self.assertEqual(t["current_reviewer"], "")
        self.assertEqual(t["reviewers_by_level"]["L1"], [AMARA])
        row = t["review_decisions"][4]
        self.assertEqual((row["decision"], row["by"], row["report"]), ("False positive", "System User", wc["exports"][0]["filename"]))
        self.assertEqual(len(fcc.workflow.verification_points(t)), 5)
        self.assertEqual((t["work"][-1]["analyst"], t["work"][-1]["outcome"]), (AMARA, "Auto-verified"))
        self.assertEqual((t["audit"][-1]["action"], t["audit"][-1]["actor"]), ("Case verified", "System User"))
        self.assertEqual(fcc.routing.load(AMARA, fcc.KYC_CASES), load_before - 1)
        self.assertNotIn(t, fcc.qc.pool(fcc.KYC_CASES, "ttlive"))
        page = self.c.get("/ai-screening").data.decode("utf-8")
        self.assertIn("MICHAEL JAMES TAYLOR", page)
        self.assertIn("Cases closed by the rules", page)
        # A re-screen that finds nothing new writes no rules row of its own.
        self.assertEqual(sum(1 for e in wc["audit"] if e["action"] == "AI screening rules applied"), 1)

    def test_ttshop_never_auto_closes(self):
        """A seller keeps the human close: Data Confirm labels it, Checker 1
        still recalculates the risk and approves, Checker 2 still signs."""
        self.login("jon.miranda")
        self.c.post("/tenant", data={"tenant": "ttshop", "next": "/kyc/list"})
        f = self.K["filler-shop-3"]                                        # DataConfirming, no hit
        self.post(f"/kyc/{f['id']}/reserve")
        c = f["client"]
        r = self.post(f"/kyc/{f['id']}/data_confirm", name=c["legal_name"], name_ok="1", id_number=c["id_number"], id_number_ok="1",
                      nationality=c["nationality"], nationality_ok="1", poa_ok="1", summary="Registry checked.")
        self.assertEqual(f["status"], "DataScreening", flash_text(r))
        self.assertEqual(f["screening"]["workflow"], "auto-verified")
        self.assertIn("approve the business", flash_text(r))
        self.post(f"/kyc/{f['id']}/recalculate_risk")
        r = self.post(f"/kyc/{f['id']}/edd_review", decision="No match - Approve", summary="Clean seller.")
        self.assertEqual(f["pending_reason"], "Pending Checker 2", flash_text(r))

    def test_ai_rules_toggle_persists(self):
        """The team lead switches the nationality rule on; it survives a
        restart, changes the next re-screen, and reset puts the defaults back."""
        self.login("amara.iyer")
        r = self.c.post("/ai-screening/rules", data={"key": "nationality_mismatch", "enabled": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(fcc.AI_SETTINGS["enabled"]["nationality_mismatch"])   # L1 cannot switch rules
        self.login("leah.fernandez")
        r = self.post("/ai-screening/rules", key="nationality_mismatch", enabled="1")
        self.assertTrue(fcc.AI_SETTINGS["enabled"]["nationality_mismatch"], flash_text(r))
        self.assertEqual(fcc.AI_SETTINGS["history"][-1]["by"], LEAH)
        self.assertIn("Nationality mismatch: off", flash_text(r))
        fcc._load_seed()                                                   # simulated restart
        self.assertTrue(fcc.AI_SETTINGS["enabled"]["nationality_mismatch"])
        self.assertTrue(fcc.ai_screening.rules_enabled()["nationality_mismatch"])
        r = self.post("/ai-screening/rules", key="no_such_rule", enabled="1")
        self.assertIn("Unknown rule", flash_text(r))
        # Mercer's seventh record is a UK national with no listed date of birth:
        # with the nationality rule on, the re-screen clears it and the case
        # verifies itself instead of waiting for Amara.
        self.login("amara.iyer")
        m = fcc.KYC_BY_ID[self.K["mercer"]["id"]]
        alert = fcc.ALERT_BY_ID[m["screening"]["alert_case_id"]]
        r = self.confirm(m)
        self.assertEqual(m["status"], "Verified", flash_text(r))
        self.assertEqual(m["closed_by"], "System User")
        self.assertEqual(alert["status"], "Closed")
        self.assertEqual(sorted({a["smart_strategy"] for a in alert["alerts"]}), ["DOB mismatch", "Nationality mismatch"])
        self.assertEqual(m["screening"]["ai"]["last_run"], {"run": "rescreen", "at": m["closed_at"], "resolved": 1})
        self.post("/reset")
        self.assertFalse(fcc.AI_SETTINGS["enabled"]["nationality_mismatch"])
        self.assertEqual(fcc.AI_SETTINGS["history"], [])
        self.assertEqual(StateStore.count(), 0)

    def test_workflow_shown_across_screens(self):
        """The two workflows are visible wherever a case is listed."""
        self.login("amara.iyer")
        t, m = self.K["taylor"], self.K["mercer"]
        self.assertIn(b"Auto-verified", self.c.get(f"/kyc/detail/{t['id']}").data)
        page = self.c.get(f"/kyc/detail/{m['id']}").data.decode("utf-8")
        self.assertIn("Human-in-the-loop", page)
        self.assertIn("6 of 7 match(es) auto-resolved", page)
        listing = self.c.get("/kyc/list").data.decode("utf-8")
        self.assertIn('data-workflow="auto-verified"', listing)
        self.assertIn('data-workflow="human-in-the-loop"', listing)
        self.assertIn('id="f-workflow"', listing)
        cms = self.c.get(f"/name_screening/case/{t['screening']['alert_case_id']}").data.decode("utf-8")
        self.assertIn("Auto-resolved by the AI screening rules", cms)
        self.assertIn("<option>Gender mismatch</option>", cms)
        cms = self.c.get(f"/name_screening/case/{m['screening']['alert_case_id']}").data.decode("utf-8")
        self.assertIn("1 for review", cms)
        queue = self.c.get("/name_screening/queue").data.decode("utf-8")
        self.assertIn("Auto-resolved :", queue)
        self.assertIn('title="closed by the AI screening rules">auto</span>', queue)
        wc = self.c.get(f"/wc/{m['screening']['alert_case_id']}/match/{fcc.WC_BY_ID[m['screening']['alert_case_id']]['matches'][0]['id']}").data
        self.assertIn(b"AI screening rule", wc)
        analytics = self.c.get("/analytics").data.decode("utf-8")
        self.assertIn("AI screening rules fired", analytics)
        self.assertIn("Auto-verified vs human-in-the-loop", analytics)

    def test_case_list_filters_collapsible(self):
        """The filter panel on the case list folds behind an arrow, keeps its
        buttons row, and remembers the fold in the browser."""
        self.login("amara.iyer")
        page = self.c.get("/kyc/list").data.decode("utf-8")
        for needle in ('id="filters-toggle"', 'aria-controls="filters-grid"', 'id="filters-grid"',
                       'id="filters-summary"', "fcc-case-filters-folded", "toggleFilters"):
            self.assertIn(needle, page, needle)
        self.assertLess(page.index('id="filters-toggle"'), page.index('id="filters-grid"'))
        self.assertLess(page.index('id="filters-grid"'), page.index('id="sort-btn"'))
        # folded, the first six filters stay (the real list's first two rows): only the rest hide
        css = self.c.get("/static/css/style.css").data.decode("utf-8")
        self.assertIn(".filters-card.folded #filters-grid .f:nth-child(n+7) { display: none; }", css)
        self.assertNotIn(".filters-card.folded #filters-grid { display: none; }", css)
        grid = page[page.index('id="filters-grid"'):]
        first = [grid.index(x) for x in ('id="f-case-id"', 'id="f-client-id"', 'id="f-name"', 'id="f-dob-from"',
                                         'id="f-status-box"', 'id="f-created-from"', 'id="f-reviewer"')]
        self.assertEqual(first, sorted(first))

    def test_case_list_page_size_and_frozen_details(self):
        """The real list's "N / Page" menu (10 by default, the choice kept
        for the session) and a Details column frozen at the right edge."""
        self.login("amara.iyer")
        page = self.c.get("/kyc/list").data.decode("utf-8")
        self.assertEqual(page.count("<tr data-case-id="), 10)
        self.assertIn('id="page-size"', page)
        for n in (10, 20, 30, 40, 50, 100):
            self.assertIn(f'<option value="{n}"', page)
        self.assertIn('<option value="10" selected>10 / Page</option>', page)
        self.assertEqual(self.c.get("/kyc/list?per_page=20").data.decode("utf-8").count("<tr data-case-id="), 20)
        later = self.c.get("/kyc/list?status=DataConfirming").data.decode("utf-8")    # the size holds: Search, the menu link
        self.assertIn('<option value="20" selected>20 / Page</option>', later)
        self.assertEqual(self.c.get("/kyc/list?per_page=7").data.decode("utf-8").count("<tr data-case-id="), 20)   # not a choice
        self.assertIn('<th class="sticky-right">Details</th>', page)
        self.assertEqual(page.count('<td class="sticky-right">'), 10)

    def test_case_list_opens_on_open_cases(self):
        """The front page lists no closed case: with no filter set the list
        holds the open ones only (sort, page size and Priority Review keep
        that), and any filter searches the whole book, verified cases too."""
        self.login("amara.iyer")
        book = [c for c in fcc.KYC_CASES if c["tenant"] == "ttlive"]
        still_open = [c for c in book if c["status"] not in ("Verified", "Rejected")]
        verified = [c for c in book if c["status"] == "Verified"]
        self.assertTrue(still_open and verified)
        for url in ("/kyc/list?per_page=100", "/kyc/list?sort=asc&per_page=100", "/kyc/list?priority=1&per_page=100"):
            page = self.c.get(url).data.decode("utf-8")
            self.assertNotIn('data-status="Verified"', page, url)
            self.assertNotIn('data-status="Rejected"', page, url)
            self.assertIn("Open cases only", page, url)
        self.assertIn(f'Total: <b id="total-count">{len(still_open)}</b>', self.c.get("/kyc/list?per_page=100").data.decode("utf-8"))
        self.assertIn("Open cases only", self.c.get("/", follow_redirects=True).data.decode("utf-8"))   # where sign-in lands
        page = self.c.get("/kyc/list?status=Verified&per_page=100").data.decode("utf-8")
        self.assertEqual(page.count('data-status="Verified"'), len(verified))
        self.assertNotIn("Open cases only", page)
        found = self.c.get(f"/kyc/list?case_id={verified[0]['id']}").data.decode("utf-8")
        self.assertIn(f'data-case-id="{verified[0]["id"]}"', found)                   # a search reaches a verified case

    def test_case_list_filters_and_pages_on_the_server(self):
        """The filters, the sort and the page are query parameters the server
        applies, so a thousand-case book still sends one page (10 rows, or
        what the "N / Page" menu picks)."""
        from werkzeug.datastructures import MultiDict
        from fincrime.analytics.case_list import CaseListConsole, read_query
        self.login("amara.iyer")
        t, m = self.K["taylor"], self.K["mercer"]
        page = self.c.get("/kyc/list?name=taylor").data.decode("utf-8")
        self.assertIn(f'data-case-id="{t["id"]}"', page)
        self.assertNotIn(f'data-case-id="{m["id"]}"', page)
        self.assertIn('value="taylor"', page)                               # the box keeps what was typed
        self.assertIn("1 of", page)
        verified = [c for c in fcc.KYC_CASES if c["tenant"] == "ttlive" and c["status"] == "Verified"]
        page = self.c.get("/kyc/list?status=Verified&status=Rejected&per_page=100").data.decode("utf-8")
        self.assertEqual(page.count('data-status="Verified"'), len(verified))
        self.assertNotIn('data-status="DataConfirming"', page)
        self.assertIn('value="Verified" checked', page)
        self.assertIn("No case matches", self.c.get("/kyc/list?case_id=no-such-case").data.decode("utf-8"))
        vm = CaseListConsole.compute(fcc.KYC_CASES, "ttlive", query=read_query(MultiDict({"page": "2"})), per_page=10)
        self.assertEqual((vm["page"], len(vm["rows"]), vm["first"]), (2, 10, 11))
        self.assertEqual(vm["pages"], -(-vm["shown"] // 10))
        created = [r["created_at"] for r in vm["rows"]]
        self.assertEqual(created, sorted(created, reverse=True))           # latest first by default
        vm = CaseListConsole.compute(fcc.KYC_CASES, "ttlive", query=read_query(MultiDict({"sort": "asc", "page": "99"})), per_page=10)
        self.assertEqual(vm["page"], vm["pages"])                           # past the end: the last page
        created = [r["created_at"] for r in vm["rows"]]
        self.assertEqual(created, sorted(created))
        # the screening queue pages too, and its pager keeps the query string
        per_page, fcc.NS_PER_PAGE = fcc.NS_PER_PAGE, 2
        try:
            queue = self.c.get("/name_screening/queue?level=L1").data.decode("utf-8")
            self.assertIn('aria-current="page">1<', queue)
            self.assertIn("level=L1&amp;page=2", queue)
            self.assertIn('aria-current="page">2<', self.c.get("/name_screening/queue?level=L1&page=2").data.decode("utf-8"))
        finally:
            fcc.NS_PER_PAGE = per_page

    def test_document_viewer_zooms_and_pans(self):
        """The Preview dialog and the Data Confirm viewer share one controller:
        wheel zoom, drag to pan, double-click, zoom / fit / rotate buttons and
        an open-in-new-tab link, with the script rendered once per page."""
        t = self.K["taylor"]
        self.login("amara.iyer")
        self.c.post(f"/kyc/{t['id']}/reserve")
        r = self.c.get(f"/kyc/detail/{t['id']}")
        page = r.data.decode("utf-8")
        for needle in ('id="doc-stage"', 'id="dc-stage"', "addEventListener('wheel'", "addEventListener('pointerdown'",
                       "addEventListener('dblclick'", "setPointerCapture", 'class="viewer-hint"'):
            self.assertIn(needle, page, needle)
        self.assertEqual(page.count("window.viewerInit = function"), 1)          # one script serves both stages
        for act in ("in", "out", "fit", "rotate"):
            self.assertEqual(page.count(f'data-act="{act}"'), 2, act)             # one button strip per stage
        self.assertNotIn("dcZoom", page)
        opens = [a for a in anchors(r) if "data-open" in a]
        self.assertEqual(len(opens), 2)
        self.assertTrue(all(new_tab(a) for a in opens))
        self.assertTrue(any((a.get("href") or "").startswith("/documents/") for a in opens))

    def test_case_page_is_a_sidebar_and_tabs(self):
        """The case page: the customer, progress and every action in a
        sidebar; the content in tabs, all of it in the page (hidden, not
        dropped); one card per document with the same image URL."""
        self.login("amara.iyer")
        m = self.K["mercer"]
        page = self.c.get(f"/kyc/detail/{m['id']}").data.decode("utf-8")
        rail = page[page.index('class="case-rail"'):page.index('class="case-main"')]
        for action in (">Review<", ">Reserve<", ">Release<", ">Assign<", "Change Status", "Modify User Info", "Change Log", "Audit History"):
            self.assertIn(action, rail)
        self.assertIn('class="stepper vertical"', rail)
        for tab in ("overview", "documents", "screening", "decisions"):
            self.assertIn(f'data-tab="{tab}"', page)
            self.assertIn(f'data-panel="{tab}"', page)
        self.assertNotIn('data-tab="agent"', page)                      # no AI Agent read on this case
        self.assertEqual(page.count('class="doc-card"'), len(m["documents"]))
        for d in m["documents"]:
            self.assertIn(f"/documents/{m['id']}/{d['id']}.svg", page)
        self.assertIn('id="vendor-modal"', page)
        self.assertIn("Screening result", page)
        self.assertIn("Review decisions", page)

    def test_review_button_explains_itself(self):
        """A greyed Review button says why in text beside it: nothing to
        review yet, the level the stage needs, who holds the case, or closed."""
        s = self.K["filler-intake-0"]                     # an intake case; only L2 / QC can move it by hand
        def why():
            page = self.c.get(f"/kyc/detail/{s['id']}").data.decode("utf-8")
            m = re.search(r'id="review-why">([^<]*)<', page)
            return m.group(1) if m else ("ENABLED" if "openModal('dc-modal')\">Review" in page else "")

        self.login("amara.iyer")
        self.assertEqual(why(), "Nothing to review at this stage")
        self.login("nisha.rao")
        self.post(f"/kyc/{s['id']}/status", status="DataConfirming", comment="ID verification done.")
        self.assertEqual(why(), "Data Confirming needs L1 — sign in as Amara Iyer, Jon Miranda or Mario Kim")
        self.post(f"/kyc/{s['id']}/reserve")
        self.assertEqual(why(), "Data Confirming needs L1 — sign in as Amara Iyer, Jon Miranda or Mario Kim")
        self.login("amara.iyer")
        self.assertEqual(why(), "Reserved by nisha.rao@straive.com — ask them to release it, or use Assign to Others")
        self.login("nisha.rao")
        self.post(f"/kyc/{s['id']}/release")
        self.login("amara.iyer")
        self.assertEqual(why(), "Reserve the case first (the Reserve button below)")
        self.post(f"/kyc/{s['id']}/reserve")
        self.assertEqual(why(), "ENABLED")
        self.confirm(s)                                    # no hit, an individual: auto-verifies
        self.assertEqual(s["status"], "Verified")
        self.assertEqual(why(), "Case is closed")

    def test_refused_action_stays_on_the_page(self):
        """A refused action leaves the analyst where they were, with the error
        as a notification: Leah (QC) cannot reserve, and lands back on the
        case page rather than the case list. The notification sits in a
        fixed layer, so it shows over a reopened modal."""
        m = self.K["mercer"]
        page = f"/kyc/detail/{m['id']}"
        self.login("leah.fernandez")
        r = self.c.post(f"/kyc/{m['id']}/reserve", headers={"Referer": "http://localhost" + page + "?open=x"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith(page + "?open=x"), r.headers["Location"])
        r = self.c.get(page)
        self.assertEqual(flash_text(r), "That action needs EDD or L1 or L2 access.")
        html = r.data.decode("utf-8")
        for needle in ('id="toasts"', 'class="toast error"', "data-linger=", 'class="toast-x"', "toastClose"):
            self.assertIn(needle, html, needle)
        self.assertEqual(m["current_reviewer"], "")
        r = self.c.post(f"/kyc/{m['id']}/reserve")                                   # no referrer: home
        self.assertTrue(r.headers["Location"].endswith("/kyc/list"), r.headers["Location"])
        r = self.c.post(f"/kyc/{m['id']}/reserve", headers={"Referer": "https://evil.example" + page})
        self.assertTrue(r.headers["Location"].endswith("/kyc/list"), r.headers["Location"])   # another site: home
        # A validation error inside a modal also stays put, modal reopened, message as a toast.
        self.login("amara.iyer")
        self.post(f"/kyc/{m['id']}/reserve")
        r = self.c.post(f"/kyc/{m['id']}/data_confirm", data={"summary": ""})
        self.assertTrue(r.headers["Location"].endswith(page + "?open=dc-modal"), r.headers["Location"])
        html = self.c.get(page + "?open=dc-modal").data.decode("utf-8")
        self.assertIn('class="toast error"', html)

    def test_alert_outcome_multi_select(self):
        """The List Screening rows are check boxes: several alerts can be ticked
        and given one outcome at once, a single alert still works, and none
        or a foreign id is refused before anything is written."""
        h = self.K["hassan"]
        aid = h["screening"]["alert_case_id"]
        self.login("mario.kim")
        page = self.c.get(f"/name_screening/case/{aid}").data.decode("utf-8")
        for needle in ('type="checkbox" name="pick"', 'id="pick-all"', 'id="ao-alert-ids"', 'id="ao-multi"', "afPickAll"):
            self.assertIn(needle, page, needle)
        self.assertNotIn('type="radio" name="pick"', page)
        self.post(f"/name_screening/{aid}/handle")
        a = fcc.ALERT_BY_ID[aid]
        pending = [x for x in a["alerts"] if x["outcome"] == "Pending"]
        self.assertEqual(len(pending), 3)
        two = [pending[0]["id"], pending[1]["id"]]
        r = self.post(f"/name_screening/{aid}/alert_outcome", alert_id=two, review_mode="Manual Review",
                      result_label="False Positive", rationale="Different Person - Public Records",
                      comment="Two namesakes, both cleared.", risk_level="Low")
        self.assertEqual(flash_text(r), "Alert outcome recorded for 2 alerts.")
        done = [x for x in a["alerts"] if x["id"] in two]
        self.assertTrue(all(x["outcome"] == "False Positive" and x["resolved_by"] == MARIO for x in done))
        self.assertEqual(sum(1 for x in a["alerts"] if x["outcome"] == "Pending"), 1)
        self.assertEqual(sum(1 for row in a["audit"] if row["action"] == "Alert outcome: False Positive"), 2)
        r = self.post(f"/name_screening/{aid}/alert_outcome", review_mode="Manual Review", result_label="False Positive",
                      rationale="Different Person - Public Records", comment="x", risk_level="Low")
        self.assertIn("Tick at least one alert", flash_text(r))
        r = self.post(f"/name_screening/{aid}/alert_outcome", alert_id=[pending[2]["id"], "nope"], review_mode="Manual Review",
                      result_label="False Positive", rationale="Different Person - Public Records", comment="x", risk_level="Low")
        self.assertIn("not on this case", flash_text(r))
        self.assertEqual(sum(1 for x in a["alerts"] if x["outcome"] == "Pending"), 1)      # nothing half-written

    def test_model_assessment(self):
        """AI assessment asks the model about the ticked alerts and records a
        verdict on the alert rows and the watchlist records, resolving
        nothing; without a key it is off; a model failure stops the run
        without losing what was assessed; the team lead can switch it off."""
        from fincrime.engines import ai_review
        from fincrime.engines.errors import WorkflowError
        h = self.K["hassan"]
        aid = h["screening"]["alert_case_id"]
        self.login("mario.kim")
        saved = ai_review.KEY, ai_review._call_model
        try:
            ai_review.KEY = ""
            page = self.c.get(f"/name_screening/case/{aid}").data.decode("utf-8")
            self.assertIn("Model assessment is not configured", page)              # the disabled button says why
            self.assertIn("Model assessment is not configured", flash_text(self.post(f"/name_screening/{aid}/ai_assess")))

            ai_review.KEY = "test-key"
            briefs = []

            def fake(text):
                briefs.append(text)
                return ({"verdict": "different person", "confidence": 91, "summary": "Born forty years apart.",
                         "reasons": ["record lists 1962, client 2005"], "missing_data": [],
                         "suggested_outcome": "False Positive"}, {"input": 1, "output": 1, "cached": 0})
            ai_review._call_model = fake
            a = fcc.ALERT_BY_ID[aid]
            pending = [x for x in a["alerts"] if x["outcome"] == "Pending"]
            two = [pending[0]["id"], pending[1]["id"]]
            r = self.post(f"/name_screening/{aid}/ai_assess", alert_id=two)
            self.assertEqual(flash_text(r), f"AI assessment recorded for 2 alert(s) ({ai_review.MODEL}).")
            self.assertEqual(len(briefs), 2)
            self.assertIn("HASSAN", briefs[0])                                        # the brief carries the client
            self.assertIn("system_comparison", briefs[0])
            done = [x for x in a["alerts"] if x["id"] in two]
            self.assertTrue(all(x["ai_assessment"]["verdict"] == "different person"
                                and x["ai_assessment"]["confidence"] == 91 for x in done))
            self.assertTrue(all(x["outcome"] == "Pending" for x in done))            # advisory: nothing resolved
            wc = fcc.WC_BY_ID[aid]
            self.assertEqual(sum(1 for m in wc["matches"] if m.get("ai_assessment")), 2)
            for record in (a, wc, h):
                self.assertEqual(sum(1 for row in record["audit"] if row["action"] == ai_review.ACTION), 2)
            page = self.c.get(f"/name_screening/case/{aid}").data.decode("utf-8")
            self.assertIn("different person · 91%", page)
            self.assertIn('data-ai-outcome="False Positive"', page)
            self.assertIn('id="ao-ai"', page)
            self.assertEqual(self.c.get("/ai-screening").status_code, 200)

            def boom(text):
                raise WorkflowError("gateway down")
            ai_review._call_model = boom
            r = self.post(f"/name_screening/{aid}/ai_assess")                         # nothing ticked: every pending row
            self.assertIn("gateway down", flash_text(r))
            self.assertFalse(pending[2].get("ai_assessment"))

            self.login("leah.fernandez")
            self.assertIn("Model assessment: on → off", flash_text(self.post("/ai-screening/rules", key="model_review", enabled="0")))
            self.login("mario.kim")
            r = self.post(f"/name_screening/{aid}/ai_assess", alert_id=[pending[2]["id"]])
            self.assertIn("switched off", flash_text(r))
        finally:
            ai_review.KEY, ai_review._call_model = saved

    def test_comparison_shows_location(self):
        """The World-Check comparison table shows the client's country and
        nationality beside the record's and ticks them when they agree."""
        def rows(key):
            c = self.K[key]
            wc = fcc.WC_BY_ID[c["screening"]["alert_case_id"]]
            return {r["field"]: (r["submitted"], r["matched"], r["match"])
                    for r in fcc.worldcheck.comparison_rows(wc, wc["matches"][0], c["client"])}

        sidorov = rows("sidorov")
        self.assertEqual(sidorov["Country"], ("Cyprus", "Russian Federation; Cyprus", True))
        self.assertEqual(sidorov["Citizenship"], ("Russian Federation", "Russian Federation", True))
        taylor = rows("taylor")
        self.assertEqual(taylor["Country"], ("United Kingdom", "Canada", False))
        self.assertEqual(taylor["Citizenship"], ("United Kingdom", "Canada", False))
        # An ISO code on the client is printed as a name; a record with no data never ticks.
        wc = fcc.WC_BY_ID[self.K["sidorov"]["screening"]["alert_case_id"]]
        r = {x["field"]: x for x in fcc.worldcheck.comparison_rows(wc, dict(wc["matches"][0], country="", citizenship=""),
                                                                   {"country": "IN", "nationality": "IN"})}
        self.assertEqual((r["Country"]["submitted"], r["Country"]["match"]), ("India", False))
        self.login("amara.iyer")
        aid = self.K["sidorov"]["screening"]["alert_case_id"]
        page = self.c.get(f"/wc/{aid}/match/{wc['matches'][0]['id']}").data.decode("utf-8")
        self.assertIn("&#10003;</span> Cyprus", page)
        self.assertNotIn('id="wc-ai"', page)                       # the AI's suggestion is the ID screening's
        self.assertNotIn("AI suggestion", self.c.get(f"/wc/{aid}").data.decode("utf-8"))

    def test_wc_pagination_and_facets(self):
        self.login("amara.iyer")
        aid = self.K["hassan"]["screening"]["alert_case_id"]
        page5 = self.c.get(f"/wc/{aid}?page=5").data.decode("utf-8")
        self.assertIn("201–212", page5)
        self.assertIn("(209)", page5)
        self.assertEqual(self.c.get(f"/wc/{aid}?page=99").status_code, 200)

    def test_recommendation(self):
        alert = fcc.ALERT_BY_ID[self.K["mercer"]["screening"]["alert_case_id"]]
        self.assertIsNone(fcc.recommendation.recommend_case_outcome(alert))
        pep = fcc.ALERT_BY_ID[self.K["mendoza"]["screening"]["alert_case_id"]]
        self.assertEqual(fcc.recommendation.recommend_case_outcome(pep, fcc.WC_BY_ID[pep["id"]])["value"], "Escalate - True Positive")
        san = fcc.ALERT_BY_ID[self.K["almansoor"]["screening"]["alert_case_id"]]
        self.assertEqual(fcc.recommendation.recommend_case_outcome(san, fcc.WC_BY_ID[san["id"]])["value"], "Reject - True Positive")

    def test_persistence_and_reset(self):
        self.login("amara.iyer")
        s = self.K["solis"]                                                # an individual, no hit
        r = self.confirm(s)
        # No match on any list: the case verifies itself once the data is confirmed.
        self.assertEqual(s["status"], "Verified", flash_text(r))
        self.assertEqual(s["closed_by"], "System User")
        self.assertEqual(s["review_decisions"][4]["decision"], "No match")
        self.assertIn("auto-verified", flash_text(r))
        self.assertGreaterEqual(StateStore.count(), 1)
        fcc._load_seed()                                                   # simulated restart
        self.assertEqual(fcc.KYC_BY_ID[s["id"]]["status"], "Verified")
        self.post("/reset")
        self.assertEqual(fcc.KYC_BY_ID[s["id"]]["status"], "DataConfirming")
        self.assertEqual(StateStore.count(), 0)

    def test_analytics_and_queue_render(self):
        self.login("nisha.rao")
        for path in ("/analytics", "/queue/l2", "/name_screening/queue", "/work", "/pipeline", "/trackers", "/audit", "/routing",
                     "/ai-screening"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        self.login("leah.fernandez")
        self.assertEqual(self.c.get("/qc").status_code, 200)
        self.assertIn(b"Switch off", self.c.get("/ai-screening").data)

    # phase 8: routing, capacity and hand-offs (SOP 5.2.1–5.2.3) -------------
    def test_routing_skill_capacity_segregation(self):
        v = self.K["volkov"]                                               # Cyrillic native name
        best, why, considered = fcc.routing.pick(v)
        self.assertEqual(best["key"], "amara.iyer", why)                   # the only L1 with the cyrillic skill
        blocked = {a["key"]: reason for a, ok, reason in considered if not ok}
        self.assertIn("cyrillic", blocked["jon.miranda"])
        amara = fcc.ANALYST_BY_KEY["amara.iyer"]
        amara["capacity"] = fcc.routing.load(AMARA)
        best, why, _ = fcc.routing.pick(v)
        self.assertIsNone(best)
        self.assertIn("capacity", why)
        # A business needs the KYB skill; Amara (no KYB, no Businesses tenant) is never eligible.
        ok, reason = fcc.routing.eligible(amara, self.K["vanguard"])
        self.assertFalse(ok)

    def test_request_cases_on_demand(self):
        self.login("amara.iyer")
        before = fcc.routing.load(AMARA)
        r = self.post("/work/request", n="3")
        got = fcc.routing.load(AMARA) - before
        self.assertEqual(got, min(3, 6 - before), flash_text(r))
        self.assertIn(f"Routed {got} case(s)", flash_text(r))
        routed = [c for c in fcc.KYC_CASES if (c.get("routing") or {}).get("trigger") == "on_demand"]
        self.assertEqual(len(routed), got)
        self.assertTrue(all(c["current_reviewer"] == AMARA and c["work"][-1]["analyst"] == AMARA for c in routed))
        r = self.post("/work/request", n="5")                              # now at capacity
        self.assertIn("at capacity", flash_text(r))
        self.assertEqual(fcc.routing.load(AMARA), 6)

    def test_get_next_case_redirects(self):
        self.login("jon.miranda")
        r = self.c.post("/work/next")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/kyc/detail/", r.headers["Location"])
        case = fcc.KYC_BY_ID[r.headers["Location"].rstrip("/").rsplit("/", 1)[1]]
        self.assertEqual(case["current_reviewer"], JON)
        self.assertEqual(case["audit"][-1]["action"], "Auto-routed")
        self.assertEqual(case["routing"]["trigger"], "next")
        self.assertEqual(self.c.get(f"/kyc/detail/{case['id']}").status_code, 200)

    def test_escalation_handoff_zero_lag(self):
        h = self.K["hassan"]
        self.escalate("mario.kim", h)
        self.assertEqual(h["status"], "DataEscalatingL2")
        self.assertEqual(h["current_reviewer"], NISHA)                     # handed off at once
        self.assertEqual(h["handoffs"][-1]["via"], "hub")
        self.assertEqual(h["handoffs"][-1]["lag_seconds"], 0)
        self.assertEqual(h["work"][-1]["analyst"], NISHA)
        self.assertTrue(h["investigation_file"])
        alert = fcc.ALERT_BY_ID[h["screening"]["alert_case_id"]]
        self.assertEqual(alert["case_level"], "L2")
        page = self.c.get("/pipeline").data.decode("utf-8")
        self.assertIn("AHMED HASSAN", page)
        r = self.post(f"/kyc/{h['id']}/l2_decision", decision="Approve", comment="fine")
        self.assertIn("earlier level", flash_text(r))                      # Mario worked it at L1
        self.login("nisha.rao")
        self.assertIn(h["id"], self.c.get("/work").data.decode("utf-8"))

    def test_backlog_run_passes_over_a_full_tier(self):
        """With every L1 slot taken, a lead's run routes nothing at L1 and
        leaves the waiting cases exactly as they were: no audit row, no save."""
        for a in fcc.ANALYSTS:
            if "L1" in a["roles"]:
                a["capacity"] = fcc.routing.load(a["email"])
        waiting = [c for c in fcc.routing.unassigned("ttlive") if fcc.routing.level_for(c) == "L1"]
        self.assertTrue(waiting)
        before = {c["id"]: (len(c["audit"]), dict(c.get("routing") or {})) for c in waiting}
        self.login("leah.fernandez")
        r = self.post("/routing/run")
        self.assertIn("still waiting for capacity", flash_text(r))
        self.assertEqual({c["id"]: (len(c["audit"]), dict(c.get("routing") or {})) for c in waiting}, before)
        self.assertTrue(all(not c["current_reviewer"] for c in waiting))

    def test_capacity_override_persists_and_queueing(self):
        self.login("leah.fernandez")
        r = self.post("/routing/capacity", key="nisha.rao", capacity="1")
        self.assertEqual(fcc.ANALYST_BY_KEY["nisha.rao"]["capacity"], 1, flash_text(r))
        hid = self.K["hassan"]["id"]
        fcc._load_seed()                                                   # simulated restart: fresh record objects
        self.assertEqual(fcc.ANALYST_BY_KEY["nisha.rao"]["capacity"], 1)
        h = fcc.KYC_BY_ID[hid]
        self.escalate("mario.kim", h)                                      # Nisha is full, Mario is excluded
        self.assertEqual(h["current_reviewer"], "")
        self.assertEqual(h["routing"]["status"], "queued")
        self.assertIsNone(h["handoffs"][-1]["lag_seconds"])
        self.login("leah.fernandez")
        console = self.c.get("/routing").data.decode("utf-8")
        self.assertIn("nobody free", console)
        self.post("/routing/capacity", key="nisha.rao", capacity="4")
        r = self.post("/routing/run", case_id=h["id"])
        self.assertEqual(h["current_reviewer"], NISHA, flash_text(r))
        self.assertGreaterEqual(h["handoffs"][-1]["lag_seconds"], 0)
        self.post("/reset")
        self.assertEqual(fcc.ANALYST_BY_KEY["nisha.rao"]["capacity"], 4)

    # phase 8: trackers, investigation file, audit log (SOP 5.2.4) -----------
    def test_trackers_render_and_csv(self):
        self.login("nisha.rao")
        for view in ("productivity", "fraud", "edd", "tts"):
            self.assertEqual(self.c.get(f"/trackers?view={view}").status_code, 200, view)
        r = self.c.get("/trackers?view=fraud&format=csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith("﻿".encode("utf-8")))
        self.assertIn(b"Fraud Typology,Status\r\n", r.data)
        self.assertIn(b"KWAME MENSAH", r.data)
        page = self.c.get("/trackers?view=edd").data.decode("utf-8")
        self.assertIn("Screening Date", page)
        self.assertIn(self.K["mensah"]["investigation_file"]["name"], page)
        self.c.post("/tenant", data={"tenant": "ttshop", "next": "/kyc/list"})
        page = self.c.get("/trackers?view=edd").data.decode("utf-8")
        self.assertIn("Business Escalation Tracker", page)
        self.assertIn(self.K["oceanic"]["investigation_file"]["name"], page)

    def test_investigation_file_zip(self):
        self.login("amara.iyer")
        m = self.K["mercer"]
        aid = m["screening"]["alert_case_id"]
        wc = fcc.WC_BY_ID[aid]
        self.confirm(m)
        self.post(f"/name_screening/{aid}/handle")
        self.post(f"/wc/{aid}/export")
        r = self.post(f"/kyc/{m['id']}/edd_review", decision="Potential match - Escalate", summary="Unresolved UK record.",
                      attachment=wc["exports"][0]["filename"])
        self.assertEqual(m["status"], "DataEscalatingL2", flash_text(r))
        self.assertEqual(m["investigation_file"]["name"], f"{m['client_id']} ({m['id']})")
        self.assertEqual(self.c.get(f"/kyc/detail/{m['id']}/file").status_code, 200)
        z = self.c.get(f"/kyc/{m['id']}/investigation_file.zip")
        self.assertEqual(z.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(z.data))
        names = archive.namelist()
        pdfs = [n for n in names if n.endswith(".pdf")]
        self.assertEqual(len(pdfs), 1)
        self.assertTrue(archive.read(pdfs[0]).startswith(b"%PDF"))
        self.assertTrue(any(n.endswith("audit_trail.csv") for n in names))
        self.assertTrue(any("/documents/" in n for n in names))

    def test_global_audit_log(self):
        self.login("amara.iyer")
        page = self.c.get("/audit").data.decode("utf-8")
        for kind in ("KYC case", "Screening case", "World-Check case"):
            self.assertIn(kind, page)
        self.assertIn("FCC Router", page)
        r = self.c.get("/audit?format=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"At,Actor,Record", r.data)

    # phase 8: QC and Checker 2 (SOP 2.1, 4.2) --------------------------------
    def test_qc_sampling_and_scorecard(self):
        self.login("leah.fernandez")
        s1 = fcc.qc.sample(fcc.KYC_CASES, "ttlive")
        s2 = fcc.qc.sample(fcc.KYC_CASES, "ttlive")
        self.assertTrue(s1)
        self.assertEqual([c["id"] for c in s1], [c["id"] for c in s2])    # deterministic for the day
        page = self.c.get("/qc").data.decode("utf-8")
        self.assertIn(s1[0]["id"], page)
        target = next(c for c in s1 if not c.get("qc_result"))
        r = self.post(f"/kyc/{target['id']}/qc_review", result="Fail", reason="Rationale insufficient",
                      comment="The closing note does not say what was checked.")
        self.assertEqual(target["qc_result"], "Fail", flash_text(r))
        self.assertEqual(target["qc_reviewer"], LEAH)
        self.assertEqual(target["pending_reason"], "Pending QC")
        self.assertEqual([c["id"] for c in fcc.qc.sample(fcc.KYC_CASES, "ttlive")], [c["id"] for c in s1])  # sample stable after a review
        closer = target["closed_by"]
        self.login(closer.split("@")[0])
        work = self.c.get("/work").data.decode("utf-8")
        self.assertIn(target["id"], work)
        self.post(f"/kyc/{target['id']}/qc_ack")
        self.assertTrue(target["qc_ack"])
        self.assertEqual(target["pending_reason"], "")
        self.login("leah.fernandez")
        page = self.c.get("/qc").data.decode("utf-8")
        self.assertIn("Rationale insufficient", page)
        row = next(r for r in fcc.qc.scorecard(fcc.KYC_CASES, "ttlive") if r["email"] == closer)
        self.assertGreaterEqual(row["failed"], 1)

    def test_checker2_ttshop(self):
        self.login("jon.miranda")
        self.c.post("/tenant", data={"tenant": "ttshop", "next": "/kyc/list"})
        f = self.K["filler-shop-7"]                                        # DataScreening, no hit
        self.post(f"/kyc/{f['id']}/reserve")
        r = self.post(f"/kyc/{f['id']}/edd_review", decision="No match - Approve", summary="Clean.")
        self.assertEqual(f["status"], "DataScreening")
        self.assertIn("Recalculate", flash_text(r))
        self.post(f"/kyc/{f['id']}/recalculate_risk")
        r = self.post(f"/kyc/{f['id']}/edd_review", decision="No match - Approve", summary="Clean seller, documents in order.")
        self.assertEqual(f["pending_reason"], "Pending Checker 2", flash_text(r))
        self.assertEqual(f["checker1"]["by"], JON)
        self.assertEqual(f["status"], "DataScreening")
        holder = f["current_reviewer"]
        self.assertIn(holder, (NISHA, MARIO))                              # routed to an L2 with the KYB skill
        r = self.post(f"/kyc/{f['id']}/checker2", decision="Approve", comment="ok")
        self.assertEqual(f["status"], "DataScreening", "Checker 1 must not sign off as Checker 2")
        self.login(holder.split("@")[0])
        self.c.post("/tenant", data={"tenant": "ttshop", "next": "/queue/l2"})
        self.assertIn(f["id"], self.c.get("/queue/l2").data.decode("utf-8"))
        self.post(f"/kyc/{f['id']}/reserve")
        r = self.post(f"/kyc/{f['id']}/checker2", decision="Approve", comment="Risk rating and documents verified independently.")
        self.assertEqual(f["status"], "Verified", flash_text(r))
        self.assertEqual(f["checker2"]["by"], holder)
        self.assertEqual(f["closed_by"], holder)

    def test_level_role_enforced(self):
        o = self.K["oceanic"]                                              # Pending EDD, held by Ryan
        self.login("nisha.rao")
        r = self.post(f"/kyc/{o['id']}/l2_decision", decision="Approve", comment="ok")
        self.assertEqual(o["status"], "DataEscalatingL2")
        self.assertIn("EDD role", flash_text(r))
        self.login("ryan.lee")
        self.post(f"/kyc/{o['id']}/reserve")
        r = self.post(f"/kyc/{o['id']}/l2_decision", decision="Reject", comment="Sanctioned parent — 50% rule.")
        self.assertEqual(o["status"], "Rejected", flash_text(r))

    def test_screening_l2_tile(self):
        h = self.K["hassan"]
        aid = h["screening"]["alert_case_id"]
        self.escalate("mario.kim", h)
        page = self.c.get("/name_screening/queue?level=L2").data.decode("utf-8")
        self.assertIn(aid, page)

    # phase 8: SOP 4 details -----------------------------------------------------
    def test_poa_checks_block_confirm(self):
        self.login("amara.iyer")
        cam = self.K["camargo"]
        r = self.confirm(cam)
        self.assertEqual(cam["status"], "DataConfirming")
        self.assertIn("6 months", flash_text(r))
        vol = self.K["volkov"]
        r = self.confirm(vol)
        self.assertEqual(vol["status"], "DataConfirming")
        self.assertIn("escalate", flash_text(r).lower())
        page = self.c.get(f"/kyc/detail/{vol['id']}").data.decode("utf-8")
        self.assertIn("Sanctioned-country ID rule", page)
        sid = self.K["sidorov"]
        r = self.confirm(sid, poa_ok="0")
        self.assertEqual(sid["status"], "DataConfirming")
        self.assertIn("proof of address", flash_text(r).lower())
        r = self.confirm(sid)
        self.assertEqual(sid["status"], "DataScreening", flash_text(r))

    def test_fraud_typology_tracker(self):
        n = self.K["nguyen"]
        r = self.escalate("amara.iyer", n, reason="Fraud ID / AI-generated document", comment="Data-page font mismatch.")
        self.assertEqual(n["status"], "DataConfirming")
        self.assertIn("typology", flash_text(r).lower())
        r = self.post(f"/kyc/{n['id']}/escalate", reason="Fraud ID / AI-generated document", comment="Data-page font mismatch.",
                      fraud_typology="Forged or altered document")
        self.assertEqual(n["status"], "DataEscalatingL2", flash_text(r))
        self.assertEqual(n["fraud_typology"], "Forged or altered document")
        page = self.c.get("/trackers?view=fraud").data.decode("utf-8")
        self.assertIn(n["id"], page)
        self.assertIn("Forged or altered document", page)


if __name__ == "__main__":
    unittest.main()
