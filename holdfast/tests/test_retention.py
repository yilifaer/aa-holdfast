"""What is kept, for how long, and what pruning must not undo.

All of it came out of running the AA app checklist against 0.1.10.

The alert log was pruned by age alone. A row in it is what stops an alert
repeating, so dropping one whose condition still held re-armed the alert:
an ADM that stayed low was announced again every fourteen days.

Den notifications were kept forever. Their text names the attacker's
character, corporation and alliance -- people with no account here -- so
they now go after HOLDFAST_DEN_EVENT_RETENTION_DAYS.

And a claim was deleted with its slot, which goes whenever its skyhook drops
out of a listing: the record of who was granted which ground vanished with
the ground.
"""

import importlib
from datetime import timedelta
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from django.core.cache import cache
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import tasks
from ..app_settings import (
    HOLDFAST_DEN_EVENT_RETENTION_DAYS,
    HOLDFAST_STALE_PRUNE_DAYS,
)
from ..core import alerts, den_sync
from ..models import (
    AlertLog,
    DenCharacter,
    DenClaim,
    DenEvent,
    DenSlot,
    HoldfastConfig,
    MercenaryTacticalOperation,
    Owner,
    Skyhook,
    SovCampaign,
    SovHub,
    SovSystem,
)
from .test_home_alliance import (
    HOME,
    character_for,
    corporation,
    set_home,
    user_with,
)
from .test_operations import OperationFixture
from .test_operations import user_with as operator_with

snapshot = importlib.import_module("holdfast.migrations.0016_claim_history_outlives_slot")

OLD = timedelta(days=HOLDFAST_STALE_PRUNE_DAYS + 1)


def _sent(key, ago=OLD):
    return AlertLog.objects.create(key=key, sent_at=timezone.now() - ago)


def _kept():
    return set(AlertLog.objects.values_list("key", flat=True))


