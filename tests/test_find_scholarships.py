"""course_catalog.find_scholarships and the scholarships.json loader (spec section 13).

Loader tests mutate a copy of the real file. Formula and matching tests run on
fake schemes patched into SCHOLARSHIPS over a real record (B.Tech Civil), so
they do not depend on the policy text. Golden cases run on the real,
operator-verified catalog/scholarships.json.
"""
import copy
import unittest
from unittest import mock

import course_catalog as cc

REAL = cc._load("scholarships.json")


def scheme_of(data, sid):
    return next(s for s in data["schemes"] if s["id"] == sid)


class TestLoader(unittest.TestCase):
    def assertRejects(self, mutate, message, records=None):
        data = copy.deepcopy(REAL)
        mutate(data)
        with self.assertRaisesRegex(ValueError, message):
            cc.check_scholarships(data, cc.RECORDS if records is None else records)

    def test_real_file_loads_all_verified(self):
        cc.check_scholarships(REAL, cc.RECORDS)
        self.assertEqual(len(cc.SCHOLARSHIPS["schemes"]), 21)
        self.assertTrue(all(s["verified"] for s in cc.SCHOLARSHIPS["schemes"]))

    def test_unknown_scheme_key(self):
        self.assertRejects(lambda d: d["schemes"][0].update(tier=[]), "keys must be")

    def test_unknown_input(self):
        self.assertRejects(lambda d: scheme_of(d, "jee-main").update(input="neet_rank"), "unknown input")

    def test_unknown_kind(self):
        self.assertRejects(lambda d: scheme_of(d, "early-bird")["tiers"][0]["award"].update(kind="cashback"),
                           "unknown kind")

    def test_award_fields_must_match_the_kind(self):
        self.assertRejects(lambda d: scheme_of(d, "sibling")["tiers"][0]["award"].pop("cap_per_sem"),
                           "needs exactly")

    def test_fixed_inr_amount_shape(self):
        self.assertRejects(lambda d: scheme_of(d, "auat-mba")["tiers"][0]["award"].update(amount={"outside": 1}),
                           "amount must be")

    def test_score_tiers_use_min(self):
        def mutate(d):
            tier = scheme_of(d, "auat")["tiers"][0]
            tier["max"] = tier.pop("min")
        self.assertRejects(mutate, "tiers use min")

    def test_tiers_run_best_first(self):
        self.assertRejects(lambda d: scheme_of(d, "auat")["tiers"].reverse(), "best first")

    def test_only_the_last_tier_may_be_unbounded(self):
        self.assertRejects(lambda d: scheme_of(d, "auat")["tiers"][0].pop("min"), "only the last tier")

    def test_flag_input_needs_exactly_one_tier(self):
        def mutate(d):
            tiers = scheme_of(d, "alumni")["tiers"]
            tiers.append(copy.deepcopy(tiers[0]))
        self.assertRejects(mutate, "exactly one tier")

    def test_unknown_degree(self):
        self.assertRejects(lambda d: scheme_of(d, "jee-main")["programs"].update(degrees=["BTech"]),
                           "unknown degree")

    def test_programs_need_levels_or_degrees(self):
        self.assertRejects(lambda d: scheme_of(d, "early-bird").update(programs={"exclude_degrees": []}),
                           "programs needs levels or degrees")

    def test_scheme_must_cover_a_program(self):
        self.assertRejects(lambda d: scheme_of(d, "early-bird-pharmacy").update(
            programs={"degrees": ["B.Pharm"], "exclude_degrees": ["B.Pharm"]}), "covers no program")

    def test_covered_programs_need_a_third_semester(self):
        records = copy.deepcopy(cc.RECORDS)
        next(r for r in records if r["id"] == "btech-civil-engineering")["fees"]["per_semester"] = [1, 2]
        self.assertRejects(lambda d: None, "btech-civil-engineering has fewer than 3 semesters", records)

    def test_verified_must_be_a_boolean(self):
        self.assertRejects(lambda d: d["schemes"][0].update(verified="true"), "verified must be true or false")

    def test_line_verified_must_be_a_boolean(self):
        self.assertRejects(lambda d: d["general_conditions"][0].update(verified="yes"),
                           r"general_conditions\[0\]: verified must be true or false")

    def test_duplicate_id(self):
        self.assertRejects(lambda d: d["schemes"][1].update(id=d["schemes"][0]["id"]), "duplicate id")


