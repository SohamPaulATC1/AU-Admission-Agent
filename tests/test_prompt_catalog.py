"""The prompts carry the catalog map and tool rules, and no fee figures (spec section 10).

TestPromptCatalog checks prompt_au.txt (v1, the rollback); TestPromptCatalogV2
runs the same checks on prompt_au_v2.txt (the live prompt since 2026-10-07),
and TestPromptV2Counselor pins what v2 adds: Neha leads the call toward a
campus visit.
"""

import os
import re
import unittest

import course_catalog

PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "PROMPT_FILES")
PROMPT = os.path.join(PROMPT_DIR, "prompt_au.txt")
PROMPT_V2 = os.path.join(PROMPT_DIR, "prompt_au_v2.txt")


class TestPromptCatalog(unittest.TestCase):
    path = PROMPT

    @classmethod
    def setUpClass(cls):
        with open(cls.path, encoding="utf-8") as f:
            cls.text = f.read()

    def test_formats_like_app_py_does(self):
        # app.py fills these two with str.format; any other brace would raise.
        filled = self.text.format(user_name="Asha", phone_number="9800000000")
        self.assertIn("Asha", filled)
        self.assertIn("9800000000", filled)

    def test_names_all_tools(self):
        for name in ("list_programs", "get_program_details", "find_eligible_programs", "find_scholarships",
                     "transferCall", "endCall"):
            self.assertIn(name, self.text)

    def test_school_map_matches_the_catalog(self):
        # Degree types are not repeated here: the list_programs degree enum
        # already carries them, and the prompt is re-billed every turn.
        for code, name in course_catalog.SCHOOLS:
            self.assertIn(f"{code} - {name}", self.text)

    def test_no_fee_figures(self):
        self.assertIsNone(re.search(r"\d{1,2},\d{2},\d{3}|\d{2},\d{3}|\d{5,}", self.text))

    def test_reuse_rule_allows_fetching_a_missing_field(self):
        # Final review: a blanket "do not call again for the same program" pushes
        # the model to invent eligibility after a fees-only lookup.
        self.assertNotIn("Do not call the tool again for the same program", self.text)
        self.assertIn("if a needed field was not fetched yet", self.text)

    def test_error_result_rule(self):
        # Final review: without a rule the model may read "unknown degree 'MBBS';
        # use one of: ..." to the caller.
        self.assertIn("`error`", self.text)
        self.assertIn("never read the error aloud", self.text)

    def test_language_mix_is_critical_with_examples(self):
        # TEST6: the model answered in pure, formal Bengali ("সর্বোচ্চ শিক্ষাগত
        # যোগ্যতা") after the rule was shortened to "Benglish (the same mix)".
        self.assertIn("Language mix (CRITICAL)", self.text)
        self.assertIn("আপনার highest qualification", self.text)
        self.assertIn("आपकी highest qualification", self.text)

    def test_reply_language_counts_as_the_choice(self):
        # TEST6: the caller kept answering in Bengali without naming a language,
        # and the agent carried on in English.
        self.assertIn("answers in Bengali or Hindi", self.text)

    def test_open_eligibility_rule(self):
        # Spec 11.1 and 11.5: "appear eligible", never a negative verdict.
        self.assertIn("you appear eligible for", self.text)
        self.assertIn('Never say "you are not eligible"', self.text)
        self.assertEqual(self.text.count("not eligible"), 1)
        self.assertNotIn('Do NOT say "you are eligible"', self.text)
        self.assertIn("admissions team confirms final eligibility", self.text)
        # Review focus: the tool cannot tell a CGPA from a percentage.
        self.assertIn("CGPA", self.text)
        self.assertIn("expected percentage", self.text)
        self.assertIn("`not_covered`", self.text)

    def test_eligibility_verdict_only_from_the_eligibility_tool(self):
        # Final review finding 1: a grouped (by_degree) result with no further
        # narrowing must never send the model to list_programs for names, or a
        # caller who fails a program's rule can be read "appear eligible".
        self.assertIn("never use `list_programs`", self.text)
        self.assertIn("narrow once more with `degree` or `keyword`", self.text)
        self.assertIn("still grouped", self.text)

    def test_zero_count_falls_back_to_reading_eligibility_text(self):
        # Final review finding 2: with every rule unverified, count is always
        # 0 today; routing straight to a counselor is worse than phase 1,
        # which read the named program's eligibility text instead.
        self.assertIn("ask which specific program interests them", self.text)

    def section(self, heading):
        return self.text.split(heading, 1)[1].split("\n## ", 1)[0]

    def test_scholarship_section(self):
        # Spec 13.5; CGPA and "never a guess" are review-focus items: the tool
        # cannot tell a CGPA from a percentage or a guessed situation from a fact.
        section = self.section("## SCHOLARSHIPS")
        for phrase in ("`find_scholarships`", "only one scholarship applies", "`ask_about`", "`ruled_out`",
                       "never promise it", "`pitch_lines`", "CGPA", "never a guess",
                       "Adamas University Admission Test"):
            with self.subTest(phrase):
                self.assertIn(phrase, section)

    def test_neha_raises_scholarships_herself(self):
        # Operator 2026-10-06: callers do not know Adamas gives scholarships, so
        # Neha brings it up unprompted, once, and never nags after a decline.
        section = self.section("## SCHOLARSHIPS")
        for phrase in ("usually do not know", "do not wait to be asked", "once per call",
                       "do not raise it again", "right away"):
            with self.subTest(phrase):
                self.assertIn(phrase, section)

    def test_marks_are_asked_before_the_first_scholarship_call(self):
        # Operator 2026-10-06: marks first, so ask_about does not lead with
        # unreachable rank-exam tiers. Call flow step 4 asks for them, and the
        # scholarship section says to have them before the first call.
        step4 = next(line for line in self.text.splitlines() if line.startswith("4. **Academic profile"))
        self.assertIn("marks or percentage", step4)
        section = self.section("## SCHOLARSHIPS")
        self.assertIn("before the first call", section)
        self.assertIn("pass it as `qualifying_pct`", section)

    def test_scholarships_no_longer_routed_to_a_counselor(self):
        self.assertNotIn("Scholarship", self.section("## NOT AVAILABLE"))

    def test_ask_about_question_ends_the_scholarship_turn(self):
        # Text-only test 2026-10-07: with ask_about set, Neha presented best and
        # closed with "anything else?" (call flow step 6) in 2 of 2 runs, never
        # asking the rank that could raise the scholarship.
        section = self.section("## SCHOLARSHIPS")
        self.assertIn("end your turn with the first `ask_about` question", section)
        self.assertIn("instead of asking if they would like to know anything else", section)
        self.assertLess(section.index("Present `best`"), section.index("`ask_about` question"))

    def test_loose_program_name_goes_to_get_program_details(self):
        # Text-only test 2026-10-07: "B.Tech Computer Science" went to
        # list_programs and the call stalled on "which specialisation?";
        # get_program_details resolves loose names (or returns ambiguous).
        self.assertIn("even a loose one", self.text)
        self.assertIn("resolves loose names", self.text)

    def test_not_offered_only_after_a_lookup(self):
        # Text-only test 2026-10-07: "Underwater Basket Weaving" got "we do not
        # offer that" with no tool call; a real program under another name
        # would get the same answer.
        self.assertIn("Never say a program is not offered unless a lookup on this call found nothing", self.text)

    def test_eligibility_more_narrows_by_keyword(self):
        # A one-degree result names 10 programs and counts the rest in `more`.
        self.assertIn("If it also returns `more`", self.text)

    def test_marks_for_a_chosen_program_go_to_scholarships(self):
        # Text-only test 2026-10-07: after the scholarship offer, "I got 82
        # percent, with Physics, Chemistry and Maths" went to
        # find_eligible_programs about 1 time in 16.
        self.assertIn("only when the caller asks what they can apply for", self.text)
        self.assertIn("marks shared for a program already chosen, or in reply to the scholarship offer, go to `find_scholarships`",
                      self.text)

    def test_exam_rule_allows_scholarship_questions(self):
        # The old "never ask for any other exam" line would stop Neha asking for
        # the JEE rank that ask_about suggests.
        self.assertIn("for a scholarship is fine", self.text)
        self.assertIn("no WBJEE/JEE demand for B.Tech", self.text)


