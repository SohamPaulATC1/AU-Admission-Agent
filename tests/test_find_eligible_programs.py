"""course_catalog.find_eligible_programs (spec section 11.4).

Matching tests run on a fake rule set patched into the index, so they do not
depend on which real rules the operator has verified. The golden profiles run
on the real data and skip until every UG rule is verified.
"""
import unittest
from unittest import mock

import course_catalog as cc
from tests import tool_playground

PCM = ["Physics", "Chemistry", "Mathematics"]
PCB = ["Physics", "Chemistry", "Biology"]
TOOL = "find_eligible_programs"


def rule(minimum=50, options=None, conditions=(), verified=True):
    return {"min_aggregate": minimum, "subject_options": options, "conditions": list(conditions),
            "verified": verified}


def record(name, degree, eligibility_rule, school="SoX"):
    return {"id": name.lower().replace(" ", "-"), "name": name, "aliases": [], "degree": degree,
            "level": "UG", "school": {"code": school, "name": "X"}, "department": "Dept",
            "eligibility_rule": eligibility_rule}


FAKES = [
    record("B.Tech (Alpha Engineering)", "B.Tech", rule(60, [["Physics", "Mathematics", "Chemistry"],
                                                              ["Physics", "Mathematics", "Biology"]],
                                                          ["at least 45% in each of those subjects"])),
    record("B.A (Beta Studies)", "B.A", rule(50)),
    record("B.Sc (Gamma Science)", "B.Sc", rule(55, [["Biology"]])),
    record("B.Ed", "B.Ed", rule(None, conditions=["a degree"])),
    record("BBA (Delta)", "BBA", rule(40, verified=False)),
]


def find(**args):
    return cc.handle_tool_call(TOOL, args)


def names(response):
    return [p["name"] for p in response["programs"]]


class FakeIndexCase(unittest.TestCase):
    records = FAKES

    def setUp(self):
        patcher = mock.patch.object(cc, "INDEX", [cc._index(r) for r in self.records])
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMatching(FakeIndexCase):
    def test_minimum_is_inclusive(self):
        self.assertIn("B.Tech (Alpha Engineering)", names(find(aggregate_pct=60, subjects=PCM)))
        self.assertNotIn("B.Tech (Alpha Engineering)", names(find(aggregate_pct=59.9, subjects=PCM)))

    def test_one_subject_set_must_be_fully_covered(self):
        self.assertNotIn("B.Tech (Alpha Engineering)", names(find(aggregate_pct=70, subjects=PCB)))
        self.assertIn("B.Tech (Alpha Engineering)",
                      names(find(aggregate_pct=70, subjects=["physics", "MATHEMATICS", "Biology"])))

    def test_any_stream_rule_matches_no_subjects(self):
        self.assertEqual(find(aggregate_pct=52, subjects=[]), {
            "count": 1, "programs": [{"name": "B.A (Beta Studies)"}], "not_covered": 2})

    def test_unverified_and_null_minimum_are_not_covered_never_named(self):
        response = find(aggregate_pct=99, subjects=PCB)
        self.assertEqual(response["not_covered"], 2)
        self.assertNotIn("B.Ed", names(response))
        self.assertNotIn("BBA (Delta)", names(response))

    def test_conditions_only_when_present(self):
        response = find(aggregate_pct=70, subjects=["Physics", "Mathematics", "Chemistry", "Biology"])
        self.assertEqual(response["programs"], [
            {"name": "B.A (Beta Studies)"},
            {"name": "B.Sc (Gamma Science)"},
            {"name": "B.Tech (Alpha Engineering)", "conditions": ["at least 45% in each of those subjects"]},
        ])

    def test_nothing_matches(self):
        self.assertEqual(find(aggregate_pct=30, subjects=PCM), {"count": 0, "not_covered": 2})

    def test_filters_narrow_the_pool(self):
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, degree="B.Sc"), {
            "count": 1, "programs": [{"name": "B.Sc (Gamma Science)"}], "not_covered": 0})
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, degree="bsc")["count"], 1)
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, keyword="gamma")["count"], 1)
        self.assertEqual(find(aggregate_pct=70, subjects=PCB, degree="B.Com"), {"count": 0, "not_covered": 0})

    def test_float_percentage_and_sdk_sequences(self):
        self.assertEqual(find(aggregate_pct=55.5, subjects=("Biology",))["count"], 2)
        self.assertEqual(find(aggregate_pct=55, subjects="Biology"), find(aggregate_pct=55, subjects=["Biology"]))

    def test_status(self):
        self.assertEqual(cc.result_status(TOOL, find(aggregate_pct=52, subjects=[])), "eligible=1")
        self.assertEqual(cc.result_status(TOOL, find(subjects=[])), "error")

    def test_errors(self):
        for args, fragment in (({"subjects": []}, "aggregate_pct is required"),
                               ({"aggregate_pct": "58", "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": "58%", "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": True, "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": 101, "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": -1, "subjects": []}, "aggregate_pct"),
                               ({"aggregate_pct": 60}, "subjects is required"),
                               ({"aggregate_pct": 60, "subjects": 5}, "subjects must be a list"),
                               ({"aggregate_pct": 60, "subjects": ["Accountancy"]}, "unknown subject 'Accountancy'"),
                               ({"aggregate_pct": 60, "subjects": [], "degree": "MBA"}, "unknown degree 'MBA'"),
                               ({"aggregate_pct": 60, "subjects": [], "keyword": "course"}, "keyword"),
                               ({"aggregate_pct": 60, "subjects": [], "level": "UG"}, "unknown argument 'level'")):
            with self.subTest(args):
                self.assertIn(fragment, cc.handle_tool_call(TOOL, args)["error"])
        self.assertIn("B.Tech", find(aggregate_pct=60, subjects=[], degree="MBA")["error"])
        self.assertIn("error", cc.handle_tool_call(TOOL, None))


