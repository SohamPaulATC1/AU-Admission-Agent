"""catalog/build_catalog.py against the real AU Excel files (spec sections 3, 5, 9).

Skipped when the Excel files are absent (EC2 carries only the JSON).
"""
import collections
import copy
import json
import os
import unittest

from catalog import build_catalog

HAVE_EXCEL = os.path.exists(build_catalog.PROGRAM_XLSX) and os.path.exists(build_catalog.FEE_XLSX)


@unittest.skipUnless(HAVE_EXCEL, "AU Admission Data Excel files not present")
class TestBuildFromExcel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = build_catalog.build()
        cls.records = cls.catalog["records"]
        cls.by_name = {r["name"]: r for r in cls.records}

    def test_record_count_and_exclusions(self):
        self.assertEqual(len(self.records), 87)
        self.assertEqual(self.catalog["excluded"], sorted(build_catalog.EXCLUDED))
        for name in build_catalog.EXCLUDED:
            self.assertNotIn(name, self.by_name)

    def test_uid_133_records_share_fees_and_keep_their_own_eligibility(self):
        group = [r for r in self.records if r["uid"] == "133"]
        self.assertEqual(len(group), 3)
        self.assertEqual(len({json.dumps(r["fees"]) for r in group}), 1)
        self.assertEqual(len({r["eligibility"] for r in group}), 2)  # the two AI & DS rows share a text
        for r in group:  # a shared fee row's name is not an alias of the other records
            self.assertNotIn("B.Tech CSE (Artificial Intelligence & Machine Learning)", r["aliases"])

    def test_bba_e_commerce_takes_the_bba_fee_row(self):
        self.assertEqual(self.by_name["BBA (E-Commerce)"]["fees"], self.by_name["BBA"]["fees"])
        self.assertNotIn("BBA", self.by_name["BBA (E-Commerce)"]["aliases"])

    def test_mpharm_row_is_split_with_own_fees_and_shared_intake(self):
        a = self.by_name["M.Pharm (Pharmaceutics)"]
        b = self.by_name["M.Pharm (Pharmacology)"]
        self.assertEqual((a["uid"], b["uid"]), ("171", "171"))
        self.assertEqual(a["eligibility"], b["eligibility"])
        self.assertEqual(a["seats_note"], "intake shared with M.Pharm (Pharmacology)")
        self.assertEqual(b["seats_note"], "intake shared with M.Pharm (Pharmaceutics)")
        self.assertEqual(sum("seats_note" in r for r in self.records), 2)

    def test_sap_add_on_is_attached_to_the_sap_record_only(self):
        sap = self.by_name["B.Tech CSE with specialization in SAP"]
        self.assertEqual(sap["add_ons"], [{"name": "SAP add-on certification", "duration_years": 1, "fee": 65000}])
        self.assertEqual(sap["fees"]["total"], 881800)
        self.assertEqual(sum(bool(r["add_ons"]) for r in self.records), 1)

    def test_cse_record_matches_the_fee_sheet(self):
        fees = self.by_name["B.Tech (Computer Science and Engineering)"]["fees"]
        self.assertEqual(fees["per_semester"],
                         [108250, 108250, 105750, 105750, 105000, 105000, 105000, 105000])
        self.assertEqual(fees["per_year"], [216500, 211500, 210000, 210000])
        self.assertEqual((fees["program_fee"], fees["admission_fee"], fees["total"]), (848000, 43800, 891800))
        self.assertEqual(fees["due_at_admission"], 152050)

    def test_fee_arithmetic_holds_for_every_record(self):
        for r in self.records:
            fees = r["fees"]
            with self.subTest(r["name"]):
                self.assertEqual(sum(fees["per_semester"]), fees["program_fee"])
                self.assertEqual(sum(fees["per_year"]), fees["program_fee"])
                self.assertEqual(fees["program_fee"] + fees["admission_fee"], fees["total"])
                self.assertEqual(fees["due_at_admission"], fees["first_semester"] + fees["admission_fee"])
                self.assertEqual(fees["first_semester"], fees["per_semester"][0])
                self.assertEqual(fees["session"], "2027")
                if r["duration_years"] is not None:
                    self.assertEqual(len(fees["per_semester"]), 2 * r["duration_years"])

    def test_ids_are_unique_and_readable(self):
        ids = [r["id"] for r in self.records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("btech-computer-science-and-engineering", ids)
        self.assertIn("ba-english-language-and-literature", ids)  # source spells it "B. A (...)"
        self.assertIn("bba-llb-hons", ids)
        for i in ids:
            self.assertRegex(i, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_degree_set_is_pinned(self):
        self.assertEqual(sorted({r["degree"] for r in self.records}), [
            "B.A", "B.Com", "B.Ed", "B.Optom", "B.Pharm", "B.Sc", "B.Tech", "BA LL.B", "BBA",
            "BBA LL.B", "BCA", "BFND", "BMLS", "D.Pharm", "LL.M", "M.A", "M.Pharm", "M.Sc",
            "M.Tech", "MBA", "MCA", "Post-M.Sc Diploma",
        ])
        self.assertEqual(collections.Counter(r["degree"] for r in self.records)["B.Tech"], 18)

    def test_levels_schools_and_durations(self):
        self.assertEqual({r["level"] for r in self.records}, {"UG", "PG", "Diploma"})
        self.assertEqual(collections.Counter(r["school"]["code"] for r in self.records), {
            "SoBAS": 16, "SoB": 6, "SoE": 3, "SoET": 20, "SoHMS": 12,
            "SoLJ": 3, "SoLACS": 14, "SoLB": 8, "SoMC": 4, "SoSA": 1,
        })
        free_text = [r for r in self.records if r["duration_years"] is None]
        self.assertEqual([r["name"] for r in free_text], ["Post-M.Sc Diploma in Medical Physics"])
        self.assertTrue(free_text[0]["duration_note"].startswith("1st Year"))

    def test_eligibility_whitespace_is_tidied(self):
        for r in self.records:
            self.assertNotRegex(r["eligibility"], r"10\s+\+|\+\s+2|\s{2}")

    def test_fee_file_names_become_aliases(self):
        bmls = self.by_name["Bachelor Of Medical Laboratory Science (BMLS)"]
        self.assertIn("B.Sc Medical Laboratory Technology", bmls["aliases"])
        self.assertIn("BA. LLB (Hons)", self.by_name["B.A. LL.B (Hons)"]["aliases"])

    def test_every_ug_record_has_an_eligibility_rule(self):
        for r in self.records:
            with self.subTest(r["name"]):
                if r["level"] == "UG":
                    self.assertEqual(list(r["eligibility_rule"]),
                                     ["min_aggregate", "subject_options", "conditions", "verified"])
                else:
                    self.assertNotIn("eligibility_rule", r)
        self.assertEqual(self.catalog["subjects"], [
            "Physics", "Chemistry", "Mathematics", "Biology", "Biotechnology", "Computer Science",
            "Computer Application", "Technical Vocational", "Statistics", "Economics", "Geography",
            "Psychology", "Agriculture", "Nutrition", "Home Science", "Human Development"])
        self.assertEqual(list(self.catalog), ["session", "source", "excluded", "subjects", "records"])

    def test_shared_respective_subject_text_requires_both_subjects(self):
        for name in ("B.Sc (Physics)", "B.Sc (Chemistry)"):
            self.assertEqual(self.by_name[name]["eligibility_rule"]["subject_options"], [["Physics", "Chemistry"]])

    def test_bed_needs_a_degree_so_is_never_matched(self):
        self.assertIsNone(self.by_name["B.Ed"]["eligibility_rule"]["min_aggregate"])

    def test_committed_courses_json_is_fresh(self):
        with open(build_catalog.COURSES_JSON, encoding="utf-8") as f:
            committed = f.read()
        self.assertEqual(committed, build_catalog.dumps(self.catalog),
                         "catalog/courses.json is stale: run catalog/build_catalog.py")


class TestBuildHelpers(unittest.TestCase):
    def test_slug_joins_spaced_degree_letters(self):
        self.assertEqual(build_catalog.slug("B. A (English Language and Literature)"),
                         "ba-english-language-and-literature")
        self.assertEqual(build_catalog.slug("B.B.A. LL.B (Hons)"), "bba-llb-hons")
        self.assertEqual(build_catalog.slug("B. Sc. (Hons.) Agriculture"), "bsc-hons-agriculture")
        self.assertEqual(build_catalog.slug("B.Tech CSE (Artificial Intelligence & Machine Learning)"),
                         "btech-cse-artificial-intelligence-and-machine-learning")

    def test_degree_of_rejects_unknown_names(self):
        self.assertEqual(build_catalog.degree_of("M. Sc. Graphics and Animation"), "M.Sc")
        self.assertEqual(build_catalog.degree_of("B.B.A. LL.B (Hons)"), "BBA LL.B")
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.degree_of("MBBS")

    def test_amount_parsing(self):
        self.assertEqual(build_catalog.amount("", "x"), 0)
        self.assertEqual(build_catalog.amount("108250.0", "x"), 108250)
        with self.assertRaises(build_catalog.BuildError):
            build_catalog.amount("100.5", "x")

    def _fee_row(self, **overrides):
        row = {
            "Duration": "2", "1st Sem Fees 2026": "100", "2nd Sem Fees 2026": "100",
            "3rd Sem Fees 2026": "90", "4th Sem Fees 2026": "90", "5th - 6th Sem Fees 2026": "0",
            "7th - 10th Sem Fees 2026": "0", "Final Program Fees 2026 (Incl. FL, Alumni )": "380",
            "Admission Fees 2026 ( Incl. T-Shirt & Blazer )": "50", "Total Fees 2026": "430",
        }
        row.update(overrides)
        return row

    def test_fee_block_on_a_consistent_row(self):
        years, fees = build_catalog.fee_block(self._fee_row(), "t")
        self.assertEqual(years, 2)
        self.assertEqual(fees["per_semester"], [100, 100, 90, 90])
        self.assertEqual(fees["per_year"], [200, 180])
        self.assertEqual(fees["due_at_admission"], 150)

    def test_fee_block_rejects_bad_arithmetic(self):
        for overrides in ({"Total Fees 2026": "431"},
                          {"Final Program Fees 2026 (Incl. FL, Alumni )": "381", "Total Fees 2026": "431"},
                          {"5th - 6th Sem Fees 2026": "10"}):
            with self.subTest(overrides), self.assertRaises(build_catalog.BuildError):
                build_catalog.fee_block(self._fee_row(**overrides), "t")


class TestAttachEligibilityRules(unittest.TestCase):
    RECORDS = [
        {"name": "A", "level": "UG", "eligibility": "t1"},
        {"name": "B", "level": "UG", "eligibility": "t1"},
        {"name": "C", "level": "UG", "eligibility": "t2"},
        {"name": "D", "level": "PG", "eligibility": "t3"},
    ]

    def rule(self, text, programs, **overrides):
        rule = {"text": text, "programs": programs, "min_aggregate": 50, "subject_options": None,
                "conditions": [], "verified": False}
        rule.update(overrides)
        return rule

    def attach(self, rules, subjects=("Physics", "Chemistry")):
        records = copy.deepcopy(self.RECORDS)
        returned = build_catalog.attach_eligibility_rules(records, {"subjects": list(subjects), "rules": rules})
        return records, returned

    def good_rules(self):
        return [self.rule("t1", ["A", "B"], subject_options=[["Physics", "Chemistry"]], verified=True,
                          note="reviewer only"),
                self.rule("t2", ["C"], min_aggregate=None, conditions=["needs a degree"])]

    def test_rules_attach_to_ug_records_only(self):
        records, subjects = self.attach(self.good_rules())
        self.assertEqual(subjects, ["Physics", "Chemistry"])
        self.assertEqual(records[0]["eligibility_rule"], {
            "min_aggregate": 50, "subject_options": [["Physics", "Chemistry"]], "conditions": [], "verified": True})
        self.assertEqual(records[1]["eligibility_rule"], records[0]["eligibility_rule"])
        self.assertIsNone(records[2]["eligibility_rule"]["min_aggregate"])
        self.assertNotIn("eligibility_rule", records[3])
        self.assertNotIn("note", records[0]["eligibility_rule"])

    def assertBuildError(self, rules, fragment):
        with self.assertRaises(build_catalog.BuildError) as caught:
            self.attach(rules)
        self.assertIn(fragment, str(caught.exception))

    def test_ug_text_without_a_rule(self):
        self.assertBuildError(self.good_rules()[:1], "no eligibility rule")

    def test_rule_matching_no_ug_record(self):
        self.assertBuildError(self.good_rules() + [self.rule("t3", ["D"])], "matches no UG record")

    def test_programs_must_match_the_records_sharing_the_text(self):
        rules = self.good_rules()
        rules[0]["programs"] = ["A"]
        self.assertBuildError(rules, "lists ['A']")

    def test_duplicate_text(self):
        self.assertBuildError(self.good_rules() + [self.rule("t1", ["A", "B"])], "two rules share")

    def test_unknown_subject(self):
        rules = self.good_rules()
        rules[0]["subject_options"] = [["Physics", "Accountancy"]]
        self.assertBuildError(rules, "unknown subject 'Accountancy'")

    def test_verified_must_be_a_boolean(self):
        rules = self.good_rules()
        rules[0]["verified"] = "true"
        self.assertBuildError(rules, "verified must be true or false")

    def test_unknown_or_missing_keys(self):
        rules = self.good_rules()
        rules[0]["min_agregate"] = 50
        self.assertBuildError(rules, "unexpected keys ['min_agregate']")
        rules = self.good_rules()
        del rules[1]["conditions"]
        self.assertBuildError(rules, "missing keys ['conditions']")

    def test_field_types(self):
        for field, value, fragment in (("min_aggregate", 150, "min_aggregate"),
                                       ("min_aggregate", True, "min_aggregate"),
                                       ("subject_options", [], "subject_options"),
                                       ("subject_options", [[]], "subject_options"),
                                       ("conditions", "CLAT", "conditions"),
                                       ("conditions", [""], "conditions")):
            rules = self.good_rules()
            rules[0][field] = value
            with self.subTest((field, value)):
                self.assertBuildError(rules, fragment)


if __name__ == "__main__":
    unittest.main()
