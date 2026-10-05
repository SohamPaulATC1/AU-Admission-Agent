"""course_catalog: name resolution, tool responses, errors and size budgets (spec section 6).

Runs on the committed catalog/*.json only, so it also runs on EC2.
"""
import collections
import json
import unittest

import course_catalog as cc
from tests import tool_playground

CSE = "B.Tech (Computer Science and Engineering)"


def details(*programs, fields=None):
    args = {"programs": list(programs)}
    if fields is not None:
        args["fields"] = fields
    return cc.handle_tool_call("get_program_details", args)


def one(query, fields=None):
    return details(query, fields=fields)["results"][0]


class TestHelpers(unittest.TestCase):
    def test_normalize_joins_split_degree_tokens(self):
        self.assertEqual(cc.normalize("B. Tech (CSE)"), "btech cse")
        self.assertEqual(cc.normalize("M. Sc."), "msc")
        self.assertEqual(cc.normalize("B.B.A. LL.B (Hons)"), "bba llb hons")
        self.assertEqual(cc.normalize("AI & ML"), "ai and ml")

    def test_inr_uses_indian_grouping(self):
        for amount, text in ((0, "0"), (999, "999"), (43800, "43,800"), (108250, "1,08,250"),
                             (891800, "8,91,800"), (12345678, "1,23,45,678")):
            self.assertEqual(cc.inr(amount), text)

    def test_school_counts(self):
        self.assertEqual(collections.Counter(r["school"]["code"] for r in cc.RECORDS), {
            "SoBAS": 16, "SoB": 6, "SoE": 3, "SoET": 20, "SoHMS": 12,
            "SoLJ": 3, "SoLACS": 14, "SoLB": 8, "SoMC": 4, "SoSA": 1,
        })

    def test_alias_keys_do_not_collide_with_another_record(self):
        owners = collections.defaultdict(set)
        for entry in cc.INDEX:
            for k in entry["alias_keys"] | {entry["name_key"]}:
                owners[k].add(entry["record"]["id"])
        shared = {k: ids for k, ids in owners.items() if len(ids) > 1}
        # Deliberately shared: "law" names the three SoLJ programs.
        self.assertEqual(shared, {"law": {"ba-llb-hons", "bba-llb-hons", "llm"}})

    def test_every_expansion_matches_some_record(self):
        for short, words in cc.EXPANSIONS.items():
            with self.subTest(short):
                self.assertTrue(any(words <= e["match_tokens"] | e["dept_tokens"] for e in cc.INDEX))


