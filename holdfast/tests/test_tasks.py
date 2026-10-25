"""Task locking, the raid list, and registering while the broker is down.

All three came out of running the AA app checklist against 0.1.10.

No task was queued once. A run_alerts that outlived its ten minutes ran beside
the next one and could post the same alert twice, and a second registration
of the same corporation queued a second sync of it. Every task now goes
through Alliance Auth's QueueOnce, which takes a lock in the Django cache when
the task is queued and drops it when the run ends.

The Raid Targets page called ESI inside the request whenever the cached list
had expired -- routinely, with a five-minute cache under a ten-minute refresh
-- so an ESI error was a 500, and a 304 after expiry was an empty list. The
page now only reads what the task stored.

And a registration saved its rows and then queued the first sync; with the
broker down the queueing raised, and the member got a 500 for a registration
that had gone through.
"""

import inspect
from unittest import mock

from allianceauth.services.tasks import QueueOnce
from celery import Task
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse
from kombu.exceptions import OperationalError

from .. import tasks
from ..app_settings import (
    HOLDFAST_OWNER_SYNC_JITTER_SECONDS,
    HOLDFAST_RAIDABLE_CACHE_SECONDS,
)
from ..core import esi_sync
from ..models import DenCharacter, DenClaim, DenSlot, Owner, Skyhook
from ..schedule import CELERYBEAT_SCHEDULE
from ..views import den as den_views
from ..views import owners as owners_views
from .test_home_alliance import (
    HOME,
    Token,
    _load_charlink_hook,
    _not_modified,
    character_for,
    corporation,
    messages_of,
    request_for,
    set_home,
    stub_corporation_refresh,
    user_with,
)

BROKER_DOWN = OperationalError("[Errno 111] Connection refused")

# Queued one row at a time by the fan-out above them, so they share its gap.
FANNED_OUT = {
    "update_owner": "update_all_owners",
    "update_den_character": "update_all_den_characters",
}


def _all_tasks():
    return {
        name: task
        for name, task in vars(tasks).items()
        if isinstance(task, Task) and task.name.startswith("holdfast.")
    }


def _gap_seconds(schedule):
    """The shortest time between two runs of a crontab entry."""
    if len(schedule.hour) < 24:
        hours = sorted(schedule.hour)
        gaps = [b - a for a, b in zip(hours, hours[1:] + [hours[0] + 24], strict=True)]
        return min(gaps) * 3600
    minutes = sorted(schedule.minute)
    gaps = [b - a for a, b in zip(minutes, minutes[1:] + [minutes[0] + 60], strict=True)]
    return min(gaps) * 60


def _schedule_gap(task_name):
    task_name = FANNED_OUT.get(task_name, task_name)
    for entry in CELERYBEAT_SCHEDULE.values():
        if entry["task"] == f"holdfast.tasks.{task_name}":
            return _gap_seconds(entry["schedule"])
    raise AssertionError(f"{task_name} is not scheduled")


class LockSettingsTests(SimpleTestCase):
    def test_every_task_is_queued_once_and_quietly(self):
        """graceful: a second dispatch is dropped, not raised at the caller --
        which is a view or the beat."""
        self.assertTrue(_all_tasks())
        for name, task in _all_tasks().items():
            with self.subTest(task=name):
                self.assertIsInstance(task, QueueOnce)
                self.assertIs(task.once.get("graceful"), True)
                self.assertIn("timeout", task.once)

    def test_per_object_tasks_lock_per_object(self):
        """One owner's sync must not stop another's from being queued."""
        self.assertEqual(tasks.update_owner.once["keys"], ["owner_pk"])
        self.assertEqual(tasks.update_den_character.once["keys"], ["den_character_pk"])

    def test_a_lock_outlives_its_run_and_expires_before_the_next(self):
        """The lock is taken when the task is queued and dropped when it ends.
        It has to cover the fan-out's countdown plus the longest run the time
        limit allows, or a slow run overlaps the next; and it has to expire
        before the next scheduled run, or one left behind by a killed worker
        silently skips that run too."""
        for name, task in _all_tasks().items():
            with self.subTest(task=name):
                self.assertTrue(task.time_limit, "no time limit")
                waits = HOLDFAST_OWNER_SYNC_JITTER_SECONDS if name in FANNED_OUT else 0
                self.assertLess(task.time_limit + waits, task.once["timeout"])
                self.assertLess(task.once["timeout"], _schedule_gap(name))


