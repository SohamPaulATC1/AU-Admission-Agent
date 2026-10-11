"""DEBUG_RECORDING switch (2026-10-11, operator): 1 opens the five debug WAVs per call, 0 opens none.

The write sites already skip a missing writer (covered in test_preservation_4_8), so the
switch only has to gate where the writers are opened.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest

from tests.harness.appctl import REPO_ROOT, app


def _import_app(value):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("DEBUG_RECORDING", None)
    if value is not None:
        env["DEBUG_RECORDING"] = value
    code = "from tests.harness.appctl import app; print(app.DEBUG_RECORDING)"
    return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=300)


class TestDebugRecordingSwitch(unittest.TestCase):
    def test_default_is_on(self):
        result = _import_app(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-1], "True")

    def test_zero_turns_it_off(self):
        result = _import_app(" 0 ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-1], "False")

    def test_unknown_value_fails_at_startup(self):
        result = _import_app("yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEBUG_RECORDING must be '0' or '1', got 'yes'", result.stderr)

    def test_every_debug_writer_is_opened_under_the_switch(self):
        source = inspect.getsource(app.handle_media_stream)
        gate = source.index("        if DEBUG_RECORDING:\n")
        opens = [i for i in range(len(source)) if source.startswith("wave.open(", i)]
        self.assertEqual(len(opens), 3, "debug, far-end and the aligned loop")
        for position in opens:
            self.assertGreater(position, gate)
            line_start = source.rindex("\n", 0, position) + 1
            indent = len(source[line_start:position]) - len(source[line_start:position].lstrip())
            self.assertGreater(indent, 8, "wave.open must sit inside the if block")


if __name__ == "__main__":
    unittest.main()
