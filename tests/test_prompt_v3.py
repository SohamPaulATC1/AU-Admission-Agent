"""Prompt v3 (Gemini Live best-practices layout) keeps every v2 rule and adds template checks.

Spec: .kiro/specs/prompt-v3-gemini-template/. v3 is opt-in through the
PROMPT_FILE env var; app.py's default stays prompt_au_v2.txt.

The base module is imported as a module alias, never with
``from tests.test_prompt_catalog import Test...``: binding the base TestCase
classes here would make unittest discovery run them a second time.
"""

import os
import random
import re
import unittest

import tests.test_prompt_catalog as base

PROMPT_V3 = os.path.join(base.PROMPT_DIR, "prompt_au_v3.txt")
SIZE_CAP = 14228  # int(len(prompt_au.txt) * 1.2), the v2 size bound

REQUIRED_HEADINGS = [
    "## ROLE & OBJECTIVE",
    "## LANGUAGE & TONE :",
    "## YOUR GOAL IS TO :",
    "**CONVERSATIONAL RULES / FLOW:**",
    "## GUARDRAILS & SAFETY (CRITICAL) :",
]
# Prefixes: the heading line may carry a suffix such as "(CRITICAL)".
CONTEXT_HEADINGS = [
    "## CATALOG MAP",
    "## CATALOG TOOLS",
    "## SCHOLARSHIPS",
    "## CAMPUS VISIT",
    "## OBJECTIONS",
    "## NOT AVAILABLE",
]
TOOLS = ("list_programs", "get_program_details", "find_eligible_programs",
         "find_scholarships", "transferCall", "endCall")
KEEP_IN_ENGLISH = ("student", "parent", "guardian", "course", "program", "admission",
                   "qualification", "board", "eligibility", "fees", "semester",
                   "campus", "scholarship", "visit")
# Option B (operator, 2026-10-09): UNMISTAKABLY on the six language rules plus
# farewell-before-endCall, handover-before-transferCall and the tools-only rule.
LANGUAGE_STRICT_PREFIXES = ("- Accent:", "- **English caller:**", "- **Bengali caller:**",
                            "- **Hindi caller:**", "- Reply language:", "- YOU MUST SPEAK ALL NUMBERS")
OTHER_STRICT_PREFIXES = ("8. **Closing", "- **Handover", "- If no tool returned")


def _read_v3():
    with open(PROMPT_V3, "rb") as f:
        return f.read().decode("utf-8")  # strict: raises on non-UTF-8


def _random_values(rng):
    """Caller values app.py may pass: empty, ASCII, Bengali, Devanagari, digits, braces."""
    pool = ["", "Asha", "Rahul Sen", "Mr. Banerjee", "অরিন্দম", "प्रिया शर्मा",
            "9800000000", "+91 98000 00000", "{user_name}", "a{b}c", "}{", " "]
    def one():
        if rng.random() < 0.5:
            return rng.choice(pool)
        return "".join(rng.choice("abcXYZ019 অআहि{}-") for _ in range(rng.randint(0, 12)))
    return one(), one()


class TestPromptCatalogV3(base.TestPromptCatalogV2):
    """Every v1/v2 catalog check, with the v2 ask_about override, on v3."""
    path = PROMPT_V3


class TestPromptV3Counselor(base.TestPromptV2Counselor):
    """Every v2 counselor check on v3. The base setUpClass hardcodes PROMPT_V2."""

    @classmethod
    def setUpClass(cls):
        cls.text = _read_v3()