class TestStaging(FakeIndexCase):
    records = [record(f"B.A (Subject {chr(65 + i)})", "B.A", rule(50)) for i in range(10)] \
        + [record("B.Sc (Extra)", "B.Sc", rule(50))]

    def test_more_than_ten_groups_by_degree(self):
        self.assertEqual(find(aggregate_pct=60, subjects=[]), {
            "count": 11, "by_degree": [{"degree": "B.A", "count": 10}, {"degree": "B.Sc", "count": 1}],
            "not_covered": 0})

    def test_ten_lists_names(self):
        response = find(aggregate_pct=60, subjects=[], degree="B.A")
        self.assertEqual(response["count"], 10)
        self.assertEqual(len(response["programs"]), 10)


class TestStagingOneDegree(FakeIndexCase):
    # Text-only test 2026-10-07: degree=B.Tech with 18 matches came back as
    # by_degree [{"degree": "B.Tech", "count": 18}], nothing left to narrow on,
    # and the model read list_programs names as "you appear eligible".
    records = [record(f"B.A (Subject {chr(65 + i)})", "B.A", rule(50)) for i in range(12)]

    def test_one_degree_lists_the_first_ten_and_counts_the_rest(self):
        response = find(aggregate_pct=60, subjects=[])
        self.assertEqual(response["count"], 12)
        self.assertEqual(names(response), [f"B.A (Subject {chr(65 + i)})" for i in range(10)])
        self.assertEqual(response["more"], 2)
        self.assertNotIn("by_degree", response)
        self.assertEqual(find(aggregate_pct=60, subjects=[], degree="B.A"), response)

    def test_keyword_still_narrows(self):
        self.assertEqual(find(aggregate_pct=60, subjects=[], keyword="Subject C"), {
            "count": 1, "programs": [{"name": "B.A (Subject C)"}], "not_covered": 0})


