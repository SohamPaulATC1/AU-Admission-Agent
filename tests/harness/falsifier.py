"""Hand-rolled seeded falsifier -- the stand-in for ``hypothesis``.

``hypothesis`` is not installed and ``requirements.txt`` must not be modified
(tasks.md standing constraints), so property-based testing here is a seeded loop
over ``numpy.random.default_rng(seed)`` that prints the seed, the case index and
the failing input on first failure.

What this gives up relative to ``hypothesis``, stated so nobody over-reads the
coverage: no shrinking, no automatic edge-case biasing, and no example database.
Boundary cases therefore have to be enumerated explicitly -- see the
``boundary_cases`` argument, which is always run first.
"""

from __future__ import annotations

import numpy as np


class PropertyFailure(AssertionError):
    """Raised with the seed, case index and the failing input attached."""

    def __init__(self, name, seed, index, case, detail):
        self.property_name = name
        self.seed = seed
        self.index = index
        self.case = case
        self.detail = detail
        super().__init__(
            f"property {name!r} falsified\n"
            f"  seed        = {seed}\n"
            f"  case index  = {index}\n"
            f"  failing input = {case!r}\n"
            f"  detail      = {detail}"
        )


def run_property(
    name: str,
    generate,
    check,
    *,
    cases: int = 500,
    seed: int = 20260301,
    boundary_cases=(),
):
    """Run ``check(case)`` over ``cases`` generated inputs plus every boundary case.

    ``generate(rng, index) -> case``
    ``check(case) -> None`` (raises on failure) or ``str``/``False`` for failure.

    Boundary cases run first and at index ``-1, -2, ...`` so a boundary failure is
    immediately distinguishable from a random one in the report.

    Returns the number of cases executed.
    """
    executed = 0

    for offset, case in enumerate(boundary_cases):
        _run_one(name, seed, -(offset + 1), case, check)
        executed += 1

    rng = np.random.default_rng(seed)
    for index in range(cases):
        case = generate(rng, index)
        _run_one(name, seed, index, case, check)
        executed += 1

    return executed


def _run_one(name, seed, index, case, check):
    try:
        outcome = check(case)
    except AssertionError as failure:
        raise PropertyFailure(name, seed, index, case, str(failure)) from failure
    if outcome is False:
        raise PropertyFailure(name, seed, index, case, "check returned False")
    if isinstance(outcome, str) and outcome:
        raise PropertyFailure(name, seed, index, case, outcome)


def uniform_int(rng: np.random.Generator, low: int, high: int) -> int:
    """Inclusive integer draw."""
    return int(rng.integers(low, high + 1))


def uniform_float(rng: np.random.Generator, low: float, high: float) -> float:
    return float(rng.uniform(low, high))


def choice(rng: np.random.Generator, options):
    options = list(options)
    return options[int(rng.integers(0, len(options)))]
