"""Tests for the workforce siphon fingerprint.

This is the subtlest logic in the app and the one whose failure would be
silent: get it wrong and the app simply never reports a hostile den, which
looks exactly like having none. The numbers below are not invented -- they come
from a 413-skyhook export taken from the game client, cross-checked against
what ESI reported for the same skyhooks on the same day.
"""

from django.test import SimpleTestCase

from ..core.siphon import detect_siphon


class DetectSiphonTests(SimpleTestCase):
    """No database needed: detect_siphon is a pure function."""

    def test_round_figures_are_untouched(self):
        """An undisturbed skyhook always reports a multiple of ten."""
        for value in (10170, 9940, 8450, 7630, 1020, 5200, 10):
            with self.subTest(value=value):
                self.assertEqual(detect_siphon(value), (None, None))

    def test_recognises_a_ten_percent_cut(self):
        """The three real cases the game client showed at -10.0%."""
        cases = {
            7605: 8450,   # I9-ZQZ IV
            7812: 8680,   # SWBV-2 IV
            8235: 9150,   # 2-RSC7 III
        }
        for observed, expected_base in cases.items():
            with self.subTest(observed=observed):
                percent, base = detect_siphon(observed)
                self.assertEqual(percent, 10.0)
                self.assertEqual(base, expected_base)
                self.assertEqual(base - observed, round(base * 0.1))

    def test_implied_base_is_always_a_round_figure(self):
        """A rate only counts as an explanation if the base it implies is one
        a skyhook could actually have produced."""
        for observed in (7605, 7812, 8235, 7308):
            with self.subTest(observed=observed):
                _percent, base = detect_siphon(observed)
                self.assertIsNotNone(base)
                self.assertEqual(base % 10, 0)

    def test_higher_rates_are_recognised(self):
        """Only 10% has been seen in the wild, but a higher anarchy level must
        not go unnoticed just because nobody has met one yet."""
        # 8450 base, 20% taken -> 6760, which is a multiple of ten, so this
        # particular pair is invisible. Pick one that is not.
        percent, base = detect_siphon(6764)  # 8455 is not a valid base
        self.assertIsNone(percent, "should not invent an explanation")

        percent, base = detect_siphon(int(8130 * 0.8))  # 6504
        self.assertEqual(percent, 20.0)
        self.assertEqual(base, 8130)

    def test_known_blind_spot_is_a_blind_spot(self):
        """A base that is a multiple of a hundred survives a 10% cut as a
        multiple of ten, so the fingerprint cannot see it.

        Documented rather than fixed: it is why the high-water check in
        den_sync exists alongside this one, and why the UI says a clean list is
        not proof of none.
        """
        self.assertEqual(detect_siphon(int(8400 * 0.9)), (None, None))  # 7560

    def test_rubbish_input_is_refused_quietly(self):
        for value in (None, 0, -5):
            with self.subTest(value=value):
                self.assertEqual(detect_siphon(value), (None, None))

    def test_unexplained_odd_number_is_not_guessed_at(self):
        """An un-round figure no known rate explains stays unreported.

        Reporting it as "siphoned by an unknown amount" would put a number on
        the dashboard that nobody could act on.
        """
        percent, base = detect_siphon(7607)
        self.assertIsNone(percent)
        self.assertIsNone(base)
