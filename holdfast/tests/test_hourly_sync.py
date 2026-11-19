"""The owner sync runs hourly, and nothing quietly gets slower for it.

CCP caches the structure routes for an hour, so a sync every fifteen minutes
mostly re-read the same answers. Running it hourly cuts the calls, but a
corporation with more structures than one run's budget would then take four
times as long to rotate through them. The top-up at :22, :37 and :52 is what
prevents that, and a structure refreshed in the last 50 minutes is not
fetched again.
"""

from datetime import timedelta
from unittest import mock

from allianceauth.eveonline.models import EveCorporationInfo
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .. import tasks
from ..app_settings import HOLDFAST_DETAIL_CALLS_PER_RUN
from ..core import esi_sync
from ..core.esi_sync import DETAIL_FRESH, TOKENS_PER_CALL, DetailBudget
from ..models import Owner, Skyhook, SovHub
from ..schedule import CELERYBEAT_SCHEDULE


def make_owner(tag, **kw):
    corporation = EveCorporationInfo.objects.create(
        corporation_id=98_600_000 + tag,
        corporation_name=f"Hourly Corp {tag}",
        corporation_ticker=f"H{tag}",
        member_count=3,
    )
    return Owner.objects.create(corporation=corporation, **kw)


class ScheduleTests(SimpleTestCase):
    def test_the_owner_sync_is_hourly(self):
        entry = CELERYBEAT_SCHEDULE["holdfast_update_all_owners"]
        self.assertEqual(entry["task"], "holdfast.tasks.update_all_owners")
        self.assertEqual(entry["schedule"].minute, {7})
        self.assertEqual(len(entry["schedule"].hour), 24)

    def test_the_top_up_runs_in_between(self):
        entry = CELERYBEAT_SCHEDULE["holdfast_top_up_owner_details"]
        self.assertEqual(entry["task"], "holdfast.tasks.top_up_all_owners")
        self.assertEqual(entry["schedule"].minute, {22, 37, 52})

    def test_workforce_tracking_still_follows_every_possible_refresh(self):
        """Five minutes after the sync and after each top-up."""
        tracking = CELERYBEAT_SCHEDULE["holdfast_track_workforce"]["schedule"].minute
        refreshes = (
            CELERYBEAT_SCHEDULE["holdfast_update_all_owners"]["schedule"].minute
            | CELERYBEAT_SCHEDULE["holdfast_top_up_owner_details"]["schedule"].minute
        )
        self.assertEqual({(m + 5) % 60 for m in refreshes}, set(tracking))

    def test_one_run_still_stays_under_half_the_bucket(self):
        """The hourly run is the heavy one: two listings and a full budget."""
        spend = (HOLDFAST_DETAIL_CALLS_PER_RUN + 2) * TOKENS_PER_CALL
        self.assertLess(spend, 150)