class TestResolution(unittest.TestCase):
    def assertFound(self, query, name):
        status, record, _, _ = cc.resolve(query)
        self.assertEqual((status, record and record["name"]), ("found", name), query)

    def test_aliases_and_short_forms(self):
        self.assertFound("cse", CSE)
        self.assertFound("CSE", CSE)
        self.assertFound("computer science engineering", CSE)
        self.assertFound("b tech cse", CSE)
        self.assertFound("btech ai ml", "B.Tech CSE (Artificial Intelligence & Machine Learning)")
        self.assertFound("b.tech ai & ml", "B.Tech CSE (Artificial Intelligence & Machine Learning)")
        self.assertFound("ece", "B.Tech (Electronics and Communication Engineering)")
        self.assertFound("mechanical engg", "B.Tech (Mechanical Engineering)")
        self.assertFound("mlt", "Bachelor Of Medical Laboratory Science (BMLS)")

    def test_canonical_names_ids_and_fee_file_names(self):
        for record in cc.RECORDS:
            with self.subTest(record["id"]):
                self.assertFound(record["name"], record["name"])
                self.assertFound(record["id"], record["name"])
                for alias in record["aliases"]:
                    if alias != "law":
                        self.assertFound(alias, record["name"])

    def test_degree_word_narrows_the_pool(self):
        self.assertFound("MBA", "MBA")
        self.assertFound("bsc psychology", "B.Sc (Psychology)")
        self.assertFound("ma english", "M.A (English Language and Literature)")
        self.assertFound("m tech data science", "M.Tech (Data Science and Digital Transformation)")
        self.assertFound("btech biotech", "B.Tech (Biotechnology)")

    def test_single_clear_subject(self):
        self.assertFound("civil", "B.Tech (Civil Engineering)")
        self.assertFound("optometry", "Bachelor of Optometry")
        self.assertFound("medical physics", "Post-M.Sc Diploma in Medical Physics")

    def test_ambiguous(self):
        status, _, candidates, more = cc.resolve("data science")
        self.assertEqual(status, "ambiguous")
        self.assertEqual(candidates, [
            "B.Tech CSE (Data Science)",
            "B.Sc (Applied Statistics and Data Science)",
            "M.Sc. (Applied Statistics and Data Science)",
            "M.Tech (Data Science and Digital Transformation)",
        ])
        self.assertEqual(more, 2)
        self.assertEqual(cc.resolve("law")[2], ["LL.M", "B.A. LL.B (Hons)", "B.B.A. LL.B (Hons)"])
        self.assertEqual(cc.resolve("llb")[2], ["B.A. LL.B (Hons)", "B.B.A. LL.B (Hons)"])
        self.assertEqual(cc.resolve("psychology")[0], "ambiguous")
        self.assertEqual(len(cc.resolve("psychology")[2]), 4)

    def test_department_only_word_is_never_found(self):
        # Final review: a word that only matches the department (there is no
        # M.Tech CSE, no B.Tech computer application) must not make a found.
        for query in ("mtech computer science", "btech computer application",
                      "ba social science", "ma social science", "mtech civil engineering"):
            status, record, _, _ = cc.resolve(query)
            self.assertNotEqual(status, "found", f"{query!r} -> {record and record['name']}")

    def test_not_found(self):
        # "computer wala": untranslated caller words must not land on a wrong program.
        for query in ("mbbs", "nursing", "information technology", "", "   ", "course", "it",
                      "computer wala"):
            self.assertEqual(cc.resolve(query)[0], "not_found", repr(query))

    def test_constants_are_pinned(self):
        self.assertEqual((cc.FOUND_SCORE, cc.CANDIDATE_SCORE, cc.SCORE_MARGIN, cc.DEPARTMENT_WEIGHT),
                         (0.75, 0.6, 0.2, 0.5))
        self.assertEqual((cc.MAX_PROGRAMS, cc.LIST_NAMES_MAX, cc.MAX_CANDIDATES), (3, 20, 4))


class TestListPrograms(unittest.TestCase):
    def call(self, **args):
        return cc.handle_tool_call("list_programs", args)

    def test_no_arguments_groups_by_school(self):
        response = self.call()
        self.assertEqual(list(response), ["by_school"])
        self.assertEqual(len(response["by_school"]), 10)
        self.assertIn({"school": "SoET - School of Engineering and Technology", "count": 20},
                      response["by_school"])
        self.assertEqual(sum(s["count"] for s in response["by_school"]), 87)
        self.assertEqual(cc.handle_tool_call("list_programs", None), response)

    def test_small_result_lists_names(self):
        response = self.call(degree="B.Tech")
        self.assertEqual(response["count"], 18)
        self.assertEqual(len(response["programs"]), 18)
        self.assertEqual(response["programs"][-2:], ["B.Tech (Biotechnology)", "B.Tech (Food Technology)"])
        self.assertEqual(self.call(degree="btech"), response)
        self.assertEqual(self.call(school="soet")["count"], 20)

    def test_large_result_groups_by_degree(self):
        response = self.call(level="UG")
        self.assertEqual(response["count"], 53)
        self.assertNotIn("programs", response)
        self.assertEqual(response["by_degree"][:2], [{"degree": "B.Tech", "count": 18},
                                                     {"degree": "B.Sc", "count": 15}])

    def test_filters_combine(self):
        self.assertEqual(self.call(level="PG", degree="B.Tech"), {"count": 0})
        self.assertEqual(self.call(max_total_fee=300000.0, level="UG"), {
            "count": 3, "programs": ["B.A (Education)", "B.Ed", "B. A (Bengali Language and Literature)"]})
        response = self.call(keyword="data science")
        self.assertEqual(response["count"], 6)

    def test_max_total_fee_is_inclusive(self):
        cse_total = next(r for r in cc.RECORDS if r["name"] == CSE)["fees"]["total"]
        names = self.call(degree="B.Tech", max_total_fee=cse_total)["programs"]
        self.assertIn(CSE, names)
        self.assertNotIn(CSE, self.call(degree="B.Tech", max_total_fee=cse_total - 1)["programs"])

    def test_errors(self):
        for args, fragment in (({"degree": "MBBS"}, "unknown degree"),
                               ({"level": "PhD"}, "unknown level"),
                               ({"school": "SoX"}, "unknown school"),
                               ({"foo": 1}, "unknown argument 'foo'"),
                               ({"max_total_fee": True}, "max_total_fee"),
                               ({"max_total_fee": "5 lakh"}, "max_total_fee"),
                               ({"keyword": "course"}, "keyword")):
            with self.subTest(args):
                self.assertIn(fragment, cc.handle_tool_call("list_programs", args)["error"])
        self.assertEqual(cc.handle_tool_call("list_programs", {"level": None}), self.call())


