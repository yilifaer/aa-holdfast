"""Tactical operations: which ones count, which are announced, and how a
member waves one away.

Both came from the alliance's own operators. An operation on a level-0 den is
nothing anybody will run -- the reward scales with anarchy -- so counting it
on the badge and pinging it to Discord was noise, and the one place it could
not be silenced was the member's own dashboard. The threshold is a setting
because what is worth running is the alliance's call, not this app's. The
Ignore button is per operation because a den worth the badge can still carry
an operation its owner has decided to skip.
"""

import itertools
from datetime import timedelta
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..core import den_alerts
from ..core.attention import den_count
from ..models import (
    AlertLog,
    DenCharacter,
    EvolutionLevel,
    HoldfastConfig,
    MercenaryDen,
    MercenaryTacticalOperation,
    Webhook,
)

_character_ids = itertools.count(96_000_001)
_den_ids = itertools.count(2_000_000_000_300)


def user_with(username, *codenames):
    user = User.objects.create_user(username)
    if codenames:
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="holdfast", codename__in=codenames
            )
        )
    # Auth turns a user without a main character away from every app page.
    character = EveCharacter.objects.create(
        character_id=next(_character_ids),
        character_name=f"Main of {username}",
        corporation_id=98000300,
        corporation_name="Operations Corp",
        corporation_ticker="OPS",
    )
    CharacterOwnership.objects.create(
        user=user, character=character, owner_hash=f"main-{username}"
    )
    profile = user.profile
    profile.main_character = character
    profile.save()
    return User.objects.get(pk=user.pk)  # fresh: no permission cache


