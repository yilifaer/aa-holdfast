"""What goes in front of the embed.

A webhook used to have two pings, @here and @everyone, and on an alliance
Discord both reach far more people than care. Groups fix that -- but a group
is a name, and the Discord role behind it may not exist, or the lookup may
fail because the Discord service is down or not installed at all. None of
those may cost the alert itself: a fuel warning with a missing ping is still
a fuel warning.

Auth's Discord service is not part of the test settings, so the role lookup
is stubbed at the one seam that touches it.
"""

from unittest import mock

from dhooks_lite import Embed
from django.contrib.auth.models import Group
from django.test import TestCase

from ..core import alerts
from ..models import Webhook


class _Capture:
    """Stands in for dhooks_lite.Webhook and records what would be posted."""

    posts = []

    def __init__(self, url):
        self.url = url

    def execute(self, content=None, embeds=None, username=None):
        _Capture.posts.append(content)


class PingGroupTests(TestCase):
    def setUp(self):
        _Capture.posts = []
        self.hook = Webhook.objects.create(
            name="skyhook", url="https://discord.com/api/webhooks/1/x"
        )
        self.team = Group.objects.create(name="Skyhook Team")
        self.ghost = Group.objects.create(name="No Such Role")

    def _send(self, roles, installed=True):
        """One post, with the Discord lookups answered from `roles`."""
        lookup = mock.Mock(side_effect=lambda group: roles.get(group.name))
        with (
            mock.patch.object(alerts, "DiscordWebhook", _Capture),
            mock.patch.object(alerts, "_discord_service_installed", return_value=installed),
            mock.patch.object(alerts, "_lookup_role_id", lookup),
        ):
            delivered = alerts._send(Embed(title="Fuel low"))
        return delivered, _Capture.posts, lookup

    def test_a_group_with_a_role_becomes_a_mention(self):
        self.hook.ping_groups.add(self.team)
        delivered, posts, _ = self._send({"Skyhook Team": 123})
        self.assertTrue(delivered)
        self.assertEqual(posts, ["<@&123>"])

    def test_here_comes_first_and_roles_follow(self):
        self.hook.ping_type = Webhook.PingType.HERE
        self.hook.save()
        self.hook.ping_groups.add(self.team)
        _, posts, _ = self._send({"Skyhook Team": 123})
        self.assertEqual(posts, ["@here <@&123>"])

    def test_no_groups_means_no_lookup_and_no_content(self):
        delivered, posts, lookup = self._send({})
        self.assertTrue(delivered)
        self.assertEqual(posts, [None])
        lookup.assert_not_called()

    def test_a_group_without_a_role_is_skipped_and_said_so(self):
        self.hook.ping_groups.add(self.team, self.ghost)
        with self.assertLogs(alerts.logger, level="WARNING") as logs:
            delivered, posts, _ = self._send({"Skyhook Team": 123})
        self.assertTrue(delivered)
        self.assertEqual(posts, ["<@&123>"])
        self.assertTrue(any("No Such Role" in line for line in logs.output))

    def test_one_failing_lookup_does_not_drop_the_alert(self):
        self.hook.ping_groups.add(self.team, self.ghost)

        def lookup(group):
            if group.name == "No Such Role":
                raise RuntimeError("Discord is down")
            return 123

        with (
            mock.patch.object(alerts, "DiscordWebhook", _Capture),
            mock.patch.object(alerts, "_discord_service_installed", return_value=True),
            mock.patch.object(alerts, "_lookup_role_id", side_effect=lookup),
            self.assertLogs(alerts.logger, level="WARNING"),
        ):
            delivered = alerts._send(Embed(title="Fuel low"))
        self.assertTrue(delivered)
        self.assertEqual(_Capture.posts, ["<@&123>"])

    def test_without_the_discord_service_the_alert_still_goes_out(self):
        """An install that pings groups but never set up Auth's Discord
        service gets a warning in the log, not a silent hole in its alerts."""
        self.hook.ping_groups.add(self.team)
        with self.assertLogs(alerts.logger, level="WARNING") as logs:
            delivered, posts, lookup = self._send({"Skyhook Team": 123}, installed=False)
        self.assertTrue(delivered)
        self.assertEqual(posts, [None])
        lookup.assert_not_called()
        self.assertTrue(any("not installed" in line for line in logs.output))