CIVIL_RECORD = cc.BY_ID["btech-civil-engineering"]
# per_semester 55450, 55450, 52950, 52950, 52200 x4; admission_fee 43800.


class TestRupees(unittest.TestCase):
    def value(self, award, internal=False):
        return cc._value(award, CIVIL_RECORD, internal)

    def test_share_rounds_half_up_exactly(self):
        self.assertEqual(cc._share(55450, 50, 200), 13863)
        self.assertEqual(cc._share(52950, 50, 200), 13238)
        self.assertEqual(cc._share(55450, 10, 200), 2773)
        self.assertEqual(cc._share(55450, 12.5, 200), 3466)
        self.assertEqual(cc._share(25001, 1, 2), 12501)

    def test_pct_split_takes_half_the_pct_off_semesters_one_and_three(self):
        self.assertEqual(self.value({"kind": "pct_split", "pct": 50}),
                         {"first": 13863, "third": 13238, "total": 27101})

    def test_pct_first_sem_is_a_share_of_semester_one_paid_in_halves(self):
        self.assertEqual(self.value({"kind": "pct_first_sem", "pct": 90}),
                         {"first": 24953, "third": 24952, "total": 49905})

    def test_fixed_inr_splits_in_halves(self):
        self.assertEqual(self.value({"kind": "fixed_inr", "amount": 15000}),
                         {"first": 7500, "third": 7500, "total": 15000})
        self.assertEqual(self.value({"kind": "fixed_inr", "amount": 25001}),
                         {"first": 12501, "third": 12500, "total": 25001})

    def test_fixed_inr_internal_and_external(self):
        both = {"kind": "fixed_inr", "amount": {"external": 30000, "internal": 40000}}
        self.assertEqual(self.value(both)["total"], 30000)
        self.assertEqual(self.value(both, internal=True)["total"], 40000)
        self.assertIsNone(self.value({"kind": "fixed_inr", "amount": {"internal": 25000}}))

    def test_pct_capped_total(self):
        self.assertEqual(self.value({"kind": "pct_capped_total", "pct": 20, "cap_total": 25000}),
                         {"first": 5420, "third": 5420, "total": 10840})
        self.assertEqual(self.value({"kind": "pct_capped_total", "pct": 80, "cap_total": 25000}),
                         {"first": 12500, "third": 12500, "total": 25000})

    def test_pct_every_sem_capped_sums_every_semester(self):
        self.assertEqual(self.value({"kind": "pct_every_sem_capped", "pct": 10, "cap_per_sem": 5000}),
                         {"every": 5000, "total": 40000})
        self.assertEqual(self.value({"kind": "pct_every_sem_capped", "pct": 10, "cap_per_sem": 6000}),
                         {"every": 5545, "total": 42560})

    def test_full_fee_is_semester_one_plus_admission_fee(self):
        self.assertEqual(self.value({"kind": "full_fee"}), {"first": 99250, "third": 0, "total": 99250})

    def test_described_has_no_figure(self):
        self.assertEqual(self.value({"kind": "described", "text": "half tuition"}), {"total": 0})