class TestProgramDetails(unittest.TestCase):
    def test_default_fields(self):
        entry = one("cse")
        self.assertEqual(list(entry), ["query", "status", "name", "school", "department", "level",
                                       "duration", "eligibility", "fees"])
        self.assertEqual(entry["school"], "SoET - School of Engineering and Technology")
        self.assertEqual(entry["duration"], "4 years")
        self.assertEqual(entry["fees"], {
            "session": "2027",
            "first_semester": "1,08,250",
            "admission_fee": "43,800",
            "admission_fee_note": "one-time, paid with the first semester fee; includes T-shirt and blazer",
            "due_at_admission": "1,52,050",
            "total": "8,91,800",
        })

    def test_only_requested_fields(self):
        self.assertEqual(list(one("cse", ["eligibility"])), ["query", "status", "name", "eligibility"])
        self.assertEqual(one("cse", ["seats"])["seats"], 300)
        breakdown = one("cse", ["fee_breakdown"])["fee_breakdown"]
        self.assertEqual(breakdown["per_year"], ["2,16,500", "2,11,500", "2,10,000", "2,10,000"])
        self.assertEqual(len(breakdown["per_semester"]), 8)
        self.assertEqual(one("cse", [])["fees"]["total"], "8,91,800")  # empty list means default

    def test_add_ons_only_with_fees(self):
        sap = one("sap", ["fees"])
        self.assertEqual(sap["fees"]["add_ons"],
                         [{"name": "SAP add-on certification", "duration": "1 year", "fee": "65,000"}])
        self.assertNotIn("add_ons", one("cse", ["fees"])["fees"])
        self.assertNotIn("add_ons", json.dumps(one("sap", ["overview", "seats"])))

    def test_seats_note_and_duration_note(self):
        entry = one("m pharm pharmacology", ["seats"])
        self.assertEqual(entry["seats_note"], "intake shared with M.Pharm (Pharmaceutics)")
        self.assertTrue(one("medical physics", ["overview"])["duration"].startswith("1st Year"))

    def test_mixed_statuses_and_string_programs(self):
        response = details("cse", "data science", "mbbs", fields=["fees"])
        self.assertEqual([r["status"] for r in response["results"]], ["found", "ambiguous", "not_found"])
        self.assertEqual(response["results"][2], {"query": "mbbs", "status": "not_found"})
        self.assertNotIn("more", one("law"))
        self.assertEqual(cc.handle_tool_call("get_program_details", {"programs": "cse"}),
                         details("cse"))
        # The SDK may hand over arrays as its own sequence types, not lists.
        self.assertEqual(cc.handle_tool_call("get_program_details",
                                             {"programs": ("cse",), "fields": ("fees",)}),
                         details("cse", fields=["fees"]))
        self.assertEqual(cc.result_status("get_program_details", response), "found,ambiguous,not_found")

    def test_errors(self):
        for args, fragment in (({}, "programs is required"),
                               ({"programs": []}, "1 to 3"),
                               ({"programs": ["a", "b", "c", "d"]}, "1 to 3"),
                               ({"programs": [""]}, "non-empty"),
                               ({"programs": [5]}, "non-empty"),
                               ({"programs": ["cse"], "fields": ["price"]}, "unknown field 'price'"),
                               ({"programs": ["cse"], "extra": 1}, "unknown argument 'extra'")):
            with self.subTest(args):
                response = cc.handle_tool_call("get_program_details", args)
                self.assertIn(fragment, response["error"])
                self.assertEqual(cc.result_status("get_program_details", response), "error")
        self.assertEqual(cc.handle_tool_call("bookSeat", {}), {"error": "unknown tool bookSeat"})