class OperationFixture(TestCase):
    """One operator with one den.

    The den sits on nobody's slot on purpose: a den of ours at anarchy 2 also
    counts as siphoning our own workforce, and that is a different item.
    """

    def setUp(self):
        cache.clear()
        self.user = user_with("operator", "basic_access", "den_member")
        self.den = self.den_for(self.user)
        self.config = HoldfastConfig.get_solo()

    def den_for(self, user, anarchy=EvolutionLevel.L2):
        ownership = CharacterOwnership.objects.get(
            user=user, character=user.profile.main_character
        )
        den_character = DenCharacter.objects.create(character_ownership=ownership)
        return MercenaryDen.objects.create(
            den_id=next(_den_ids),
            den_character=den_character,
            planet_id=40_000_300,
            state=MercenaryDen.State.RUNNING,
            anarchy_level=anarchy,
        )

    def operation(self, den=None, operation_id="op-1", hours=48):
        den = den or self.den
        return MercenaryTacticalOperation.objects.create(
            operation_id=operation_id,
            den_character=den.den_character,
            den=den,
            mercenary_den_id=den.den_id,
            dungeon_type_id=12367,
            state=MercenaryTacticalOperation.State.AVAILABLE,
            expires=timezone.now() + timedelta(hours=hours),
        )

    def set_anarchy(self, level):
        self.den.anarchy_level = level
        self.den.save(update_fields=["anarchy_level"])

    def set_threshold(self, value):
        self.config.mto_anarchy_threshold = value
        self.config.save()

    def badge(self):
        cache.clear()
        return den_count(self.user)

    def listed(self):
        """The operation rows on the operator's own dashboard."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("holdfast:den_dashboard"))
        self.assertEqual(response.status_code, 200)
        return [
            item for item in response.context["items"] if "Tactical operation" in item["kind"]
        ]


class AnarchyThresholdTests(OperationFixture):
    def test_the_default_is_anarchy_two(self):
        """Two is where a den starts paying for itself -- and where it starts
        siphoning, which is the other reason to look at it."""
        self.assertEqual(self.config.mto_anarchy_threshold, 2)

    def test_an_operation_on_a_den_below_the_threshold_is_neither_counted_nor_listed(self):
        self.set_anarchy(EvolutionLevel.L1)
        self.operation()
        self.assertEqual(self.badge(), 0)
        self.assertEqual(self.listed(), [])

    def test_at_the_threshold_it_counts_and_is_listed(self):
        self.operation()
        self.assertEqual(self.badge(), 1)
        self.assertEqual(len(self.listed()), 1)

    def test_raising_the_threshold_hides_it_again(self):
        self.operation()
        self.set_threshold(4)
        self.assertEqual(self.badge(), 0)
        self.assertEqual(self.listed(), [])

    def test_zero_means_every_operation(self):
        self.set_anarchy(EvolutionLevel.L0)
        self.operation()
        self.set_threshold(0)
        self.assertEqual(self.badge(), 1)
        self.assertEqual(len(self.listed()), 1)

    def test_the_alert_waits_for_the_den_to_reach_the_threshold(self):
        """Skipped, not sent-and-forgotten: the day anarchy rises the same
        operation is news, so nothing may be written to the alert log for it."""
        Webhook.objects.create(name="den", url="https://discord.com/api/webhooks/1/x")
        self.set_anarchy(EvolutionLevel.L1)
        self.operation()
        with mock.patch.object(den_alerts, "_send", return_value=True) as send:
            self.assertEqual(den_alerts.check_mto_available(), 0)
            send.assert_not_called()
        self.assertFalse(AlertLog.objects.filter(key__startswith="mto:").exists())

        self.set_anarchy(EvolutionLevel.L2)
        with mock.patch.object(den_alerts, "_send", return_value=True):
            self.assertEqual(den_alerts.check_mto_available(), 1)
        self.assertTrue(AlertLog.objects.filter(key="mto:op-1").exists())


class ThresholdSettingTests(OperationFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(user_with("den_manager", "den_manage"))

    def _save(self, threshold):
        response = self.client.post(
            reverse("holdfast:den_settings_save"),
            {
                "den_anchor_grace_days": "7",
                "workforce_drop_percent": "5",
                "workforce_drop_grace_hours": "24",
                "mto_anarchy_threshold": threshold,
            },
        )
        self.assertEqual(response.status_code, 302)
        return [(m.level_tag, m.message) for m in get_messages(response.wsgi_request)]

    def test_the_settings_page_shows_and_saves_it(self):
        page = self.client.get(reverse("holdfast:den_settings"))
        self.assertContains(page, 'name="mto_anarchy_threshold"')
        notes = self._save("3")
        self.assertEqual(notes[0][0], "success")
        self.assertEqual(HoldfastConfig.get_solo().mto_anarchy_threshold, 3)

    def test_rubbish_is_refused_and_nothing_is_saved(self):
        for value in ("5", "-1", "two", ""):
            with self.subTest(value=value):
                notes = self._save(value)
                self.assertEqual(notes[0][0], "error")
                self.assertIn("mto_anarchy_threshold", notes[0][1])
                self.assertEqual(HoldfastConfig.get_solo().mto_anarchy_threshold, 2)


class DismissOperationTests(OperationFixture):
    def _dismiss(self, operation, user=None, method="post"):
        self.client.force_login(user or self.user)
        url = reverse("holdfast:den_mto_dismiss", args=[operation.operation_id])
        return getattr(self.client, method)(url)

    def test_ignoring_your_own_takes_it_off_the_list_and_the_badge(self):
        operation = self.operation()
        self.assertEqual(self.badge(), 1)
        self.client.force_login(self.user)
        self.assertContains(
            self.client.get(reverse("holdfast:den_dashboard")),
            reverse("holdfast:den_mto_dismiss", args=[operation.operation_id]),
        )

        response = self._dismiss(operation)
        self.assertRedirects(response, reverse("holdfast:den_dashboard"))
        operation.refresh_from_db()
        self.assertIsNotNone(operation.dismissed_at)
        self.assertEqual(self.badge(), 0)
        self.assertEqual(self.listed(), [])

    def test_somebody_else_s_operation_is_not_yours_to_ignore(self):
        other = user_with("other", "basic_access", "den_member")
        theirs = self.operation(self.den_for(other))
        self.assertEqual(self._dismiss(theirs).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.dismissed_at)

    def test_a_get_is_refused(self):
        self.assertEqual(self._dismiss(self.operation(), method="get").status_code, 405)

    def test_without_permission_it_is_forbidden(self):
        operation = self.operation()
        self.assertEqual(self._dismiss(operation, user=user_with("nobody")).status_code, 403)
        operation.refresh_from_db()
        self.assertIsNone(operation.dismissed_at)

    def test_basic_access_alone_is_not_enough(self):
        """The button lives on the dashboard, which basic access does not
        open. Letting the POST through anyway would clear nothing the member
        can see and then bounce them into a 403 on the dashboard."""
        basic = user_with("basic", "basic_access")
        operation = self.operation(self.den_for(basic))
        self.assertEqual(self._dismiss(operation, user=basic).status_code, 403)
        operation.refresh_from_db()
        self.assertIsNone(operation.dismissed_at)

    def test_the_discord_alert_does_not_care(self):
        """Ignore is about the member's own board. The channel was told once
        when the operation appeared, and dismissal must not re-arm or
        suppress anything there."""
        Webhook.objects.create(name="den", url="https://discord.com/api/webhooks/1/x")
        operation = self.operation()
        operation.dismissed_at = timezone.now()
        operation.save(update_fields=["dismissed_at"])
        with mock.patch.object(den_alerts, "_send", return_value=True):
            self.assertEqual(den_alerts.check_mto_available(), 1)


class BasicAccessBadgeTests(OperationFixture):
    """A member with basic access alone lands on the information page, which
    does not list operations, so the badge must not count them either: a
    light nobody can find the switch for is a nag, not a notice."""

    def setUp(self):
        super().setUp()
        self.basic = user_with("basic", "basic_access")
        self.operation(self.den_for(self.basic))

    def test_the_badge_does_not_count_what_the_member_cannot_reach(self):
        cache.clear()
        self.assertEqual(den_count(self.basic), 0)
        self.client.force_login(self.basic)
        self.assertRedirects(
            self.client.get(reverse("holdfast:den_home")),
            reverse("holdfast:den_information"),
        )
        self.assertEqual(self.client.get(reverse("holdfast:den_dashboard")).status_code, 403)

    def test_the_den_list_rung_lights_it(self):
        self.basic.user_permissions.add(
            Permission.objects.get(content_type__app_label="holdfast", codename="den_member")
        )
        cache.clear()
        self.assertEqual(den_count(User.objects.get(pk=self.basic.pk)), 1)

