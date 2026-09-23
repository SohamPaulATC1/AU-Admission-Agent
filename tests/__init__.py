"""Offline test suite for the bengali-grapheme-stutter-fix spec.

Run with:

    venv/bin/python -m unittest discover -s tests -v

Standing constraints honoured by this package (tasks.md "Standing constraints"):

* stdlib ``unittest`` only. ``pytest`` and ``hypothesis`` are not installed and
  are not added. Property-based testing is the hand-rolled seeded falsifier in
  ``tests.harness.falsifier``.
* ``aec.py`` is byte-identical; its content hash is pinned in
  ``tests/test_preservation_4_7_aec_integrity.py``.
* ``requirements.txt`` and ``.env`` are not modified.
* ``app.py`` is not modified by this pass at all -- no refactor proved
  necessary, see ``tests/harness/README.md``.
"""
