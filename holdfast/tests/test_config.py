"""Tests for the fuel bands and the alert routing fallbacks.

Both encode a rule that is easy to get subtly wrong and whose failure mode is
silence: a band that never matches, or a category that quietly reaches no
channel.
"""

from django.test import TestCase

from ..core.alerts import _webhooks_for
from ..models import AlertCategory, AlertRoute, HoldfastConfig, Webhook


class FuelSeverityTests(TestCase):
    def setUp(self):
        self.config = HoldfastConfig.get_solo()
        self.config.fuel_warning_days = 7
        self.config.fuel_danger_days = 3
        self.config.fuel_critical_days = 1
        self.config.save()

    def test_plenty_of_fuel_is_not_a_severity(self):
        self.assertIsNone(self.config.fuel_severity(30 * 24))

    def test_each_band_matches_its_own_range(self):
        cases = {
            7.5 * 24: None,
            6.9 * 24: "warning",
            3.5 * 24: "warning",
            2.9 * 24: "danger",
            1.5 * 24: "danger",
            0.9 * 24: "critical",
            0: "critical",
        }
        for hours, expected in cases.items():
            with self.subTest(hours=hours):
                self.assertEqual(self.config.fuel_severity(hours), expected)

    def test_boundaries_belong_to_the_tighter_band(self):
        """Exactly three days is red, not amber. A hub sitting on a boundary
        should read as the more urgent of the two, never the calmer one."""
        self.assertEqual(self.config.fuel_severity(3 * 24), "danger")
        self.assertEqual(self.config.fuel_severity(1 * 24), "critical")
        self.assertEqual(self.config.fuel_severity(7 * 24), "warning")

    def test_no_expiry_is_not_a_severity(self):
        """A hub burning nothing never runs dry, so it is not an alert."""
        self.assertIsNone(self.config.fuel_severity(None))

    def test_singleton(self):
        first = HoldfastConfig.get_solo()
        second = HoldfastConfig.get_solo()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(HoldfastConfig.objects.count(), 1)


class AlertRoutingTests(TestCase):
    def setUp(self):
        self.config = HoldfastConfig.get_solo()
        self.sov = Webhook.objects.create(name="sov", url="https://example.invalid/1")
        self.den = Webhook.objects.create(name="den", url="https://example.invalid/2")

    def test_unconfigured_category_reaches_every_enabled_webhook(self):
        """The fallback matters more than it looks: an upgrade that added a new
        category must not leave it silently unrouted."""
        hooks = _webhooks_for(AlertCategory.SOV_FUEL)
        self.assertCountEqual(hooks, [self.sov, self.den])

    def test_explicit_routing_wins(self):
        route = AlertRoute.for_category(AlertCategory.SOV_FUEL)
        route.webhooks.set([self.sov])
        self.assertEqual(_webhooks_for(AlertCategory.SOV_FUEL), [self.sov])

    def test_disabled_category_reaches_nothing(self):
        route = AlertRoute.for_category(AlertCategory.SOV_FUEL)
        route.is_enabled = False
        route.save()
        self.assertEqual(_webhooks_for(AlertCategory.SOV_FUEL), [])

    def test_section_switch_silences_its_categories(self):
        self.config.sov_discord_enabled = False
        self.config.save()
        self.assertEqual(_webhooks_for(AlertCategory.SOV_FUEL), [])
        # ... and leaves the other sections alone.
        self.assertTrue(_webhooks_for(AlertCategory.DEN_ATTACK))

    def test_disabled_webhook_is_skipped_but_does_not_silence_the_category(self):
        """Routing a category only to a webhook that is switched off falls back
        to the others rather than going quiet."""
        route = AlertRoute.for_category(AlertCategory.SOV_FUEL)
        route.webhooks.set([self.sov])
        self.sov.is_enabled = False
        self.sov.save()
        self.assertEqual(_webhooks_for(AlertCategory.SOV_FUEL), [self.den])