class TestDescribe(unittest.TestCase):
    def describe(self, award, conditions=()):
        scheme = {"name": "X", "conditions": list(conditions)}
        return cc._describe(scheme, award, cc._value(award, CIVIL_RECORD, False))

    def test_pct_split(self):
        self.assertEqual(self.describe({"kind": "pct_split", "pct": 50}), {
            "name": "X", "award": "50%", "first_semester": "13,863", "third_semester": "13,238",
            "total": "27,101"})

    def test_award_wording_per_kind(self):
        cases = [
            ({"kind": "pct_split", "pct": 12.5}, "12.5%"),
            ({"kind": "pct_first_sem", "pct": 90}, "90% of the first semester fee"),
            ({"kind": "fixed_inr", "amount": 15000}, "Rs 15,000"),
            ({"kind": "pct_capped_total", "pct": 20, "cap_total": 25000}, "20%, up to Rs 25,000"),
            ({"kind": "full_fee"}, "full first semester fee and admission fee"),
        ]
        for award, text in cases:
            with self.subTest(award["kind"]):
                self.assertEqual(self.describe(award)["award"], text)

    def test_every_semester_shape(self):
        self.assertEqual(self.describe({"kind": "pct_every_sem_capped", "pct": 10, "cap_per_sem": 5000}), {
            "name": "X", "award": "10% every semester, up to Rs 5,000", "every_semester_up_to": "5,000",
            "total": "40,000"})

    def test_conditions_only_when_present(self):
        self.assertEqual(self.describe({"kind": "fixed_inr", "amount": 15000}, ["pay on time"])["conditions"],
                         ["pay on time"])


TOOL = "find_scholarships"
CIVIL = "B.Tech (Civil Engineering)"
SPLIT50 = {"kind": "pct_split", "pct": 50}
MBA_TABLE = [{"min": 85, "award": {"kind": "fixed_inr", "amount": {"external": 30000, "internal": 40000}}},
             {"min": 80, "award": {"kind": "fixed_inr", "amount": {"internal": 25000}}}]


def find(**args):
    return cc.handle_tool_call(TOOL, args)


def scheme(sid, award=None, input_name="everyone", tiers=None, committee=False, verified=True, programs=None):
    return {"id": sid, "section": 1, "name": sid.title(), "source_text": "x",
            "programs": programs or {"levels": ["UG"]}, "input": input_name,
            "tiers": tiers if tiers is not None else [{"award": award}],
            "conditions": [], "committee_decides": committee, "verified": verified}


def policy(*schemes):
    return {"source": "test",
            "general_conditions": [{"text": "Only one applies.", "source_text": "x", "verified": True},
                                   {"text": "Unverified rule.", "source_text": "x", "verified": False}],
            "pitch_lines": [{"text": "Book soon.", "source_text": "x", "verified": True}],
            "schemes": list(schemes)}


def facts(response):
    return [a["fact"] for a in response.get("ask_about", [])]