class TestRealData(unittest.TestCase):
    def test_not_covered_counts_unjudgeable_ug_records(self):
        ug = [r for r in cc.RECORDS if r["level"] == "UG"]
        expected = sum(1 for r in ug if not r["eligibility_rule"]["verified"]
                       or r["eligibility_rule"]["min_aggregate"] is None)
        self.assertEqual(find(aggregate_pct=100, subjects=cc.SUBJECTS)["not_covered"], expected)

    def test_vocabulary_and_degrees(self):
        self.assertEqual(len(cc.SUBJECTS), 16)
        self.assertEqual(cc.UG_DEGREES, ["B.A", "B.Com", "B.Ed", "B.Optom", "B.Pharm", "B.Sc", "B.Tech",
                                         "BA LL.B", "BBA", "BBA LL.B", "BCA", "BFND", "BMLS"])

    def test_declaration(self):
        spec = next(s for s in cc.TOOL_SPECS if s["name"] == TOOL)
        props = spec["parameters"]["properties"]
        self.assertEqual(spec["parameters"]["required"], ["aggregate_pct", "subjects"])
        self.assertEqual(props["subjects"]["items"]["enum"], cc.SUBJECTS)
        self.assertEqual(props["degree"]["enum"], cc.UG_DEGREES)
        self.assertEqual(props["aggregate_pct"]["type"], "NUMBER")

    def test_declaration_tells_model_to_drop_unlisted_subjects(self):
        spec = next(s for s in cc.TOOL_SPECS if s["name"] == TOOL)
        self.assertIn("leave out", spec["parameters"]["properties"]["subjects"]["description"])
        self.assertIn("not CGPA", spec["parameters"]["properties"]["aggregate_pct"]["description"])
        self.assertIn("PCB = Physics, Chemistry, Biology", spec["description"])

    def test_every_response_fits_the_budget(self):
        profiles = [[], PCM, PCB, PCM + ["Biology"], ["Economics"], ["Geography"], cc.SUBJECTS]
        for pct in (50, 60, 75, 100):
            for subjects in profiles:
                for degree in [None] + cc.UG_DEGREES:
                    args = {"aggregate_pct": pct, "subjects": subjects}
                    if degree:
                        args["degree"] = degree
                    with self.subTest(args):
                        self.assertLessEqual(cc.response_chars(cc.handle_tool_call(TOOL, args)), 1500)

    def test_playground_parses_a_float_percentage(self):
        self.assertEqual(tool_playground.parse_args(TOOL, ["aggregate_pct=58.5", "subjects=Physics,Chemistry,Biology"]),
                         {"aggregate_pct": 58.5, "subjects": PCB})


ALL_VERIFIED = all(r["eligibility_rule"]["verified"] for r in cc.RECORDS if r["level"] == "UG")


@unittest.skipUnless(ALL_VERIFIED, "eligibility_rules.json not fully verified by the operator yet")
class TestGoldenProfiles(unittest.TestCase):
    """Expected results derived by hand from the eligibility texts (spec 11.6)."""

    def test_58_pcb(self):
        self.assertEqual(find(aggregate_pct=58, subjects=PCB), {
            "count": 27, "by_degree": [
                {"degree": "B.Sc", "count": 13}, {"degree": "B.A", "count": 9},
                {"degree": "B.Com", "count": 1}, {"degree": "B.Optom", "count": 1},
                {"degree": "B.Tech", "count": 1}, {"degree": "BFND", "count": 1}, {"degree": "BMLS", "count": 1}],
            "not_covered": 1})

    def test_58_pcb_btech(self):
        self.assertEqual(find(aggregate_pct=58, subjects=PCB, degree="B.Tech"), {
            "count": 1, "programs": [{"name": "B.Tech (Biomedical Engineering)",
                                      "conditions": ["at least 45% in each of those subjects"]}],
            "not_covered": 0})

    def test_62_pcm(self):
        response = find(aggregate_pct=62, subjects=PCM)
        self.assertEqual(response["count"], 47)
        self.assertIn({"degree": "B.Tech", "count": 18}, response["by_degree"])

    def test_55_commerce(self):
        self.assertEqual(find(aggregate_pct=55, subjects=["Economics"]), {
            "count": 13, "by_degree": [{"degree": "B.A", "count": 9}, {"degree": "B.Sc", "count": 3},
                                       {"degree": "B.Com", "count": 1}],
            "not_covered": 1})

    def test_82_pcm_btech_lists_names_not_one_group(self):
        response = find(aggregate_pct=82, subjects=PCM, degree="B.Tech")
        self.assertEqual((response["count"], len(response["programs"]), response["more"]), (18, 10, 8))

    def test_48_pcm_matches_nothing(self):
        self.assertEqual(find(aggregate_pct=48, subjects=PCM), {"count": 0, "not_covered": 1})

    def test_70_arts_with_geography_bsc(self):
        response = find(aggregate_pct=70, subjects=["Geography"], degree="B.Sc")
        self.assertCountEqual(names(response), [
            "B.Sc (Environmental Science and Sustainability)", "B.Sc (Economics)", "B.Sc (Geography)",
            "B.Sc (Geoinformatics)", "B.Sc. Graphics, Animation and Media Technology"])
        conditions = {p["name"]: p.get("conditions") for p in response["programs"]}
        self.assertEqual(conditions["B.Sc (Environmental Science and Sustainability)"],
                         ["10+2 with science and humanities subjects"])


if __name__ == "__main__":
    unittest.main()