class AlertLogPruneTests(OperationFixture):
    """Only rows that can no longer stop anything are aged out."""

    def setUp(self):
        super().setUp()
        set_home(HOME)
        self.owner = Owner.objects.create(corporation=corporation(98000800, "Home Corp", HOME))
        self.hub = SovHub.objects.create(
            hub_id=9_000_001,
            owner=self.owner,
            solar_system_id=30_000_801,
            fuel_expires_at=timezone.now() + timedelta(days=2),
        )
        self.skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_801, owner=self.owner, planet_id=40_000_801
        )

    def _prune(self):
        tasks.prune_alert_log()
        return _kept()

    def test_a_young_row_is_kept_whatever_it_is(self):
        _sent("adm:1", ago=timedelta(days=1))
        _sent("nobody-knows:1", ago=timedelta(days=1))
        self.assertEqual(self._prune(), {"adm:1", "nobody-knows:1"})

    def test_low_adm_that_stays_low_is_not_announced_again(self):
        """The bug as it was seen: the same system, every fourteen days."""
        config = HoldfastConfig.get_solo()
        config.adm_alert_threshold = 3.0
        config.save()
        SovSystem.objects.create(
            solar_system_id=30_000_802, alliance_id=HOME, activity_defense_multiplier=1.5
        )
        _sent("adm:30000802")
        self._prune()
        with mock.patch.object(alerts, "_send", return_value=True) as send:
            alerts.check_adm()
        send.assert_not_called()

    def test_edge_rows_stay_while_their_subject_exists_and_go_with_it(self):
        """Their check deletes them the moment the condition clears, so one
        still standing means the condition still holds."""
        den = self.den
        campaign = SovCampaign.objects.create(
            campaign_id=77, structure_id=self.hub.hub_id, solar_system_id=30_000_801,
            event_type=SovCampaign.EventType.IHUB, start_time=timezone.now(),
        )
        live = {
            f"upglow:{self.hub.hub_id}",
            f"reinf:{self.skyhook.skyhook_id}:ArmorReinforced",
            f"sovreinf:{campaign.campaign_id}",
            f"denreinf:{den.den_id}",
            f"bell:denreinf:{den.den_id}",
            f"siphon:{self.skyhook.skyhook_id}",
            f"siphonfp:{self.skyhook.skyhook_id}:10.0",
        }
        gone = {
            "upglow:1", "reinf:1:ArmorReinforced", "sovreinf:1", "denreinf:1",
            "bell:denreinf:1", "siphon:1", "siphonfp:1:10.0", "adm:1",
        }
        for key in live | gone:
            _sent(key)
        self.assertEqual(self._prune(), live)

    def test_a_fuel_row_stays_while_it_is_still_the_hub_s_prediction(self):
        stamp = self.hub.fuel_expires_at.strftime("%Y%m%d%H")
        _sent(f"fuel:{self.hub.hub_id}:danger:{stamp}")
        _sent(f"fuel:{self.hub.hub_id}:danger:2026010100")  # refuelled since
        _sent(f"fuel:1:danger:{stamp}")  # hub gone
        self.assertEqual(self._prune(), {f"fuel:{self.hub.hub_id}:danger:{stamp}"})

    def test_a_theft_row_stays_only_while_its_window_is_still_ahead(self):
        start = timezone.now() + timedelta(hours=1)
        self.skyhook.theft_start = start
        self.skyhook.theft_end = start + timedelta(hours=2)
        self.skyhook.save()
        current = f"theft:{self.skyhook.skyhook_id}:{start:%Y%m%d%H%M}"
        _sent(current)
        _sent(f"theft:{self.skyhook.skyhook_id}:202601010000")
        self.assertEqual(self._prune(), {current})

    def test_an_mto_row_stays_while_the_operation_is_live(self):
        self.operation(operation_id="live")
        done = self.operation(operation_id="done")
        done.state = MercenaryTacticalOperation.State.COMPLETED
        done.save()
        for op in ("live", "done", "gone"):
            _sent(f"mto:{op}")
        self.assertEqual(self._prune(), {"mto:live"})

    def test_an_attack_bell_stays_while_its_event_is_pending(self):
        for notification_id, alerted in ((501, False), (502, True)):
            DenEvent.objects.create(
                notification_id=notification_id,
                den_character=self.den.den_character,
                den=self.den,
                kind=DenEvent.Kind.ATTACKED,
                timestamp=timezone.now(),
                is_alerted=alerted,
            )
            _sent(f"bell:denattack:{notification_id}")
        _sent("bell:denattack:503")
        self.assertEqual(self._prune(), {"bell:denattack:501"})

    def test_a_row_of_no_known_kind_ages_out(self):
        _sent("from-an-older-release:1")
        _sent("fuel:not-a-number:danger:x")
        self.assertEqual(self._prune(), set())


class DenEventRetentionTests(OperationFixture):
    def _event(self, notification_id, days_ago):
        return DenEvent.objects.create(
            notification_id=notification_id,
            den_character=self.den.den_character,
            den=self.den,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now() - timedelta(days=days_ago),
            text="aggressorCharacterID: 2112000001",
            is_alerted=True,
        )

    def test_the_prune_drops_events_past_retention(self):
        self._event(601, HOLDFAST_DEN_EVENT_RETENTION_DAYS + 1)
        self._event(602, 2)
        tasks.prune_alert_log()
        self.assertEqual(list(DenEvent.objects.values_list("notification_id", flat=True)), [602])

    def test_a_pruned_notification_is_not_imported_again(self):
        """ESI keeps a notification for as long as it likes; reading it back
        in every sync would undo the prune."""
        now = timezone.now()
        notes = [
            {
                "notification_id": 701,
                "type": "MercenaryDenAttacked",
                "timestamp": now - timedelta(days=HOLDFAST_DEN_EVENT_RETENTION_DAYS + 1),
                "text": "",
            },
            {"notification_id": 702, "type": "MercenaryDenAttacked", "timestamp": now, "text": ""},
        ]
        with mock.patch.object(den_sync, "esi") as client:
            operation = client.client.Character.GetCharactersCharacterIdNotifications
            operation.return_value.result.return_value = [
                mock.Mock(model_dump=mock.Mock(return_value=note)) for note in notes
            ]
            den_sync.sync_notifications(self.den.den_character, token=object())
        self.assertEqual(list(DenEvent.objects.values_list("notification_id", flat=True)), [702])


