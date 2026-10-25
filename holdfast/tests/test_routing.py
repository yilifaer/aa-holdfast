"""Routing never widens a category's audience on its own.

"Tick nothing and it goes to every enabled webhook" is what keeps a fresh
install, and a category added by an upgrade, from going quiet. It also used to
cover a category whose ticked webhooks had all been deleted or switched off --
so deleting the leadership channel's webhook, or Discord rejecting it, sent
leadership alerts to every channel there was. A category somebody routed now
stays routed: with no working channel it goes nowhere, and every page that
can fix it says so.
"""

import importlib
from unittest import mock

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, TestCase
from django.urls import reverse

from ..core import alerts
from ..core.alerts import _webhooks_for
from ..models import CATEGORY_SECTIONS, AlertCategory, AlertRoute, Webhook
from ..views.common import ROUTING_SHOWN, save_routes
from .test_delivery import _Counting, _sample
from .test_webhooks import notes_of, user_with

FUEL = AlertCategory.SOV_FUEL
migration = importlib.import_module("holdfast.migrations.0014_webhook_gone_and_pinned_routes")


def _save_sov_routes(ticked=(), every=False):
    """Post the SOV routing form: every category switched on, fuel routed to
    ``ticked``, the others left unticked."""
    data = {
        f"route_enabled_{category}": "on"
        for category in AlertCategory
        if CATEGORY_SECTIONS[category] == "sov"
    }
    data[ROUTING_SHOWN] = "1"
    data[f"route_webhooks_{FUEL}"] = [str(hook.pk) for hook in ticked]
    if every:
        data[f"route_all_{FUEL}"] = "on"
    save_routes(RequestFactory().post("/", data), "sov")


class StrandedRouteTests(TestCase):
    def setUp(self):
        self.leaders = Webhook.objects.create(
            name="leaders", url="https://discord.com/api/webhooks/1/a"
        )
        self.public = Webhook.objects.create(
            name="public", url="https://discord.com/api/webhooks/2/b"
        )

    def route(self):
        return AlertRoute.objects.get(category=FUEL)

    def test_a_category_nobody_routed_reaches_every_enabled_webhook(self):
        self.assertCountEqual(_webhooks_for(FUEL), [self.leaders, self.public])
        _save_sov_routes()
        self.assertFalse(self.route().is_routed)
        self.assertCountEqual(_webhooks_for(FUEL), [self.leaders, self.public])

    def test_ticking_a_channel_routes_the_category(self):
        _save_sov_routes([self.leaders])
        self.assertTrue(self.route().is_routed)
        self.assertEqual(_webhooks_for(FUEL), [self.leaders])

    def test_its_only_channel_switched_off_sends_it_nowhere(self):
        _save_sov_routes([self.leaders])
        self.leaders.is_enabled = False
        self.leaders.save()
        self.assertEqual(_webhooks_for(FUEL), [])

    def test_its_only_channel_deleted_sends_it_nowhere(self):
        _save_sov_routes([self.leaders])
        self.leaders.delete()
        self.assertEqual(_webhooks_for(FUEL), [])

    def test_a_channel_ticked_in_admin_or_before_the_upgrade_counts(self):
        """The flag is what survives a delete; while a ticked channel exists,
        the tick alone is enough."""
        AlertRoute.for_category(FUEL).webhooks.set([self.leaders])
        self.leaders.is_enabled = False
        self.leaders.save()
        self.assertEqual(_webhooks_for(FUEL), [])

    def test_saving_the_page_untouched_does_not_widen_it(self):
        """With its webhook deleted the row shows nothing ticked. Saving the
        page to change a fuel band must not turn that into "everyone"."""
        _save_sov_routes([self.leaders])
        self.leaders.delete()
        _save_sov_routes()
        self.assertTrue(self.route().is_routed)
        self.assertEqual(_webhooks_for(FUEL), [])

    def test_choosing_every_webhook_is_explicit(self):
        _save_sov_routes([self.leaders])
        self.leaders.delete()
        _save_sov_routes(every=True)
        self.assertFalse(self.route().is_routed)
        self.assertEqual(_webhooks_for(FUEL), [self.public])

    def test_unticking_its_channels_is_a_choice_too(self):
        _save_sov_routes([self.leaders])
        _save_sov_routes()
        self.assertFalse(self.route().is_routed)
        self.assertCountEqual(_webhooks_for(FUEL), [self.leaders, self.public])

    def test_the_stranded_list(self):
        self.assertEqual(alerts.stranded_routes(), [])
        _save_sov_routes([self.leaders])
        self.leaders.delete()
        self.assertEqual([r.category for r in alerts.stranded_routes()], [FUEL])
        self.assertEqual([r.category for r in alerts.stranded_routes("sov")], [FUEL])
        self.assertEqual(alerts.stranded_routes("den"), [])

        # A category switched off is meant to be quiet; it is not stranded.
        route = self.route()
        route.is_enabled = False
        route.save()
        self.assertEqual(alerts.stranded_routes(), [])

    def test_discord_switching_off_the_last_channel_says_which_categories(self):
        _save_sov_routes([self.leaders])
        with (
            mock.patch.object(alerts, "DiscordWebhook", _Counting(404)),
            self.assertLogs(alerts.logger, level="WARNING") as logs,
        ):
            alerts.post_to(self.leaders, _sample())
        self.assertEqual(len(logs.records), 1)
        self.assertIn(str(FUEL.label), logs.output[0])
        self.assertEqual(_webhooks_for(FUEL), [])