class DueTests(TestCase):
    def setUp(self):
        self.owner = make_owner(1)
        self.now = timezone.now()

    def hub(self, hub_id, minutes_ago):
        return SovHub.objects.create(
            hub_id=hub_id,
            owner=self.owner,
            solar_system_id=30_000_000 + hub_id,
            detail_updated_at=None
            if minutes_ago is None
            else self.now - timedelta(minutes=minutes_ago),
        )

    def test_the_window_is_fifty_minutes(self):
        self.assertEqual(DETAIL_FRESH, timedelta(minutes=50))

    def test_what_counts_as_due(self):
        self.hub(1, None)
        self.hub(2, 49)
        self.hub(3, 51)
        Skyhook.objects.create(
            skyhook_id=9,
            owner=self.owner,
            planet_id=1,
            detail_updated_at=self.now - timedelta(minutes=5),
        )
        self.assertEqual(esi_sync.details_due_count(self.owner, self.now), 2)

    def test_refresh_skips_what_was_refreshed_49_minutes_ago_and_takes_51(self):
        self.hub(2, 49)
        self.hub(3, 51)
        fetched = []
        with mock.patch.object(
            esi_sync,
            "_fetch_sov_hub_detail",
            side_effect=lambda owner, token, hub, memo: fetched.append(hub.hub_id),
        ):
            hubs, hooks = esi_sync.refresh_details(
                self.owner, None, DetailBudget(10 * TOKENS_PER_CALL), {}
            )
        self.assertEqual(fetched, [3])
        self.assertEqual((hubs, hooks), (1, 0))

    def test_complete_means_nothing_is_still_due(self):
        for hub_id in range(1, 5):
            self.hub(hub_id, 120)

        def fetch(owner, token, hub, memo):
            SovHub.objects.filter(pk=hub.pk).update(detail_updated_at=timezone.now())

        with (
            mock.patch.object(esi_sync, "_fetch_sov_hub_detail", side_effect=fetch),
            mock.patch.object(Owner, "fetch_token", return_value=object()),
        ):
            partial = esi_sync.update_owner(self.owner, detail_budget=2, listings=False)
            rest = esi_sync.update_owner(self.owner, detail_budget=2, listings=False)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["hub_details"], 2)
        self.assertTrue(rest["complete"])

    def test_nothing_due_means_no_token_and_no_call(self):
        self.hub(1, 10)
        with (
            mock.patch.object(Owner, "fetch_token") as token,
            mock.patch.object(esi_sync, "esi") as client,
        ):
            stats = esi_sync.update_owner(self.owner, listings=False)
        token.assert_not_called()
        self.assertEqual(client.mock_calls, [])
        self.assertTrue(stats["complete"])

    def test_the_hourly_run_always_pulls_the_listings(self):
        with (
            mock.patch.object(Owner, "fetch_token", return_value=object()) as token,
            mock.patch.object(esi_sync, "sync_sov_hub_listing", return_value=0) as hubs,
            mock.patch.object(esi_sync, "sync_skyhook_listing", return_value=0) as hooks,
        ):
            esi_sync.update_owner(self.owner)
        token.assert_called_once()
        hubs.assert_called_once()
        hooks.assert_called_once()


class TopUpTaskTests(TestCase):
    def setUp(self):
        self.busy = make_owner(10)
        self.idle = make_owner(11)
        SovHub.objects.create(hub_id=1, owner=self.busy, solar_system_id=1)
        SovHub.objects.create(
            hub_id=2, owner=self.idle, solar_system_id=2, detail_updated_at=timezone.now()
        )

    def test_only_owners_with_something_due_are_queued_without_listings(self):
        with mock.patch.object(tasks.update_owner, "apply_async") as queued:
            self.assertEqual(tasks.top_up_all_owners(), 1)
        queued.assert_called_once()
        self.assertEqual(queued.call_args.kwargs["args"], [self.busy.pk])
        self.assertEqual(queued.call_args.kwargs["kwargs"], {"listings": False})

    def test_nothing_due_queues_nothing(self):
        SovHub.objects.filter(owner=self.busy).update(detail_updated_at=timezone.now())
        with mock.patch.object(tasks.update_owner, "apply_async") as queued:
            self.assertEqual(tasks.top_up_all_owners(), 0)
        queued.assert_not_called()

    def test_a_top_up_with_nothing_due_calls_nothing_and_records_nothing(self):
        with (
            mock.patch.object(esi_sync, "update_owner") as sync,
            mock.patch.object(Owner, "fetch_token") as token,
        ):
            self.assertIsNone(tasks.update_owner(self.idle.pk, listings=False))
        sync.assert_not_called()
        token.assert_not_called()
        self.idle.refresh_from_db()
        self.assertIsNone(self.idle.last_sync_at)

    def test_a_top_up_with_something_due_syncs_without_listings(self):
        stats = {
            "sov_hubs": 1,
            "skyhooks": 0,
            "hub_details": 1,
            "skyhook_details": 0,
            "complete": True,
            "rate_limited": False,
        }
        with mock.patch.object(esi_sync, "update_owner", return_value=stats) as sync:
            tasks.update_owner(self.busy.pk, listings=False)
        sync.assert_called_once_with(mock.ANY, listings=False)
        self.busy.refresh_from_db()
        self.assertTrue(self.busy.last_sync_ok)
