"""prompt_au.txt carries the catalog map and tool rules, and no fee figures (spec section 10)."""

import os
import re
import unittest

import course_catalog

PROMPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompt_au.txt")


class TestPromptCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PROMPT, encoding="utf-8") as f:
            cls.text = f.read()

    def test_formats_like_app_py_does(self):
        # app.py fills these two with str.format; any other brace would raise.
        filled = self.text.format(user_name="Asha", phone_number="9800000000")
        self.assertIn("Asha", filled)
        self.assertIn("9800000000", filled)

    def test_names_the_phase_1_tools_only(self):
        for name in ("list_programs", "get_program_details", "transferCall", "endCall"):
            self.assertIn(name, self.text)
        self.assertNotIn("find_eligible_programs", self.text)

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

    def test_open_eligibility_rule(self):
        self.assertIn('Do NOT say "you are eligible"', self.text)


if __name__ == "__main__":
    unittest.main()
