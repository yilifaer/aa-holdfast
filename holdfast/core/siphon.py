"""Detecting a mercenary den that is stealing a skyhook's workforce.

ESI will not show you someone else's den. What it will show you is the
skyhook's ``effective_workforce`` -- and that number is *after* the den has
taken its cut, which turns out to be enough.

The trick is that an untouched skyhook always reports a workforce that is a
round multiple of ten. A den takes a flat percentage, so the figure it leaves
behind is only still a multiple of ten when the original happened to be a
multiple of a hundred. Anything else lands on an un-round number:

    8450 base, 10% siphoned  ->  7605   (not a multiple of ten: caught)
    8400 base, 10% siphoned  ->  7560   (still a multiple of ten: missed)

On a real 199-skyhook sample, 25 of the base values were multiples of a
hundred, so this misses about 13% of 10% siphons and about 25% of 20% ones.
That is a floor, not a ceiling: it needs no history at all, which is what makes
it worth having. The high-water check in ``den_sync`` covers the other
direction -- a skyhook we have watched go from clean to siphoned -- and between
them the blind spots barely overlap.

Verified against an in-game export of 413 skyhooks: every one of the 199
un-siphoned readings was a multiple of ten, and all three that the client
showed at -10.0% were not.
"""

import logging

# Rates worth testing, widest first. Only 10% has been seen in the wild; the
# rest are here so a higher anarchy level does not go unnoticed.
SIPHON_RATES = (10, 20, 30, 40, 50)

logger = logging.getLogger(__name__)


def detect_siphon(effective_workforce):
    """Return ``(percent, base)`` for a siphoned reading, else ``(None, None)``.

    Integer arithmetic throughout: a rate only counts as an explanation if it
    divides exactly *and* implies a base that is itself a multiple of ten.
    """
    if effective_workforce is None or effective_workforce <= 0:
        return None, None
    if effective_workforce % 10 == 0:
        return None, None

    numerator = effective_workforce * 100
    for rate in SIPHON_RATES:
        denominator = 100 - rate
        if numerator % denominator:
            continue
        base = numerator // denominator
        if base % 10 == 0:
            return float(rate), base

    # Un-round but no rate explains it. Flag it as siphoned by an unknown
    # amount rather than silently calling it clean -- the un-roundness is the
    # signal, the rate is only the interpretation.
    logger.info(
        "Workforce %s is not a multiple of ten but no known rate explains it",
        effective_workforce,
    )
    return None, None


def apply_to(skyhook) -> bool:
    """Update a skyhook's siphon fields in place. True if anything changed."""
    percent, base = detect_siphon(skyhook.effective_workforce)
    if (skyhook.workforce_siphon_percent, skyhook.workforce_base) == (percent, base):
        return False
    skyhook.workforce_siphon_percent = percent
    skyhook.workforce_base = base
    return True