class FakePolicyCase(unittest.TestCase):
    schemes = ()

    def setUp(self):
        patcher = mock.patch.object(cc, "SCHOLARSHIPS", policy(*self.schemes))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestMatching(FakePolicyCase):
    # On Civil: Floor 15,000; Marks 27,101 (90+) or 5,421 (75+); Rank 55,450 (<=1000)
    # or 11,090; Alumni 27,101; Sibling 20,000; Sports is committee-only.
    schemes = (
        scheme("floor", {"kind": "fixed_inr", "amount": 15000}),
        scheme("marks", input_name="qualifying_pct",
               tiers=[{"min": 90, "award": SPLIT50}, {"min": 75, "award": {"kind": "pct_split", "pct": 10}}]),
        scheme("rank", input_name="jee_main_rank",
               tiers=[{"max": 1000, "award": {"kind": "pct_first_sem", "pct": 100}},
                      {"award": {"kind": "pct_first_sem", "pct": 20}}]),
        scheme("sports", {"kind": "full_fee"}, "sports_achiever", committee=True),
        scheme("alumni", SPLIT50, "alumnus"),
        scheme("sibling", {"kind": "fixed_inr", "amount": 20000}, "sibling_enrolled"),
        scheme("hidden", {"kind": "fixed_inr", "amount": 900000}, verified=False),
        scheme("pg-only", {"kind": "fixed_inr", "amount": 800000}, programs={"levels": ["PG"]}),
    )

    def test_highest_wins_and_the_rest_are_listed_highest_first(self):
        response = find(program=CIVIL, qualifying_pct=95, jee_main_rank=1000)
        self.assertEqual(response["best"]["name"], "Rank")
        self.assertEqual(response["also_qualifies"], [{"name": "Marks", "total": "27,101"},
                                                      {"name": "Floor", "total": "15,000"}])

    def test_tier_bounds_are_inclusive(self):
        self.assertEqual(find(program=CIVIL, qualifying_pct=90)["best"]["name"], "Marks")
        self.assertEqual(find(program=CIVIL, jee_main_rank=1000)["best"]["total"], "55,450")
        self.assertIn({"name": "Rank", "total": "11,090"}, find(program=CIVIL, jee_main_rank=1001)["also_qualifies"])

    def test_unverified_and_uncovered_schemes_never_appear(self):
        response = find(program=CIVIL, qualifying_pct=99, situations=cc.SITUATIONS)
        self.assertEqual(response["best"]["name"], "Marks")
        text = str(response)
        self.assertNotIn("Hidden", text)
        self.assertNotIn("Pg-Only", text)

    def test_committee_scheme_is_never_best(self):
        response = find(program=CIVIL, situations=["sports_achiever"])
        self.assertEqual(response["best"]["name"], "Floor")
        self.assertEqual(response["may_also_apply"], [{"name": "Sports"}])

    def test_ask_about_is_highest_first_and_capped_at_three(self):
        self.assertEqual(find(program=CIVIL)["ask_about"], [
            {"fact": "jee_main_rank", "up_to": "55,450"},
            {"fact": "qualifying_pct", "up_to": "27,101"},
            {"fact": "studied_at_adamas_university", "up_to": "27,101"}])

    def test_ask_about_only_facts_that_beat_best(self):
        response = find(program=CIVIL, qualifying_pct=95, jee_main_rank=1000)
        self.assertEqual(response["best"]["name"], "Rank")
        self.assertNotIn("ask_about", response)

    def test_given_and_ruled_out_facts_are_not_asked(self):
        self.assertEqual(facts(find(program=CIVIL, qualifying_pct=80)),
                         ["jee_main_rank", "studied_at_adamas_university", "sibling_enrolled"])
        self.assertEqual(facts(find(program=CIVIL, ruled_out=["jee_main_rank"])),
                         ["qualifying_pct", "studied_at_adamas_university", "sibling_enrolled"])

    def test_alumni_falls_back_to_the_school_question(self):
        response = find(program=CIVIL, ruled_out=["studied_at_adamas_university"])
        self.assertIn({"fact": "studied_at_adamas_school", "up_to": "27,101"}, response["ask_about"])

    def test_school_alumnus_matches_alumni(self):
        self.assertEqual(find(program=CIVIL, situations=["studied_at_adamas_school"])["best"]["name"], "Alumni")

    def test_rules_and_pitch_lines_are_the_verified_texts(self):
        response = find(program=CIVIL)
        self.assertEqual(response["rules"], ["Only one applies."])
        self.assertEqual(response["pitch_lines"], ["Book soon."])

    # Review focus.
    def test_float_rank_matches_like_an_integer(self):
        self.assertEqual(find(program=CIVIL, jee_main_rank=1000.0)["best"]["name"], "Rank")

    def test_stated_fact_wins_over_ruled_out(self):
        response = find(program=CIVIL, situations=["sibling_enrolled"], ruled_out=["sibling_enrolled"])
        self.assertEqual(response["best"]["name"], "Sibling")


class TestNoBest(FakePolicyCase):
    schemes = (scheme("pg-only", {"kind": "fixed_inr", "amount": 800000}, programs={"levels": ["PG"]}),)

    def test_no_best_means_no_rules_or_pitch(self):
        self.assertEqual(find(program=CIVIL), {"program": CIVIL})


