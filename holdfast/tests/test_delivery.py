"""What counts as delivered, and how often a den owner's bell rings.

Both came out of running the AA app checklist against 0.1.10.

dhooks-lite raises only for network trouble. A webhook somebody deleted
answers 404, a busy one 429, and both came back as an ordinary response that
post_to took for success -- so the alert was recorded as sent and never seen,
and the Send test button said the channel worked.

The den attack and reinforcement checks rang the owner's bell before looking
at whether Discord had taken the post. The event stays pending until it does,
so with den alerts switched off or no webhook at all, the same bell rang every
ten minutes for as long as that lasted.
"""

import io
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from ..core import alerts, den_alerts
from ..models import DenEvent, MercenaryDen, Webhook
from .test_operations import OperationFixture

SECRET_URL = "https://discord.com/api/webhooks/123/very-secret-token"


class _Answer:
    """What dhooks-lite hands back: a status, raised for nothing."""

    def __init__(self, status):
        self.status_code = status
        self.status_ok = 200 <= status <= 299


def _webhook_answering(status=None, raises=None):
    class _Hook:
        def __init__(self, url):
            self.url = url

        def execute(self, **_kwargs):
            if raises:
                raise raises
            return _Answer(status)

    return _Hook


class DeliveryTests(TestCase):
    def setUp(self):
        self.hook = Webhook.objects.create(name="ops", url=SECRET_URL)

    def _post(self, **answer):
        with mock.patch.object(alerts, "DiscordWebhook", _webhook_answering(**answer)):
            return alerts.post_to(self.hook, alerts._embed(title="t", description="d", color=alerts.COLOR_INFO))

    def test_a_2xx_answer_is_delivered(self):
        self.assertTrue(self._post(status=204))

    def test_a_refused_post_is_not(self):
        for status in (401, 404, 429):
            with self.assertLogs(alerts.logger, level="WARNING") as logs:
                self.assertFalse(self._post(status=status), status)
            self.assertIn(str(status), logs.output[0])

    def test_a_network_error_is_not_and_the_url_stays_out_of_the_log(self):
        """The URL is the webhook's password, and requests puts it in the
        exception's text."""
        error = ConnectionError(f"Max retries exceeded with url: {SECRET_URL}")
        with self.assertLogs(alerts.logger, level="WARNING") as logs:
            self.assertFalse(self._post(raises=error))
        self.assertNotIn("very-secret-token", "\n".join(logs.output))

    def test_an_alert_that_was_refused_is_not_recorded_as_sent(self):
        with mock.patch.object(alerts, "DiscordWebhook", _webhook_answering(status=404)):
            with self.assertLogs(alerts.logger, level="WARNING"):
                delivered = alerts._send(alerts._embed(title="t", description="d", color=alerts.COLOR_INFO))
        self.assertFalse(delivered)

    def test_the_test_command_says_when_nothing_arrived(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(alerts, "DiscordWebhook", _webhook_answering(status=404)):
            with self.assertLogs(alerts.logger, level="WARNING"):
                call_command("holdfast_test_webhook", stdout=out, stderr=err)
        self.assertNotIn("Sent.", out.getvalue())
        self.assertIn("Not delivered", err.getvalue())


class BellOnceTests(OperationFixture):
    """Discord failing, three alert runs: the bell rings once."""

    def _runs(self, check, times=3):
        with (
            mock.patch.object(den_alerts, "_send", return_value=False) as send,
            mock.patch.object(den_alerts, "notify_user") as bell,
        ):
            for _ in range(times):
                check()
        return send, bell

    def test_an_attack_rings_once_while_discord_keeps_retrying(self):
        DenEvent.objects.create(
            notification_id=9_100_000_001,
            den_character=self.den.den_character,
            den=self.den,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now(),
        )
        send, bell = self._runs(den_alerts.check_den_attacks)
        self.assertEqual(bell.call_count, 1)
        self.assertEqual(send.call_count, 3, "Discord is still retried")

    def test_a_reinforcement_rings_once_and_again_after_the_next_one(self):
        self.den.state = MercenaryDen.State.PAUSED
        self.den.reinforce_end = timezone.now() + timedelta(hours=20)
        self.den.save(update_fields=["state", "reinforce_end"])
        _send, bell = self._runs(den_alerts.check_den_reinforced)
        self.assertEqual(bell.call_count, 1)

        self.den.state = MercenaryDen.State.RUNNING
        self.den.save(update_fields=["state"])
        self._runs(den_alerts.check_den_reinforced, times=1)
        self.den.state = MercenaryDen.State.PAUSED
        self.den.save(update_fields=["state"])
        _send, bell = self._runs(den_alerts.check_den_reinforced)
        self.assertEqual(bell.call_count, 1, "a new reinforcement rings again")
