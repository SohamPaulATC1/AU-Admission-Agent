"""Offline test suite for the bengali-grapheme-stutter-fix spec.

Run with:

    venv312/Scripts/python.exe -m unittest discover -s tests -t .

(Python 3.12 venv on Windows; 3.13+ has no ``audioop``, which app.py imports.)

Standing constraints honoured by this package (tasks.md "Standing constraints"):

* stdlib ``unittest`` only. ``pytest`` and ``hypothesis`` are not installed and
  are not added. Property-based testing is the hand-rolled seeded falsifier in
  ``tests.harness.falsifier``.
* ``aec.py`` is byte-identical; its content hash is pinned in
  ``tests/test_preservation_4_7_aec_integrity.py``.
* ``requirements.txt`` and ``.env`` are not modified.
* The harness needs no refactor of ``app.py`` to drive it, see
  ``tests/harness/README.md``. (``app.py`` itself has since been changed by the
  fix and the audio-pipeline redesign; tests that pin its source say so.)
"""