class TestInternalStudents(FakePolicyCase):
    schemes = (scheme("table", input_name="auat_pct", tiers=MBA_TABLE),)

    def test_external_amount_by_default_and_asks_about_the_upgrade(self):
        response = find(program=CIVIL, auat_pct=90)
        self.assertEqual(response["best"]["total"], "30,000")
        self.assertEqual(response["ask_about"], [{"fact": "studied_at_adamas_university", "up_to": "40,000"}])

    def test_internal_amount_for_an_adamas_university_graduate(self):
        response = find(program=CIVIL, auat_pct=90, situations=["studied_at_adamas_university"])
        self.assertEqual(response["best"]["total"], "40,000")

    def test_internal_only_tier_is_asked_not_matched(self):
        self.assertEqual(find(program=CIVIL, auat_pct=82), {
            "program": CIVIL, "ask_about": [{"fact": "studied_at_adamas_university", "up_to": "25,000"}]})
        self.assertEqual(find(program=CIVIL, auat_pct=82, ruled_out=["studied_at_adamas_university"]),
                         {"program": CIVIL})

    def test_unknown_score_is_asked_at_its_best_value(self):
        self.assertEqual(find(program=CIVIL)["ask_about"], [{"fact": "auat_pct", "up_to": "40,000"}])


class TestErrors(unittest.TestCase):
    def test_errors(self):
        cases = [
            ({}, "program is required"),
            ({"program": "  "}, "program is required"),
            ({"program": CIVIL, "qualifying_pct": "ninety"}, "qualifying_pct must be a number from 0 to 100"),
            ({"program": CIVIL, "qualifying_pct": True}, "qualifying_pct must be a number"),
            ({"program": CIVIL, "qualifying_pct": 101}, "qualifying_pct must be a number"),
            ({"program": CIVIL, "qualifying_pct": 8.2}, "looks like a CGPA; ask for the percentage"),
            ({"program": CIVIL, "qualifying_pct": 10}, "looks like a CGPA; ask for the percentage"),
            ({"program": CIVIL, "clat_score": 121}, "clat_score must be a number from 0 to 120"),
            ({"program": CIVIL, "jee_main_rank": 0}, "jee_main_rank must be a rank, 1 or more"),
            ({"program": CIVIL, "situations": ["rich_uncle"]}, "unknown situation 'rich_uncle'"),
            ({"program": CIVIL, "ruled_out": ["neet_rank"]}, "unknown fact 'neet_rank'"),
            ({"program": CIVIL, "situations": 5}, "situations must be a list"),
            ({"program": CIVIL, "budget": 5}, "unknown argument 'budget'"),
        ]
        for args, message in cases:
            with self.subTest(args):
                self.assertIn(message, cc.handle_tool_call(TOOL, args)["error"])

    def test_single_situation_string_is_accepted(self):
        self.assertNotIn("error", find(program=CIVIL, situations="sibling_enrolled"))

    def test_unresolved_program(self):
        self.assertEqual(find(program="zzzz qqqq"), {"status": "not_found"})
        response = find(program="M.Pharm")
        self.assertEqual(response["status"], "ambiguous")
        self.assertEqual(len(response["candidates"]), 2)

    def test_result_status(self):
        self.assertEqual(cc.result_status(TOOL, {"error": "x"}), "error")
        self.assertEqual(cc.result_status(TOOL, {"status": "not_found"}), "not_found")
        self.assertEqual(cc.result_status(TOOL, {"program": "X"}), "best=none")
        self.assertEqual(cc.result_status(TOOL, {"program": "X", "best": {"name": "JEE Main Scholarship"}}),
                         "best=jee-main-scholarship")