class TestPromptCatalogV2(TestPromptCatalog):
    path = PROMPT_V2

    def test_ask_about_question_ends_the_scholarship_turn(self):
        # v2 bans "anything else?" outright, so the v1 wording ("instead of
        # asking if they would like to know anything else") does not apply.
        section = self.section("## SCHOLARSHIPS")
        self.assertIn("end your turn with the first `ask_about` question", section)
        self.assertLess(section.index("Present `best`"), section.index("`ask_about` question"))


class TestPromptV2Counselor(unittest.TestCase):
    """Operator 2026-10-07: Neha leads the call toward admission instead of
    answering and asking "anything else?"; the close is a campus visit day."""

    @classmethod
    def setUpClass(cls):
        with open(PROMPT_V2, encoding="utf-8") as f:
            cls.text = f.read()

    def section(self, heading):
        return self.text.split(heading, 1)[1].split("\n## ", 1)[0]

    def test_never_asks_anything_else(self):
        # The ban itself is the only mention of either phrase.
        self.assertIn('Never end a turn with "anything else?" or "do you need more information?"', self.text)
        self.assertEqual(self.text.lower().count("anything else"), 1)
        self.assertEqual(self.text.lower().count("more information"), 1)

    def test_no_know_more_question_either(self):
        # Text-only test 2026-10-07: after fixing Saturday, Neha asked "Would you
        # like to know more about the B.Sc or B.Tech in Biotechnology?".
        self.assertIn('any "would you like to know more" question', self.text)

    def test_close_once_the_day_is_fixed(self):
        self.assertIn("Once a day is fixed", self.section("## CAMPUS VISIT"))

    def test_consent_line_already_in_the_callers_language(self):
        # Text-only test 2026-10-07: a caller answering "হ্যাঁ, বলুন।" got the
        # consent line in English (v2 2 of 5, v1 1 of 5) before Neha switched.
        step1 = next(line for line in self.text.splitlines() if line.startswith("1. **Greeting"))
        self.assertIn("your very next sentence", step1)

    def test_reply_language_rule_with_the_test6_phrase(self):
        # "হ্যাঁ, বলুন" (yes, go ahead) names no language; v2 still answered the
        # consent line in English 2 of 6 times with the rule only in step 1.
        section = self.section("## LANGUAGE & TONE")
        self.assertIn('"হ্যাঁ, বলুন"', section)

    def test_silent_after_a_terminal_tool(self):
        # Text-only test 2026-10-07, with app.py's exact endCall reply: the model
        # said "The call has ended." after it, 1 of 3 closings.
        self.assertIn("after calling it, say nothing more", self.text)

    def test_ask_about_is_capped_before_the_visit(self):
        # Text-only test 2026-10-07: vague replies ("Hmm, okay", "Maybe next
        # week") got three rank questions in a row and no campus visit invite.
        section = self.section("## SCHOLARSHIPS")
        self.assertIn("Ask at most two", section)
        self.assertIn("if one gets no clear answer, move on to the campus visit", section)

    def test_every_turn_ends_with_a_forward_question(self):
        self.assertIn("End every turn with one question that moves the call forward", self.text)

    def test_campus_visit_close(self):
        section = self.section("## CAMPUS VISIT")
        self.assertIn("Which day suits you", section)
        self.assertIn("our admission team will call you to confirm the timing and directions", section)
        self.assertIn("Fix only the day", section)

    def test_summary_records_the_visit_day(self):
        step8 = next(line for line in self.text.splitlines() if line.startswith("8. **Closing"))
        self.assertIn("campus visit day", step8)

    def test_no_invented_urgency(self):
        self.assertIn("Never invent urgency", self.text)

    def test_one_rescue_after_a_no(self):
        self.assertIn("one gentle attempt", self.text)

    def test_objections_section(self):
        section = self.section("## OBJECTIONS")
        for phrase in ("fee", "think about it", "parent", "other colleges", "busy"):
            with self.subTest(phrase):
                self.assertIn(phrase, section.lower())

    def test_recommends_instead_of_listing(self):
        self.assertIn("Recommend one or two programs", self.text)

    def test_size_stays_close_to_v1(self):
        # Re-billed every turn: restructure, do not just add.
        with open(PROMPT, encoding="utf-8") as f:
            v1 = f.read()
        self.assertLessEqual(len(self.text), len(v1) * 1.2)


class TestAppLoadsV2(unittest.TestCase):
    def test_v2_is_the_default_prompt(self):
        from tests.harness.appctl import app
        self.assertEqual(app.PROMPT_FILE, os.getenv("PROMPT_FILE", "PROMPT_FILES/prompt_au_v2.txt"))
        if "PROMPT_FILE" not in os.environ:
            with open(PROMPT_V2, encoding="utf-8") as f:
                self.assertEqual(app.RAW_SYSTEM_PROMPT, f.read())


if __name__ == "__main__":
    unittest.main()
