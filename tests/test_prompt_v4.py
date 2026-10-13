"""Prompt v4: v3 with the caller kept anonymous (operator, 2026-10-12).

Neha never gets a name: the prompt has no {user_name}, the greeting event names nobody, and she
asks for names only for the admission team's record, never to address the caller (option A).
Every other v3 rule is unchanged; v3 stays as the rollback.

The v3 module is imported as a module alias, never with ``from tests.test_prompt_v3 import Test...``:
binding its TestCase classes here would make unittest discovery run them a second time.
"""

import os
import random
import unittest

import tests.test_prompt_v3 as v3

PROMPT_V4 = os.path.join(v3.base.PROMPT_DIR, "prompt_au_v4.txt")
EDITED = ("- You are calling", "3. **Student or parent")   # v3 lines reworded in v4
ADDED = ("- Address (CRITICAL):",)                          # new in v4


def _read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


class TestPromptCatalogV4(v3.TestPromptCatalogV3):
    """Every v1/v2/v3 catalog check on v4."""
    path = PROMPT_V4

    def test_formats_like_app_py_does(self):
        # app.py still passes user_name to format(); v4 has nowhere to put it.
        filled = self.text.format(user_name="Asha", phone_number="9800000000")
        self.assertNotIn("Asha", filled)
        self.assertIn("9800000000", filled)


class TestPromptV4Counselor(v3.TestPromptV3Counselor):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(PROMPT_V4)


class TestPromptV4Template(v3.TestPromptV3Template):
    """Every v3 template check on v4; the four that assumed a {user_name} are replaced."""

    @classmethod
    def setUpClass(cls):
        cls.text = _read(PROMPT_V4)
        cls.lines = cls.text.splitlines()

    def test_role_persona(self):
        section = self.section("## ROLE & OBJECTIVE")
        for phrase in ("**Neha**", "female", "Adamas University, Kolkata", "2027 session", "{phone_number}",
                       "You do not know their name",
                       "first NAAC A-accredited private university from West Bengal"):
            with self.subTest(phrase):
                self.assertIn(phrase, section)

    def test_student_parent_step(self):
        step3 = self.line_starting("3. **Student or parent")
        for phrase in ("Am I speaking with the student, or with a parent or guardian?",
                       "May I know your name, and the student's name?", "for our records",
                       '"Thank you, noted." without repeating any name', "WARD"):
            with self.subTest(phrase):
                self.assertIn(phrase, step3)

    def test_brace_safety(self):
        placeholders = v3.re.findall(r"\{[^{}]*\}", self.text)
        self.assertEqual(set(placeholders), {"{phone_number}"})
        self.assertEqual(self.text.count("{") + self.text.count("}"), 2 * len(placeholders))

    def test_format_any_values(self):
        rng = random.Random(20261012)
        for _ in range(100):
            u, p = v3._random_values(rng)
            self.assertEqual(self.text.format(user_name=u, phone_number=p), self.text.replace("{phone_number}", p))


class TestPromptV4Anonymous(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read(PROMPT_V4)
        cls.v3_text = _read(v3.PROMPT_V3)

    def test_no_name_placeholder(self):
        self.assertNotIn("{user_name}", self.text)
        self.assertNotIn("name on record", self.text)

    def test_only_the_name_lines_changed(self):
        def kept(text, skip):
            return [line for line in text.splitlines() if not line.startswith(skip)]
        self.assertEqual(kept(self.v3_text, EDITED), kept(self.text, EDITED + ADDED))

    def test_neutral_address_rule(self):
        language = self.text.split("## LANGUAGE & TONE :", 1)[1].split("\n## ", 1)[0]
        [line] = [l for l in language.splitlines() if l.startswith("- Address (CRITICAL):")]
        for phrase in ('a simple "Hello"', "never a name", "never say a name, even one they tell you",
                       '"Sir" or "Madam"'):
            with self.subTest(phrase):
                self.assertIn(phrase, line)


class TestAppUsesV4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.harness.appctl import app
        cls.app = app

    def test_app_loads_v4(self):
        self.assertEqual(self.app.PROMPT_FILE, "PROMPT_FILES/prompt_au_v4.txt")
        self.assertEqual(self.app.RAW_SYSTEM_PROMPT, _read(PROMPT_V4))

    def test_system_prompt_never_carries_the_name(self):
        filled = self.app.RAW_SYSTEM_PROMPT.format(user_name="Rahul Sen", phone_number="9800000000")
        self.assertNotIn("Rahul", filled)

    def test_greeting_event_names_nobody(self):
        event = self.app.GREETING_EVENT
        self.assertNotIn("{", event)   # not a template: nothing about the caller goes in
        for phrase in ("The outbound call has just connected.", 'a simple "Hello"', "never a name", "STOP HERE"):
            with self.subTest(phrase):
                self.assertIn(phrase, event)

    def test_standard_greeting_uses_the_constant(self):
        with open(v3.base.os.path.join(os.path.dirname(v3.base.PROMPT_DIR), "app.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn("initial_prompt = GREETING_EVENT", source)
        self.assertNotIn("Greet {user_name}", source)
        self.assertNotIn("The outbound call to {user_name}", source)


if __name__ == "__main__":
    unittest.main()