class TestSizeBudgets(unittest.TestCase):
    """Spec 6.5, in characters of compact JSON (about 4 characters per token)."""

    def test_every_list_response_fits(self):
        calls = [{}] + [{"degree": d} for d in cc.DEGREES] + [{"school": s} for s in cc.SCHOOL_CODES] \
            + [{"level": lv} for lv in cc.LEVELS]
        for args in calls:
            with self.subTest(args):
                self.assertLessEqual(cc.response_chars(cc.handle_tool_call("list_programs", args)), 1000)

    def test_one_program_default_fields(self):
        self.assertLessEqual(cc.response_chars(details("cse")), 800)
        for record in cc.RECORDS:
            with self.subTest(record["id"]):
                self.assertLessEqual(cc.response_chars(details(record["name"])), 1400)

    def test_three_programs_fees_only(self):
        self.assertLessEqual(cc.response_chars(details("cse", "ece", "civil", fields=["fees"])), 1000)

    def test_declarations(self):
        self.assertLessEqual(cc.response_chars(cc.TOOL_SPECS), 1800)


class TestToolSpecs(unittest.TestCase):
    def test_names_and_enums_come_from_the_data(self):
        self.assertEqual(cc.TOOL_NAMES, {"list_programs", "get_program_details"})
        self.assertEqual([s["name"] for s in cc.TOOL_SPECS], ["list_programs", "get_program_details"])
        props = cc.TOOL_SPECS[0]["parameters"]["properties"]
        self.assertEqual(props["degree"]["enum"], cc.DEGREES)
        self.assertEqual(len(props["degree"]["enum"]), 22)
        self.assertEqual(props["school"]["enum"], sorted(cc.SCHOOL_CODES))
        self.assertEqual(props["level"]["enum"], ["UG", "PG", "Diploma"])
        details_spec = cc.TOOL_SPECS[1]["parameters"]
        self.assertEqual(details_spec["required"], ["programs"])
        self.assertEqual(details_spec["properties"]["fields"]["items"]["enum"], cc.FIELDS)

    def test_specs_build_sdk_declarations(self):
        from google.genai import types
        for spec in cc.TOOL_SPECS:
            declaration = types.FunctionDeclaration(**spec)
            self.assertEqual(declaration.name, spec["name"])


class TestPlayground(unittest.TestCase):
    def test_key_value_pairs(self):
        self.assertEqual(tool_playground.parse_args("get_program_details", ["programs=cse,ece", "fields=fees"]),
                         {"programs": ["cse", "ece"], "fields": ["fees"]})
        self.assertEqual(tool_playground.parse_args("list_programs", ["max_total_fee=300000", "degree=B.Tech"]),
                         {"max_total_fee": 300000, "degree": "B.Tech"})

    def test_raw_json(self):
        self.assertEqual(tool_playground.parse_args("get_program_details", ['{"programs": ["ai ml"]}']),
                         {"programs": ["ai ml"]})

    def test_malformed_pair(self):
        with self.assertRaises(ValueError):
            tool_playground.parse_args("list_programs", ["degree"])


if __name__ == "__main__":
    unittest.main()