class TestPromptV3Template(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read_v3()
        cls.lines = cls.text.splitlines()

    def section(self, heading):
        return self.text.split(heading, 1)[1].split("\n## ", 1)[0]

    def line_starting(self, prefix):
        matches = [line for line in self.lines if line.startswith(prefix)]
        self.assertEqual(len(matches), 1, prefix)
        return matches[0]

    def heading_index(self, prefix):
        matches = [i for i, line in enumerate(self.lines) if line.startswith(prefix)]
        self.assertEqual(len(matches), 1, prefix)
        return matches[0]

    # --- Structure -------------------------------------------------------

    def test_first_line_is_role(self):
        first = next(line for line in self.lines if line.strip())
        self.assertEqual(first, "## ROLE & OBJECTIVE")

    def test_required_headings_exact_once(self):
        for heading in REQUIRED_HEADINGS:
            with self.subTest(heading):
                self.assertEqual(self.lines.count(heading), 1)

    def test_heading_order(self):
        # Feature: prompt-v3-gemini-template, Property 7: Headings are unique and in template order
        order = REQUIRED_HEADINGS[:4] + CONTEXT_HEADINGS + REQUIRED_HEADINGS[4:]
        indices = [self.heading_index(h) for h in order]
        self.assertEqual(indices, sorted(indices))

    def test_flow_directly_follows_goal(self):
        # The bold FLOW heading does not end a section(), so only GOAL (which
        # has no section checks) may absorb the flow text.
        goal = self.heading_index("## YOUR GOAL IS TO :")
        flow = self.heading_index("**CONVERSATIONAL RULES / FLOW:**")
        self.assertFalse([l for l in self.lines[goal + 1:flow] if l.startswith("## ")])

    # --- Language --------------------------------------------------------

    def test_language_lines(self):
        for lang in ("INDIAN ENGLISH", "BENGLISH", "HINGLISH"):
            with self.subTest(lang):
                pattern = f"RESPOND IN {lang}. YOU MUST RESPOND UNMISTAKABLY IN {lang}"
                matches = [l for l in self.lines if pattern in l and "ACCENT" in l]
                self.assertEqual(len(matches), 1)

    def test_unmistakably_on_60_40_rules(self):
        for lang in ("Bengali", "Hindi"):
            with self.subTest(lang):
                line = next(l for l in self.lines if f"{lang} (about 60%)" in l)
                self.assertIn("UNMISTAKABLY", line)
                self.assertIn("(about 40%)", line)

    def test_keep_in_english_list(self):
        line = next(l for l in self.lines if "Language mix (CRITICAL)" in l)
        for word in KEEP_IN_ENGLISH:
            with self.subTest(word):
                self.assertIn(word, line)

    def test_reply_lock_and_numbers(self):
        section = self.section("## LANGUAGE & TONE :")
        self.assertIn("UNMISTAKABLY IN THE CALLER'S LANGUAGE", section)
        self.assertIn("PERCENTAGES AND FEES UNMISTAKABLY IN ENGLISH", section)
        self.assertIn("digit by digit", section)

    def test_single_accent_rule(self):
        self.assertIn("never drift", self.line_starting("- Accent:"))

    def test_no_silent_reminder(self):
        self.assertNotIn("silently remind yourself", self.text)

    def test_brand_name(self):
        self.assertEqual(self.text.count("অ্যাডামাস"), 1)
        line = next(l for l in self.lines if "অ্যাডামাস" in l)
        self.assertIn('Pronounce "Adamas"', line)

    def test_unmistakably_only_on_strict_rules(self):
        # Feature: prompt-v3-gemini-template, Property 2: UNMISTAKABLY appears only on Strict_Rule lines
        language = self.section("## LANGUAGE & TONE :")
        lines = [l for l in self.lines if "UNMISTAKABLY" in l]
        for line in lines:
            with self.subTest(line[:40]):
                if line.startswith(OTHER_STRICT_PREFIXES):
                    continue
                self.assertIn(line, language)
                self.assertTrue(line.startswith(LANGUAGE_STRICT_PREFIXES))
        self.assertEqual(self.text.count("UNMISTAKABLY"), 9)

    # --- Role, flow, goal ------------------------------------------------

    def test_role_persona(self):
        section = self.section("## ROLE & OBJECTIVE")
        for phrase in ("**Neha**", "female", "Adamas University, Kolkata", "2027 session",
                       "{user_name}", "{phone_number}",
                       "first NAAC A-accredited private university from West Bengal"):
            with self.subTest(phrase):
                self.assertIn(phrase, section)

    def test_student_parent_step(self):
        step3 = self.line_starting("3. **Student or parent")
        self.assertNotIn("{user_name}", step3)
        for phrase in ("Never assume", "no name", "WARD"):
            with self.subTest(phrase):
                self.assertIn(phrase, step3)

    def test_handover_is_its_own_rule(self):
        lines = [l for l in self.lines if "transferCall" in l]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertTrue(line.startswith("- **Handover"))
        self.assertLess(line.index("FIRST"), line.index("`transferCall`"))
        self.assertIn("call_summary", line)
        self.assertIn("language", line)

    def test_farewell_before_end_call(self):
        step8 = self.line_starting("8. **Closing")
        self.assertLess(step8.index("FIRST"), step8.index("`endCall`"))

    def test_summary_fields(self):
        step8 = self.line_starting("8. **Closing")
        for field in ("student or parent/guardian", "applicant name", "qualification",
                      "board/university", "marks", "program(s) of interest", "scholarship shared",
                      "campus visit day", "objections", "callback", "interest level", "outcome"):
            with self.subTest(field):
                self.assertIn(field, step8)

    def test_visit_day_with_date(self):
        section = self.section("## CAMPUS VISIT")
        self.assertIn("with its date", section)
        self.assertIn("Current Date and Time (India IST)", section)

    def test_one_question_exceptions(self):
        line = next(l for l in self.lines if "No question needed" in l)
        for word in ("farewell", "busy", "handover"):
            with self.subTest(word):
                self.assertIn(word, line)

    def test_scholarship_turn_may_run_longer(self):
        self.assertIn("the scholarship turn may run longer", self.text)

    def test_flow_steps_labeled(self):
        # Feature: prompt-v3-gemini-template, Property 4: Every flow step is labeled one-time or loop
        steps = [l for l in self.lines if re.match(r"^\d\. \*\*", l) or l.startswith("- **Handover")]
        self.assertEqual(len(steps), 9)
        for line in steps:
            with self.subTest(line[:30]):
                self.assertTrue("(one-time" in line or "(loop" in line)

    def test_one_tool_per_sentence(self):
        # Feature: prompt-v3-gemini-template, Property 3: Each sentence names at most one tool
        for sentence in re.split(r"(?<=[.?!])\s+", self.text):
            named = {t for t in TOOLS if f"`{t}`" in sentence}
            with self.subTest(sentence[:50]):
                self.assertLessEqual(len(named), 1)

    # --- Guardrails ------------------------------------------------------

    def test_guardrails_if_x_do_y(self):
        # Feature: prompt-v3-gemini-template, Property 5: Every guardrail is an if-X-do-Y rule with an example
        section = self.section("## GUARDRAILS & SAFETY (CRITICAL) :")
        bullets = [l for l in section.splitlines() if l.startswith("- ")]
        self.assertGreaterEqual(len(bullets), 6)
        for line in bullets:
            with self.subTest(line[:40]):
                self.assertTrue(line.startswith("- If "))
                self.assertIn('"', line)

    def test_tools_only_rule(self):
        section = self.section("## GUARDRAILS & SAFETY (CRITICAL) :")
        for phrase in ("fee, eligibility, duration, seat count, program, scholarship, date or policy",
                       "Gemini", "Google", "financial advice", "competitors", "placements", "off-topic"):
            with self.subTest(phrase):
                self.assertIn(phrase, section)

    # --- Format safety and size -------------------------------------------

    def test_brace_safety(self):
        placeholders = re.findall(r"\{[^{}]*\}", self.text)
        self.assertEqual(set(placeholders), {"{user_name}", "{phone_number}"})
        self.assertEqual(self.text.count("{") + self.text.count("}"), 2 * len(placeholders))

    def test_format_any_values(self):
        # Feature: prompt-v3-gemini-template, Property 1: Formatting succeeds for any caller values
        rng = random.Random(20261007)
        for _ in range(100):
            u, p = _random_values(rng)
            filled = self.text.format(user_name=u, phone_number=p)
            self.assertIn(u, filled)
            self.assertIn(p, filled)

    def test_step3_independent_of_user_name(self):
        # Feature: prompt-v3-gemini-template, Property 6: The student-or-parent step does not depend on the name
        rng = random.Random(20261007)
        raw = self.line_starting("3. **Student or parent")
        for _ in range(100):
            u, p = _random_values(rng)
            filled = self.text.format(user_name=u, phone_number=p)
            step3 = next(l for l in filled.splitlines() if l.startswith("3. **Student or parent"))
            self.assertEqual(step3, raw)

    def test_size_cap(self):
        self.assertLessEqual(len(self.text), SIZE_CAP)


if __name__ == "__main__":
    unittest.main()