class FirstSyncAfterRetentionTests(OperationFixture):
    """Pruning an operator's old events must not make them new again.

    "First sync" was "has no DenEvent", which narrows the window to
    HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES. Once retention emptied a quiet
    operator's history, the next attack read late -- a worker or ESI outage,
    a spent bucket -- was filed as backfill: no post, no bell.
    """

    def _sync(self, *notes):
        with mock.patch.object(den_sync, "esi") as client:
            operation = client.client.Character.GetCharactersCharacterIdNotifications
            operation.return_value.result.return_value = [
                mock.Mock(model_dump=mock.Mock(return_value=note)) for note in notes
            ]
            den_sync.sync_notifications(self.den.den_character, token=object())

    def _attack(self, notification_id, hours_ago):
        return {
            "notification_id": notification_id,
            "type": "MercenaryDenAttacked",
            "timestamp": timezone.now() - timedelta(hours=hours_ago),
            "text": "",
        }

    def test_a_late_attack_after_the_prune_is_still_announced(self):
        self._sync()
        DenEvent.objects.create(
            notification_id=801,
            den_character=self.den.den_character,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now() - timedelta(days=HOLDFAST_DEN_EVENT_RETENTION_DAYS + 10),
            is_alerted=True,
        )
        tasks.prune_alert_log()
        self.assertFalse(DenEvent.objects.exists(), "the history is gone")

        self._sync(self._attack(802, hours_ago=3))
        self.assertFalse(DenEvent.objects.get(notification_id=802).is_alerted)

    def test_a_genuine_first_sync_still_files_hours_old_events_as_backfill(self):
        self._sync(self._attack(803, hours_ago=3), self._attack(804, hours_ago=0))
        alerted = dict(DenEvent.objects.values_list("notification_id", "is_alerted"))
        self.assertEqual(alerted, {803: True, 804: False})

    def test_a_first_sync_that_found_nothing_still_counts(self):
        self._sync()
        self.den.den_character.refresh_from_db()
        self.assertIsNotNone(self.den.den_character.notifications_synced_at)
        self._sync(self._attack(805, hours_ago=3))
        self.assertFalse(DenEvent.objects.get(notification_id=805).is_alerted)

    def test_a_spent_bucket_is_not_a_sync(self):
        with mock.patch.object(den_sync, "esi") as client:
            operation = client.client.Character.GetCharactersCharacterIdNotifications
            operation.return_value.result.side_effect = den_sync.ESIBucketLimitException(
                mock.Mock(), 60
            )
            with self.assertLogs(den_sync.logger, level="WARNING"):
                den_sync.sync_notifications(self.den.den_character, token=object())
        self.den.den_character.refresh_from_db()
        self.assertIsNone(self.den.den_character.notifications_synced_at)


