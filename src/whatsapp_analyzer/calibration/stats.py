"""Small, pure statistics for ordinal labels (LOW < MEDIUM < HIGH). No third-party code.

Nothing here invents a confidence: it only counts agreement between categorical labels.
"""

import math
from collections import Counter
from collections.abc import Sequence

ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
LEVELS = tuple(ORDER)
WILSON_Z = 1.959964  # two-sided 95%


def wilson(correct: int, total: int) -> tuple[float, float] | None:
    """95% Wilson interval of a proportion. Valid only for a simple random sample."""
    if total <= 0:
        return None
    p = correct / total
    z2 = WILSON_Z**2
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    half = WILSON_Z * math.sqrt(p * (1 - p) / total + z2 / (4 * total**2)) / (1 + z2 / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def confusion(pairs: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """`matrix[human][predicted]` over the three ordinal labels."""
    counts = Counter(pairs)
    return {h: {p: counts[(h, p)] for p in LEVELS} for h in LEVELS}


def weighted_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Quadratic-weighted Cohen's kappa over (rater A, rater B) ordinal labels.

    1 is perfect agreement, 0 is chance level. None when it is undefined (no pairs, or both
    raters always use one and the same label, so chance agreement is already total).
    """
    total = len(pairs)
    if total == 0:
        return None
    size = len(LEVELS)
    observed = [[0.0] * size for _ in range(size)]
    for a, b in pairs:
        observed[ORDER[a]][ORDER[b]] += 1 / total
    rows = [sum(observed[i]) for i in range(size)]
    cols = [sum(observed[i][j] for i in range(size)) for j in range(size)]
    numerator = denominator = 0.0
    for i in range(size):
        for j in range(size):
            weight = ((i - j) / (size - 1)) ** 2
            numerator += weight * observed[i][j]
            denominator += weight * rows[i] * cols[j]
    if denominator == 0:
        return None
    return 1 - numerator / denominator


def distance_counts(pairs: Sequence[tuple[str, str]]) -> tuple[int, int]:
    """(pairs one step apart, pairs two steps apart)."""
    steps = Counter(abs(ORDER[a] - ORDER[b]) for a, b in pairs)
    return steps[1], steps[2]


def exact_sign_test(only_first: int, only_second: int) -> float | None:
    """Two-sided exact binomial (McNemar) p-value over the discordant pairs. None if none."""
    discordant = only_first + only_second
    if discordant == 0:
        return None
    tail = sum(math.comb(discordant, k) for k in range(min(only_first, only_second) + 1))
    return min(1.0, 2 * tail / (1 << discordant))


def rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)
