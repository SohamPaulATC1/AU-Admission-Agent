"""Bengali grapheme-cluster fixture table.

The table is ``base consonant x matra x {visarga, anusvara, candrabindu, none} x
conjunct``, per tasks.md task 2, plus the four expectations the task names.

Those four expectations are **verified against the installed interpreter**, not
copied from the spec -- see ``tests/test_harness_smoke.py``, which runs
``regex.findall(r'\\X', ...)`` on ``regex`` 2026.2.28 and compares. All four
matched on this interpreter.
"""

from __future__ import annotations

import regex

# --- code points -------------------------------------------------------------
VIRAMA = "\u09CD"        # ্  hasant / virama
VISARGA = "\u0983"       # ঃ
ANUSVARA = "\u0982"      # ং
CANDRABINDU = "\u0981"   # ঁ

CONSONANTS = {
    "ka": "\u0995",   # ক
    "kha": "\u0996",  # খ
    "ta": "\u09A4",   # ত
    "da": "\u09A6",   # দ
    "na": "\u09A8",   # ন
    "pa": "\u09AA",   # প
    "ba": "\u09AC",   # ব
    "ma": "\u09AE",   # ম
    "sha": "\u09B6",  # শ
    "sa": "\u09B8",   # স
}

MATRAS = {
    "none": "",
    "aa": "\u09BE",   # া
    "i": "\u09BF",    # ি  (pre-base in rendering, post-base in encoding)
    "ii": "\u09C0",   # ী
    "u": "\u09C1",    # ু
    "uu": "\u09C2",   # ূ
    "e": "\u09C7",    # ে
    "o": "\u09CB",    # ো
}

NASAL_SIGNS = {
    "none": "",
    "visarga": VISARGA,
    "anusvara": ANUSVARA,
    "candrabindu": CANDRABINDU,
}

CONJUNCT_PAIRS = {
    "nta": ("\u09A8", "\u09A4"),   # ন + ্ + ত  -> ন্ত
    "bda": ("\u09AC", "\u09A6"),   # ব + ্ + দ  -> ব্দ
    "tta": ("\u09A4", "\u09A4"),   # ত + ্ + ত  -> ত্ত
    "kta": ("\u0995", "\u09A4"),   # ক + ্ + ত  -> ক্ত
}


class ClusterCase:
    """One generated single-cluster fixture."""

    __slots__ = ("text", "label", "base", "matra", "sign", "conjunct")

    def __init__(self, text, label, base, matra, sign, conjunct):
        self.text = text
        self.label = label
        self.base = base
        self.matra = matra
        self.sign = sign
        self.conjunct = conjunct

    def __repr__(self):
        return f"ClusterCase({self.label!r}, text={self.text!r})"


def cluster_table() -> list[ClusterCase]:
    """base x matra x sign, plus conjunct x matra x sign.

    Every entry is expected to be exactly ONE extended grapheme cluster.
    """
    cases: list[ClusterCase] = []

    for base_name, base in CONSONANTS.items():
        for matra_name, matra in MATRAS.items():
            for sign_name, sign in NASAL_SIGNS.items():
                cases.append(ClusterCase(
                    text=base + matra + sign,
                    label=f"{base_name}+{matra_name}+{sign_name}",
                    base=base, matra=matra, sign=sign, conjunct=None,
                ))

    for conjunct_name, (first, second) in CONJUNCT_PAIRS.items():
        for matra_name, matra in MATRAS.items():
            for sign_name, sign in NASAL_SIGNS.items():
                cases.append(ClusterCase(
                    text=first + VIRAMA + second + matra + sign,
                    label=f"{conjunct_name}(conjunct)+{matra_name}+{sign_name}",
                    base=first, matra=matra, sign=sign, conjunct=(first, second),
                ))

    return cases


# --- the four expectations named by task 2 -----------------------------------
# Expectations are asserted, never adjusted, against the installed ``regex``.
VERIFIED_SEGMENTATIONS = {
    "\u09A6\u09C1\u0983\u0996\u09BF\u09A4": ["\u09A6\u09C1\u0983", "\u0996\u09BF", "\u09A4"],
    # দুঃখিত -> ['দুঃ', 'খি', 'ত']
    "\u09A8\u09BF\u0983\u09B6\u09AC\u09CD\u09A6": ["\u09A8\u09BF\u0983", "\u09B6", "\u09AC\u09CD\u09A6"],
    # নিঃশব্দ -> ['নিঃ', 'শ', 'ব্দ']
    "\u0985\u09A8\u09CD\u09A4\u0983\u09B8\u09A4\u09CD\u09A4\u09CD\u09AC\u09BE":
        ["\u0985", "\u09A8\u09CD\u09A4\u0983", "\u09B8", "\u09A4\u09CD\u09A4\u09CD\u09AC\u09BE"],
    # অন্তঃসত্ত্বা -> ['অ', 'ন্তঃ', 'স', 'ত্ত্বা']
    "\u09AA\u09C1\u09A8\u0983": ["\u09AA\u09C1", "\u09A8\u0983"],
    # পুনঃ -> ['পু', 'নঃ']
}

# Real transcripts lifted from Gemini_Assistant.log, used by the exploration test.
APOLOGY_PREFIX = "\u0986\u09AE\u09BF \u09A6\u09C1\u0983\u0996\u09BF\u09A4"      # আমি দুঃখিত
APOLOGY_TURN_1 = APOLOGY_PREFIX + ", \u0986\u09AE\u09BF\u2026"                  # আমি দুঃখিত, আমি…
APOLOGY_TURN_2 = APOLOGY_PREFIX + ", \u0995\u09BF\u09A8\u09CD\u09A4\u09C1 \u0986\u09AE\u09BF"
LEADING_CLUSTER = "\u09A6\u09C1\u0983"                                          # দুঃ

NON_BENGALI_SAMPLES = {
    "english": "I am sorry, I cannot help with that.",
    "hindi": "\u092E\u0941\u091D\u0947 \u0916\u0947\u0926 \u0939\u0948",         # मुझे खेद है
    "bengali_no_visarga_cluster": "\u0986\u09AE\u09BF \u09AC\u09B2\u099B\u09BF",  # আমি বলছি
    "mixed": "Admission \u09AB\u09B0\u09AE fill up \u0995\u09B0\u09C1\u09A8",
    "empty": "",
    "lone_combining_mark": VISARGA,
}


def clusters(text: str) -> list[str]:
    """UAX #29 extended grapheme clusters via ``regex``'s ``\\X``.

    This is the *test-side* segmenter, kept independent of production so tests
    can check ``app.grapheme_clusters`` against it.
    """
    return regex.findall(r"\X", text)