class FreshInstallRoutingTests(TestCase):
    """The settings pages leave the routing table out while no webhook
    exists, and saving one then posted no switches at all -- which turned
    every category of the section off. A fresh install that set its tracked
    alliance before adding a webhook got nothing from that section, with no
    warning anywhere: a category switched off is not stranded."""

    def test_saving_before_any_webhook_exists_switches_nothing_off(self):
        save_routes(RequestFactory().post("/", {}), "sov")
        hook = Webhook.objects.create(name="ops", url="https://discord.com/api/webhooks/1/a")
        for category in AlertCategory:
            if CATEGORY_SECTIONS[category] == "sov":
                with self.subTest(category=category):
                    self.assertEqual(_webhooks_for(category), [hook])

    def test_the_page_marks_the_form_only_when_it_shows_the_table(self):
        self.client.force_login(user_with("manager", "sov_manage"))
        marker = f'name="{ROUTING_SHOWN}"'
        self.assertNotContains(self.client.get(reverse("holdfast:sov_settings")), marker)
        Webhook.objects.create(name="ops", url="https://discord.com/api/webhooks/1/a")
        self.assertContains(self.client.get(reverse("holdfast:sov_settings")), marker)

    def test_a_form_that_showed_the_table_still_switches_categories_off(self):
        Webhook.objects.create(name="ops", url="https://discord.com/api/webhooks/1/a")
        save_routes(RequestFactory().post("/", {ROUTING_SHOWN: "1"}), "sov")
        self.assertEqual(_webhooks_for(FUEL), [])