class QueueOnceTests(TestCase):
    """Against the real lock: Auth's DjangoBackend in the test Redis, as
    runtests.py configures it. Only the broker hand-off is stubbed."""

    def setUp(self):
        cache.clear()

    def test_a_second_dispatch_while_one_is_queued_is_dropped(self):
        with mock.patch.object(Task, "apply_async") as sent:
            tasks.run_alerts.delay()
            tasks.run_alerts.delay()
        sent.assert_called_once()

    def test_each_owner_has_its_own_lock(self):
        with mock.patch.object(Task, "apply_async") as sent:
            tasks.update_owner.delay(1)
            tasks.update_owner.delay(2)
            tasks.update_owner.delay(1)
        self.assertEqual(sent.call_count, 2)

    def test_the_fan_out_skips_an_owner_already_queued(self):
        set_home(HOME)
        owner = Owner.objects.create(corporation=corporation(98000700, "Home Corp", HOME))
        with mock.patch.object(Task, "apply_async") as sent:
            tasks.update_owner.delay(owner.pk)
            tasks.update_all_owners()
        sent.assert_called_once()

    def test_enqueue_counts_an_already_queued_task_as_queued(self):
        with mock.patch.object(Task, "apply_async") as sent:
            self.assertTrue(tasks.enqueue(tasks.update_owner, 5))
            self.assertTrue(tasks.enqueue(tasks.update_owner, 5))
        sent.assert_called_once()

    def test_a_failed_dispatch_says_so_and_leaves_no_lock_behind(self):
        """The lock is taken before the broker is asked. Left behind, it would
        turn the next scheduled run of that owner away as well."""
        with (
            mock.patch.object(Task, "apply_async", side_effect=BROKER_DOWN),
            self.assertLogs(tasks.logger, level="ERROR"),
        ):
            self.assertFalse(tasks.enqueue(tasks.update_owner, 5))
        with mock.patch.object(Task, "apply_async") as sent:
            self.assertTrue(tasks.enqueue(tasks.update_owner, 5))
        sent.assert_called_once()


# --------------------------------------------------------------------------
# Raid Targets
# --------------------------------------------------------------------------

ROW = {
    "planet_id": 40_000_901,
    "solar_system_id": 30_000_901,
    "theft_vulnerability": {"start": None, "end": None},
}


def _answer(*rows):
    return mock.Mock(skyhooks=[mock.Mock(model_dump=mock.Mock(return_value=r)) for r in rows])


class RaidableTests(TestCase):
    def setUp(self):
        cache.delete(esi_sync.RAIDABLE_CACHE_KEY)
        self.member = user_with("member", "basic_access")
        character_for(self.member, 94000720, corporation(98000720, "Home Corp"))
        self.client.force_login(self.member)

    def _page(self):
        with (
            mock.patch.object(esi_sync, "esi") as client,
            mock.patch.object(tasks.refresh_raidable, "delay") as queued,
        ):
            response = self.client.get(reverse("holdfast:skyhook_raid"))
        self.assertEqual(response.status_code, 200)
        client.client.Activities.GetSkyhooksRaidable.assert_not_called()
        return response, queued

    def test_a_cold_cache_says_so_and_queues_a_fetch_without_calling_esi(self):
        response, queued = self._page()
        self.assertTrue(response.context["fetching"])
        self.assertContains(response, "being fetched")
        queued.assert_called_once_with()

    def test_a_stored_list_is_shown_and_nothing_is_queued(self):
        cache.set(esi_sync.RAIDABLE_CACHE_KEY, [ROW], 600)
        response, queued = self._page()
        self.assertFalse(response.context["fetching"])
        self.assertEqual(len(response.context["entries"]), 1)
        queued.assert_not_called()

    def test_an_empty_list_is_a_list_not_a_cold_cache(self):
        cache.set(esi_sync.RAIDABLE_CACHE_KEY, [], 600)
        response, queued = self._page()
        self.assertFalse(response.context["fetching"])
        self.assertContains(response, "Nothing raidable right now")
        queued.assert_not_called()

    def _refresh(self, side_effect=None, answer=None):
        with mock.patch.object(esi_sync, "esi") as client:
            result = client.client.Activities.GetSkyhooksRaidable.return_value.result
            result.side_effect = side_effect
            result.return_value = answer
            rows = esi_sync.refresh_raidable_skyhooks()
        return rows, result

    def test_a_304_keeps_the_previous_list_and_stores_it_again(self):
        cache.set(esi_sync.RAIDABLE_CACHE_KEY, [ROW], 5)
        rows, _result = self._refresh(side_effect=_not_modified())
        self.assertEqual(rows, [ROW])
        self.assertEqual(cache.get(esi_sync.RAIDABLE_CACHE_KEY), [ROW])
        self.assertGreater(cache.ttl(esi_sync.RAIDABLE_CACHE_KEY), 60)

    def test_nothing_stored_means_a_fetch_no_etag_can_answer_with_304(self):
        """A 304 says "what you had"; with nothing stored that is nothing."""
        rows, result = self._refresh(answer=_answer(ROW))
        self.assertEqual(rows, [ROW])
        self.assertIs(result.call_args.kwargs["force_refresh"], True)
        self.assertEqual(cache.get(esi_sync.RAIDABLE_CACHE_KEY), [ROW])

    def test_with_a_list_stored_the_etag_is_used(self):
        cache.set(esi_sync.RAIDABLE_CACHE_KEY, [], 600)
        _rows, result = self._refresh(answer=_answer(ROW))
        self.assertIs(result.call_args.kwargs["force_refresh"], False)

    def test_the_stored_list_outlives_the_refresh_schedule(self):
        """Comfortably: one failed refresh must not empty the page."""
        gap = _schedule_gap("refresh_raidable")
        self.assertGreaterEqual(HOLDFAST_RAIDABLE_CACHE_SECONDS, 2 * gap)


