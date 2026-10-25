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

import requests
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..core import alerts, den_alerts
from ..models import AlertCategory, AlertLog, DenEvent, MercenaryDen, Webhook
from .test_operations import OperationFixture
from .test_webhooks import notes_of, user_with

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
        self.assertNotIn("sent", out.getvalue())
        self.assertIn("Not delivered", err.getvalue())

    def test_a_requests_error_is_named_without_its_text_or_traceback(self):
        """requests and urllib3 put the URL in the message, and a traceback
        prints the message."""
        error = requests.ConnectionError(f"Max retries exceeded with url: {SECRET_URL}")
        with self.assertLogs(alerts.logger, level="ERROR") as logs:
            self.assertFalse(self._post(raises=error))
        self.assertNotIn("very-secret-token", "\n".join(logs.output))
        self.assertIn("ConnectionError", logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)

    def test_a_bug_of_ours_is_logged_with_its_traceback(self):
        """Anything else is not the network and carries no URL; the class
        name alone left nothing to debug it with."""
        with self.assertLogs(alerts.logger, level="ERROR") as logs:
            self.assertFalse(self._post(raises=TypeError("unexpected keyword 'embeds'")))
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIn("unexpected keyword", "\n".join(logs.output))


class PerWebhookReportTests(TestCase):
    """Two webhooks, one gone: the test paths say which is which.

    The command printed "Sent." when any one webhook took the post, and the
    test buttons and command called every failure "no channel configured, or
    switched off" -- a refusal from Discord included, which sent people to the
    routing table when the webhook was the problem.
    """

    def setUp(self):
        self.good = Webhook.objects.create(name="good", url="https://discord.com/api/webhooks/1/good")
        self.gone = Webhook.objects.create(name="gone", url="https://discord.com/api/webhooks/2/gone")

    def _answers(self, by_name):
        class _Hook:
            def __init__(self, url):
                self.url = url

            def execute(self, **_kwargs):
                return _Answer(by_name[self.url.rsplit("/", 1)[1]])

        return mock.patch.object(alerts, "DiscordWebhook", _Hook)

    def test_the_command_reports_each_webhook(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            self._answers({"good": 204, "gone": 404}),
            self.assertLogs(alerts.logger, level="WARNING"),
        ):
            call_command("holdfast_test_webhook", stdout=out, stderr=err)
        self.assertIn("sent     good", out.getvalue())
        self.assertNotIn("gone", out.getvalue())
        self.assertIn("gone: switched off", err.getvalue())
        self.assertIn("404", err.getvalue())
        self.assertNotIn("Not delivered", err.getvalue(), "one of them did take it")

    def test_a_refusal_is_not_called_no_channel(self):
        with (
            self._answers({"good": 429, "gone": 404}),
            self.assertLogs(alerts.logger, level="WARNING"),
        ):
            delivery = alerts.send_test(AlertCategory.SOV_FUEL)
        self.assertFalse(delivery.sent)
        self.assertCountEqual(
            [line.split(":")[0] for line in delivery.failures], ["good", "gone"]
        )
        text = "\n".join(delivery.failures)
        self.assertIn("good: refused by Discord", text)
        self.assertIn("gone: switched off", text)
        self.assertNotIn(alerts.NO_CHANNEL, text)

    def test_no_channel_is_still_called_no_channel(self):
        Webhook.objects.update(is_enabled=False)
        self.assertEqual(alerts.send_test(AlertCategory.SOV_FUEL).failures, [alerts.NO_CHANNEL])

    def test_the_alerts_command_says_failed_not_skipped(self):
        out = io.StringIO()
        with (
            self._answers({"good": 429, "gone": 429}),
            self.assertLogs(alerts.logger, level="WARNING"),
        ):
            call_command("holdfast_test_alerts", category=str(AlertCategory.SOV_FUEL), stdout=out)
        self.assertIn("failed", out.getvalue())
        self.assertIn("refused by Discord", out.getvalue())
        self.assertNotIn("no channel", out.getvalue())

    def test_the_settings_test_button_says_refused(self):
        self.client.force_login(user_with("manager", "sov_manage"))
        with (
            self._answers({"good": 204, "gone": 404}),
            self.assertLogs(alerts.logger, level="WARNING"),
        ):
            response = self.client.post(reverse("holdfast:sov_settings_test"))
        notes = notes_of(response)
        self.assertIn("success", [level for level, _text in notes])
        warnings = [text for level, text in notes if level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("gone: switched off", warnings[0])
        self.assertNotIn("no channel", warnings[0])


class SwitchOffRaceTests(TestCase):
    def test_a_url_fixed_while_the_post_was_out_stays_on(self):
        """Discord's 404 is about the URL it was sent to. A manager who put in
        a working one meanwhile must not find it switched off with the old
        URL's reason."""
        hook = Webhook.objects.create(name="ops", url="https://discord.com/api/webhooks/1/old")
        Webhook.objects.filter(pk=hook.pk).update(url="https://discord.com/api/webhooks/1/new")
        with (
            mock.patch.object(alerts, "DiscordWebhook", _Counting(404)),
            self.assertLogs(alerts.logger, level="WARNING") as logs,
        ):
            outcome = alerts.deliver(hook, _sample())
        self.assertEqual(outcome, alerts.REFUSED)
        hook.refresh_from_db()
        self.assertTrue(hook.is_enabled)
        self.assertEqual(hook.disabled_reason, "")
        self.assertNotIn("switched off", "\n".join(logs.output))


class _Counting:
    """A dhooks-lite stand-in that answers one status and counts the posts."""

    def __init__(self, status):
        self.status = status
        self.posts = 0

    def __call__(self, url):
        outer = self

        class _Hook:
            def execute(self, **_kwargs):
                outer.posts += 1
                return _Answer(outer.status)

        return _Hook()


def _sample():
    return alerts._embed(title="t", description="d", color=alerts.COLOR_INFO)


class GoneWebhookTests(TestCase):
    """404 and 401 do not get better by asking again.

    A deleted webhook answers 404 and one whose token was reset answers 401,
    forever. Retrying every ten minutes only filled the log and kept every
    pending alert pending; the webhook switches itself off instead and says
    why on the Webhooks page. A 429 or a 5xx is Discord being busy, and the
    next run is the retry.
    """

    def setUp(self):
        self.hook = Webhook.objects.create(name="ops", url=SECRET_URL)

    def _post(self, status):
        discord = _Counting(status)
        with mock.patch.object(alerts, "DiscordWebhook", discord):
            delivered = alerts.post_to(self.hook, _sample())
        self.hook.refresh_from_db()
        return delivered, discord

    def test_a_404_or_401_switches_the_webhook_off_with_the_reason(self):
        for status in (404, 401):
            with self.subTest(status=status):
                Webhook.objects.filter(pk=self.hook.pk).update(
                    is_enabled=True, disabled_reason="", disabled_at=None
                )
                with self.assertLogs(alerts.logger, level="WARNING") as logs:
                    delivered, _ = self._post(status)
                self.assertFalse(delivered)
                self.assertFalse(self.hook.is_enabled)
                self.assertIn(str(status), self.hook.disabled_reason)
                self.assertIsNotNone(self.hook.disabled_at)
                self.assertEqual(len(logs.records), 1, "one warning, not one per retry")
                self.assertIn("ops", logs.output[0])
                self.assertNotIn("very-secret-token", logs.output[0])

    def test_busy_and_broken_answers_stay_transient(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                with self.assertLogs(alerts.logger, level="WARNING"):
                    delivered, _ = self._post(status)
                self.assertFalse(delivered)
                self.assertTrue(self.hook.is_enabled)
                self.assertEqual(self.hook.disabled_reason, "")
                self.assertIsNone(self.hook.disabled_at)

    def test_a_switched_off_webhook_is_not_tried_again(self):
        discord = _Counting(404)
        with mock.patch.object(alerts, "DiscordWebhook", discord):
            with self.assertLogs(alerts.logger, level="WARNING"):
                self.assertFalse(alerts._send(_sample()))
            with self.assertLogs(alerts.logger, level="INFO"):
                self.assertFalse(alerts._send(_sample()))
        self.assertEqual(discord.posts, 1)

    def _gone(self):
        with self.assertLogs(alerts.logger, level="WARNING"):
            self._post(404)

    def test_saving_it_enabled_again_clears_the_reason(self):
        self._gone()
        self.hook.is_enabled = True
        self.hook.save()
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.disabled_reason, "")
        self.assertIsNone(self.hook.disabled_at)

    def test_a_webhook_left_off_keeps_its_reason_through_other_edits(self):
        self._gone()
        self.hook.notes = "the old ops channel"
        self.hook.save()
        self.hook.refresh_from_db()
        self.assertIn("404", self.hook.disabled_reason)


class DenAttackAgeTests(OperationFixture):
    """An attack Discord never took stops being retried once it is old.

    "It is not reinforced yet, so there is still a window to help" is false a
    day later, and an event left pending forever also outlived the AlertLog
    prune of its bell key and rang the owner's bell a second time.
    """

    def _event(self, hours_ago):
        return DenEvent.objects.create(
            notification_id=9_100_000_100 + hours_ago,
            den_character=self.den.den_character,
            den=self.den,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now() - timedelta(hours=hours_ago),
        )

    def test_an_old_pending_attack_is_given_up_not_posted(self):
        event = self._event(hours_ago=25)
        with (
            mock.patch.object(den_alerts, "_send", return_value=False) as send,
            mock.patch.object(den_alerts, "notify_user") as bell,
            self.assertLogs(den_alerts.logger, level="WARNING") as logs,
        ):
            den_alerts.check_den_attacks()
        event.refresh_from_db()
        self.assertTrue(event.is_alerted)
        send.assert_not_called()
        bell.assert_not_called()
        self.assertIn("den attack", logs.output[0])

    def test_a_recent_one_is_still_retried(self):
        event = self._event(hours_ago=2)
        with (
            mock.patch.object(den_alerts, "_send", return_value=False) as send,
            mock.patch.object(den_alerts, "notify_user"),
        ):
            den_alerts.check_den_attacks()
        event.refresh_from_db()
        self.assertFalse(event.is_alerted)
        send.assert_called_once()

    def test_the_bell_does_not_ring_again_after_the_prune(self):
        event = self._event(hours_ago=2)
        with (
            mock.patch.object(den_alerts, "_send", return_value=False),
            mock.patch.object(den_alerts, "notify_user") as bell,
        ):
            den_alerts.check_den_attacks()
            # A day and the prune later: the bell key is gone, the event is not.
            DenEvent.objects.filter(pk=event.pk).update(
                timestamp=timezone.now() - timedelta(hours=30)
            )
            AlertLog.objects.all().delete()
            with self.assertLogs(den_alerts.logger, level="WARNING"):
                den_alerts.check_den_attacks()
        self.assertEqual(bell.call_count, 1)


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

    def _failing_bell_runs(self, check, times=2):
        with (
            mock.patch.object(den_alerts, "_send", return_value=False),
            mock.patch.object(den_alerts, "notify_user", return_value=False) as bell,
        ):
            for _ in range(times):
                check()
        return bell

    def test_a_bell_that_failed_is_not_recorded_as_rung(self):
        """notify_user says False when Auth's notification could not be
        made. Recorded anyway, the bell for that event never rang at all."""
        DenEvent.objects.create(
            notification_id=9_100_000_002,
            den_character=self.den.den_character,
            den=self.den,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now(),
        )
        bell = self._failing_bell_runs(den_alerts.check_den_attacks)
        self.assertEqual(bell.call_count, 2, "tried again on the next run")
        self.assertFalse(AlertLog.objects.filter(key__startswith="bell:").exists())

    def test_a_reinforcement_bell_that_failed_is_tried_again(self):
        self.den.state = MercenaryDen.State.PAUSED
        self.den.save(update_fields=["state"])
        bell = self._failing_bell_runs(den_alerts.check_den_reinforced)
        self.assertEqual(bell.call_count, 2)
        self.assertFalse(AlertLog.objects.filter(key__startswith="bell:").exists())