class StrandedPagesTests(TestCase):
    def setUp(self):
        self.client.force_login(user_with("manager", "sov_manage"))
        self.leaders = Webhook.objects.create(
            name="leaders", url="https://discord.com/api/webhooks/1/a"
        )
        self.public = Webhook.objects.create(
            name="public", url="https://discord.com/api/webhooks/2/b"
        )
        _save_sov_routes([self.leaders])

    def _warnings(self, response):
        return [text for level, text in notes_of(response) if level == "warning"]

    def test_deleting_the_last_channel_says_where_alerts_stop(self):
        response = self.client.post(reverse("holdfast:webhook_delete", args=[self.leaders.pk]))
        warnings = self._warnings(response)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(FUEL.label), warnings[0])
        self.assertEqual(_webhooks_for(FUEL), [])

    def test_deleting_a_channel_that_was_not_the_last_says_nothing_of_it(self):
        _save_sov_routes([self.leaders, self.public])
        response = self.client.post(reverse("holdfast:webhook_delete", args=[self.leaders.pk]))
        self.assertEqual(self._warnings(response), [])
        self.assertEqual(_webhooks_for(FUEL), [self.public])

    def test_switching_the_last_channel_off_says_so_too(self):
        response = self.client.post(
            reverse("holdfast:webhook_edit", args=[self.leaders.pk]),
            {"name": "leaders", "url": self.leaders.url, "ping_type": Webhook.PingType.NONE},
        )
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        warnings = self._warnings(response)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(FUEL.label), warnings[0])

    def test_saving_a_switched_off_webhook_again_does_not_repeat_it(self):
        self.leaders.is_enabled = False
        self.leaders.save()
        response = self.client.post(
            reverse("holdfast:webhook_edit", args=[self.leaders.pk]),
            {"name": "leaders", "url": self.leaders.url, "ping_type": Webhook.PingType.NONE},
        )
        self.assertEqual(self._warnings(response), [])

    def test_the_pages_that_can_fix_it_say_so(self):
        for name in ("webhooks", "sov_settings"):
            with self.subTest(page=name):
                self.assertNotContains(self.client.get(reverse(f"holdfast:{name}")), "no working channel")
        self.leaders.delete()
        for name in ("webhooks", "sov_settings"):
            with self.subTest(page=name):
                page = self.client.get(reverse(f"holdfast:{name}"))
                self.assertContains(page, "no working channel")
                self.assertContains(page, str(FUEL.label))

    def test_the_settings_page_offers_every_webhook_only_where_it_is_needed(self):
        page = self.client.get(reverse("holdfast:sov_settings"))
        self.assertNotContains(page, f'name="route_all_{FUEL}"')
        self.leaders.delete()
        page = self.client.get(reverse("holdfast:sov_settings"))
        self.assertContains(page, f'name="route_all_{FUEL}"')

    def test_a_webhook_discord_switched_off_shows_why(self):
        with (
            mock.patch.object(alerts, "DiscordWebhook", _Counting(401)),
            self.assertLogs(alerts.logger, level="WARNING"),
        ):
            response = self.client.post(reverse("holdfast:webhook_test", args=[self.leaders.pk]))
        self.leaders.refresh_from_db()
        self.assertFalse(self.leaders.is_enabled)
        errors = [text for level, text in notes_of(response) if level == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("401", errors[0])
        self.assertIn("switched off", errors[0])
        page = self.client.get(reverse("holdfast:webhooks"))
        self.assertContains(page, self.leaders.disabled_reason)

    def test_ticking_enabled_again_clears_the_reason(self):
        Webhook.objects.filter(pk=self.leaders.pk).update(
            is_enabled=False, disabled_reason="Discord: HTTP 404"
        )
        self.client.post(
            reverse("holdfast:webhook_edit", args=[self.leaders.pk]),
            {
                "name": "leaders",
                "url": self.leaders.url,
                "is_enabled": "on",
                "ping_type": Webhook.PingType.NONE,
            },
        )
        self.leaders.refresh_from_db()
        self.assertTrue(self.leaders.is_enabled)
        self.assertEqual(self.leaders.disabled_reason, "")


class PinRoutedMigrationTests(TestCase):
    """0014 marks every route that already ticks a webhook as routed, so the
    upgrade itself is when such a category stops falling back."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical = MigrationLoader(connection).project_state().apps

    def test_routes_with_a_webhook_are_marked_and_the_rest_left_alone(self):
        hook = Webhook.objects.create(name="ops", url="https://discord.com/api/webhooks/1/a")
        routed = AlertRoute.objects.create(category=FUEL)
        routed.webhooks.add(hook)
        AlertRoute.objects.create(category=AlertCategory.SOV_ADM)

        migration.mark_routed(self.historical, None)

        self.assertTrue(AlertRoute.objects.get(category=FUEL).is_routed)
        self.assertFalse(AlertRoute.objects.get(category=AlertCategory.SOV_ADM).is_routed)