class NotificationsSyncedMigrationTests(OperationFixture):
    """0017 marks everyone whose list was read before the upgrade, so the
    upgrade narrows nobody's window."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical = MigrationLoader(connection).project_state().apps
        cls.migration = importlib.import_module("holdfast.migrations.0017_den_notifications_synced")

    def test_characters_with_events_or_a_finished_sync_are_marked(self):
        with_events = self.den.den_character
        DenEvent.objects.create(
            notification_id=901,
            den_character=with_events,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now(),
        )
        synced = self.den_for(operator_with("synced", "basic_access")).den_character
        synced.mark_sync(True)
        never = self.den_for(operator_with("never", "basic_access")).den_character
        never.mark_sync(False, "no token")
        DenCharacter.objects.update(notifications_synced_at=None)

        self.migration.mark_synced(self.historical, None)

        marks = dict(DenCharacter.objects.values_list("pk", "notifications_synced_at"))
        self.assertIsNotNone(marks[with_events.pk])
        self.assertIsNotNone(marks[synced.pk])
        self.assertIsNone(marks[never.pk])


class ClaimFixture(TestCase):
    """A pending claim on a slot of ours, by a member with a den character."""

    def setUp(self):
        cache.clear()
        set_home(HOME)
        home_corp = corporation(98000810, "Home Corp", HOME)
        self.owner = Owner.objects.create(corporation=home_corp)
        self.skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_810, owner=self.owner, planet_id=40_000_810
        )
        self.slot = DenSlot.objects.create(skyhook=self.skyhook)
        self.user = user_with("applicant", "basic_access", "den_member")
        self.character = character_for(self.user, 94000810, home_corp, HOME)
        DenCharacter.objects.create(
            character_ownership=CharacterOwnership.objects.get(character=self.character)
        )
        self.claim = DenClaim.objects.create(
            slot=self.slot, user=self.user, character=self.character
        )

    def _lose_the_skyhook(self):
        self.skyhook.delete()
        self.claim.refresh_from_db()


class ClaimHistoryTests(ClaimFixture):
    def test_the_claim_outlives_its_slot_and_keeps_its_name(self):
        self.assertEqual(self.claim.slot_planet_name, "40000810")
        self._lose_the_skyhook()
        self.assertIsNone(self.claim.slot)
        self.assertEqual(self.claim.planet_name, "40000810")
        self.assertIn("40000810", str(self.claim))

    def test_the_member_s_page_still_renders_it(self):
        self._lose_the_skyhook()
        self.client.force_login(self.user)
        response = self.client.get(reverse("holdfast:den_information"))
        self.assertContains(response, "40000810")

    def test_a_pending_claim_on_a_vanished_slot_is_closed_by_the_slot_sync(self):
        """Nobody can approve a claim on ground that is gone; left pending it
        would sit on the member's page for good."""
        self._lose_the_skyhook()
        den_sync.sync_den_slots()
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, DenClaim.Status.REJECTED)
        self.assertTrue(self.claim.decision_note)
        self.assertIsNotNone(self.claim.decided_at)

    def test_withdrawing_and_clearing_it_still_work(self):
        self._lose_the_skyhook()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("holdfast:den_withdraw_claim", args=[self.claim.pk]), follow=True
        )
        self.assertContains(response, "40000810")
        response = self.client.post(
            reverse("holdfast:den_dismiss_claim", args=[self.claim.pk]), follow=True
        )
        self.claim.refresh_from_db()
        self.assertIsNotNone(self.claim.dismissed_at)

    def test_the_den_admin_page_renders_with_it(self):
        self._lose_the_skyhook()
        officer = user_with("officer", "den_manage")
        character_for(officer, 94000811, Owner.objects.get().corporation, HOME)
        self.client.force_login(officer)
        response = self.client.get(reverse("holdfast:den_admin"))
        self.assertEqual(response.status_code, 200)


class ClaimSnapshotMigrationTests(ClaimFixture):
    """0016 names the claims that exist, and on the way back drops the ones a
    non-null slot cannot hold -- which is what the cascade did before."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical = MigrationLoader(connection).project_state().apps

    def test_existing_claims_are_named(self):
        DenClaim.objects.filter(pk=self.claim.pk).update(slot_planet_name="", slot_system_name="")
        snapshot.snapshot_slot_names(self.historical, None)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.slot_planet_name, "40000810")

    def test_going_back_drops_only_claims_without_a_slot(self):
        orphan = DenClaim.objects.create(slot=None, user=self.user, character=self.character)
        snapshot.drop_orphaned_claims(self.historical, None)
        self.assertFalse(DenClaim.objects.filter(pk=orphan.pk).exists())
        self.assertTrue(DenClaim.objects.filter(pk=self.claim.pk).exists())