# --------------------------------------------------------------------------
# The broker is down
# --------------------------------------------------------------------------


class BrokerOutageTests(TestCase):
    """The rows are saved; the first sync waits for the schedule."""

    def setUp(self):
        stub_corporation_refresh(self)
        set_home(HOME)
        self.home_corp = corporation(98000710, "Home Corp", HOME)
        self.user = user_with("member", "manage_owners", "basic_access", "den_member")
        self.main = character_for(self.user, 94000710, self.home_corp, HOME)

    def test_registering_a_corporation(self):
        request = request_for(self.user, "/holdfast/owners/add/", "get")
        with (
            mock.patch.object(tasks.update_owner, "delay", side_effect=BROKER_DOWN),
            self.assertLogs(tasks.logger, level="ERROR"),
        ):
            response = inspect.unwrap(owners_views.add_owner)(
                request, Token(self.main.character_id)
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Owner.objects.filter(corporation=self.home_corp).exists())
        (level, text), = messages_of(request)
        self.assertEqual(level, "success")
        self.assertIn("next scheduled sync", text)

    def test_registering_a_den_character(self):
        request = request_for(self.user, "/holdfast/owners/add-den-character/", "get")
        with (
            mock.patch.object(tasks.update_den_character, "delay", side_effect=BROKER_DOWN),
            self.assertLogs(tasks.logger, level="ERROR"),
        ):
            response = inspect.unwrap(owners_views.add_den_character)(
                request, Token(self.main.character_id)
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            DenCharacter.objects.filter(character_ownership__character=self.main).exists()
        )
        (level, text), = messages_of(request)
        self.assertEqual(level, "success")
        self.assertIn("next scheduled sync", text)

    def test_claiming_a_slot_keeps_the_claim(self):
        """The claim used to be written after the dispatch, so an outage lost
        it while keeping the den character."""
        owner = Owner.objects.create(corporation=self.home_corp)
        skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_710, owner=owner, planet_id=40_000_710
        )
        slot = DenSlot.objects.create(skyhook=skyhook)
        request = request_for(self.user, f"/holdfast/den/slot/{slot.pk}/claim/", "get")
        with (
            mock.patch.object(tasks.update_den_character, "delay", side_effect=BROKER_DOWN),
            self.assertLogs(tasks.logger, level="ERROR"),
        ):
            response = inspect.unwrap(den_views.claim_slot)(
                request, Token(self.main.character_id), slot.pk
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DenClaim.objects.filter(slot=slot, user=self.user).exists())
        (level, text), = messages_of(request)
        self.assertEqual(level, "success")
        self.assertIn("next scheduled sync", text)

    def test_saving_the_sov_settings(self):
        """Choosing the tracked alliance queues a map refresh after the save."""
        root = user_with("root", superuser=True)
        character_for(root, 94000711, self.home_corp, HOME)
        url = "/holdfast/sov/settings/save/"
        match = resolve(url)
        form = {
            "fuel_warning_days": "7",
            "fuel_danger_days": "3",
            "fuel_critical_days": "1",
            "adm_alert_threshold": "3",
            "home_alliance_id": str(HOME),
        }
        set_home(None)
        request = request_for(root, url, "post", form)
        with (
            mock.patch.object(tasks.update_sov_map, "delay", side_effect=BROKER_DOWN),
            self.assertLogs(tasks.logger, level="ERROR"),
        ):
            response = match.func(request, *match.args, **match.kwargs)
        self.assertEqual(response.status_code, 302)
        notes = messages_of(request)
        self.assertEqual(notes[0][0], "success")
        self.assertIn("next scheduled", notes[-1][1])

    def test_linking_through_charlink(self):
        """charlink turns any exception into "Failed to add character", which
        after the rows are saved is untrue."""
        hook = _load_charlink_hook()
        request = request_for(self.user, "/charlink/login/", "get")
        with (
            mock.patch.object(tasks.update_owner, "delay", side_effect=BROKER_DOWN),
            mock.patch.object(tasks.update_den_character, "delay", side_effect=BROKER_DOWN),
            self.assertLogs(tasks.logger, level="ERROR"),
        ):
            hook._add_all(request, Token(self.main.character_id))
        self.assertTrue(Owner.objects.filter(corporation=self.home_corp).exists())
        self.assertTrue(
            DenCharacter.objects.filter(character_ownership__character=self.main).exists()
        )
        self.assertTrue(any("next scheduled sync" in text for _, text in messages_of(request)))