class TestGolden(unittest.TestCase):
    """Real, operator-verified scholarships.json (spec 13.6)."""

    def test_btech_civil_with_jee_rank(self):
        response = find(program=CIVIL, jee_main_rank=80000)
        self.assertEqual(response["best"], {
            "name": "JEE Main Scholarship", "award": "50%", "first_semester": "13,863",
            "third_semester": "13,238", "total": "27,101", "conditions": ["JEE Main 2026 All India Rank"]})
        self.assertEqual(response["also_qualifies"], [{"name": "Early Bird Scholarship", "total": "15,000"}])
        self.assertEqual(response["ask_about"], [{"fact": "aujet_rank", "up_to": "55,450"},
                                                 {"fact": "wbjee_rank", "up_to": "43,360"},
                                                 {"fact": "sibling_enrolled", "up_to": "40,000"}])

    def test_bpharm_gets_the_pharmacy_early_bird(self):
        self.assertEqual(find(program="B.Pharm")["best"], {
            "name": "Early Bird Scholarship", "award": "Rs 10,000", "first_semester": "5,000",
            "third_semester": "5,000", "total": "10,000",
            "conditions": ["limited to the first 250 admissions", "fee paid within the payment deadline"]})

    def test_mpharm_has_no_scholarship(self):
        self.assertEqual(find(program="M.Pharm (Pharmaceutics)"), {"program": "M.Pharm (Pharmaceutics)"})

    def test_mba_with_cat(self):
        self.assertEqual(find(program="MBA", cat_pct=85)["best"], {
            "name": "CAT Scholarship for MBA", "award": "100% of the first semester fee",
            "first_semester": "96,625", "third_semester": "96,625", "total": "1,93,250",
            "conditions": ["valid CAT score"]})

    def test_mba_with_auat_asks_about_adamas_graduation(self):
        response = find(program="MBA", auat_pct=87, ruled_out=["aujet_rank", "cat_pct", "mat_pct"])
        self.assertEqual(response["best"]["name"], "AUAT Scholarship for MBA")
        self.assertEqual(response["best"]["total"], "30,000")
        self.assertEqual(response["ask_about"], [{"fact": "studied_at_adamas_university", "up_to": "96,001"},
                                                 {"fact": "qualifying_pct", "up_to": "40,000"}])

    def test_sibling_every_semester(self):
        self.assertEqual(find(program=CIVIL, situations=["sibling_enrolled"])["best"], {
            "name": "Sibling Scholarship", "award": "10% every semester, up to Rs 5,000",
            "every_semester_up_to": "5,000", "total": "40,000",
            "conditions": ["the brother or sister must be currently enrolled at Adamas University in a "
                           "program other than B.Pharm", "only one sibling can get it"]})

    def test_exam_that_does_not_apply_is_ignored(self):
        # Review focus: CAT means nothing for B.Tech; no error, Early Bird still best.
        self.assertEqual(find(program=CIVIL, cat_pct=99)["best"]["name"], "Early Bird Scholarship")


class TestBudgets(unittest.TestCase):
    COMBOS = [{}, {"qualifying_pct": 96}, {"qualifying_pct": 61, "auat_pct": 86},
              {"jee_main_rank": 1000, "wbjee_rank": 100, "aujet_rank": 5, "qualifying_pct": 96},
              {"cat_pct": 99, "mat_pct": 99, "clat_score": 95, "auat_pct": 90}]

    def test_every_response_fits(self):
        # Measured while planning: worst 1255 without situations, 1529 with all seven.
        for record in cc.RECORDS:
            for combo in self.COMBOS:
                for situations in ([], cc.SITUATIONS):
                    args = dict(combo, program=record["name"], situations=situations)
                    with self.subTest(record=record["id"], combo=combo, situations=bool(situations)):
                        response = find(**args)
                        self.assertEqual(response.get("program"), record["name"])
                        self.assertLessEqual(cc.response_chars(response), 1800 if situations else 1500)


class TestDeclaration(unittest.TestCase):
    def test_declaration(self):
        spec = next(s for s in cc.TOOL_SPECS if s["name"] == TOOL)
        props = spec["parameters"]["properties"]
        self.assertEqual(spec["parameters"]["required"], ["program"])
        self.assertEqual(set(props), {"program", "situations", "ruled_out", *cc.SCORE_INPUTS, *cc.RANK_INPUTS})
        self.assertEqual(props["situations"]["items"]["enum"], cc.SITUATIONS)
        self.assertEqual(props["ruled_out"]["items"]["enum"], cc.FACTS)
        # Review focus: the model must not guess a situation.
        self.assertIn("confirmed", props["situations"]["description"])


if __name__ == "__main__":
    unittest.main()
