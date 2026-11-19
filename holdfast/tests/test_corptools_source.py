"""Reading hubs, dens and operations from allianceauth-corptools.

corptools is not installed where these run, and must not need to be: its
tables are stood in for by the thin query functions in ``corptools_source``,
which return plain namespaces. What is under test is Holdfast's side -- the
mapping, the freshness and never-go-backwards rules, the fall-back to ESI, and
that an install without corptools pays nothing.
"""

import itertools
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.contrib.auth.models import Permission, User
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from esi.exceptions import HTTPNotModified

from ..core import den_sync, esi_sync
from ..models import (
    DenCharacter,
    MercenaryDen,
    MercenaryTacticalOperation,
    Owner,
    SovHub,
    SovHubReagent,
    SovHubUpgrade,
)
from ..thirdparty import corptools_source as cs

_ids = itertools.count(95_000_001)
SYSTEM = 30_000_501


def iso(delta):
    return (timezone.now() - delta).isoformat()


def reagents(*rows):
    return SimpleNamespace(all=lambda: list(rows))


def hub_row(hub_id=7001, system=SYSTEM, *, reagent_rows=None, upgrade_rows=None, **kw):
    fields = {
        "hub_id": hub_id,
        "solar_system_id": system,
        "fuel_access_list_id": None,
        "power_allocated": 100,
        "power_available": 300,
        "workforce_allocated": 10,
        "workforce_available": 40,
        "vuln_window_start": None,
        "vuln_window_end": None,
        "reagent_last_updated": timezone.now() - timedelta(minutes=30),
        "workforce_transport": {"state": {"x": 1}},
    }
    fields.update(kw)
    if reagent_rows is None:
        reagent_rows = [SimpleNamespace(type_name_id=81_001, amount=500, burning_per_hour=5)]
    if upgrade_rows is None:
        upgrade_rows = [SimpleNamespace(type_name_id=81_002, power_state="Online")]
    return SimpleNamespace(
        reagents=reagents(*reagent_rows), upgrades=reagents(*upgrade_rows), **fields
    )


def esi_hub(hub_id=7001, system=SYSTEM, state="Online", amount=500):
    """What the same hub looks like out of ESI."""
    return {
        "id": hub_id,
        "solar_system_id": system,
        "fuel_access_list_id": None,
        "resources": {
            "power": {"available": 300, "allocated": 100},
            "workforce": {"available": 40, "allocated": 10},
        },
        "vulnerability_window": {"start": None, "end": None},
        "reagent_bay": {
            "last_updated": None,
            "reagents": [{"type_id": 81_001, "amount": amount, "burning_per_hour": 5}],
        },
        "workforce_transport": {"state": {"x": 1}},
        "upgrades": [{"type_id": 81_002, "power_state": state}],
    }


def den_row(den_id=6001, **kw):
    fields = {
        "den_id": den_id,
        "planet_id": 40_000_501,
        "type_id": 85_230,
        "state": "running",
        "development_amount": 5,
        "development_level": "level1",
        "anarchy_amount": 7,
        "anarchy_level": "level2",
        "infomorph_amount": 3,
        "reinforcement_end": None,
        "skyhook_id": 9_001,
        "skyhook_corporation_id": 98_000_501,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def operation_row(operation_id="0b0f4f55-7c1d-4a42-a3c3-111111111111", state="available", **kw):
    fields = {
        "operation_id": operation_id,
        "mercenary_den_id": 6001,
        "dungeon_type_id": 777,
        "state": state,
        "expires": timezone.now() + timedelta(hours=3),
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


class CorptoolsStub:
    """Stands in for corptools' tables, for the length of a ``with``."""

    def __init__(self, corporation=None, hubs=(), character=None, dens=(), operations=()):
        self.values = {
            "_corporation_audit": corporation,
            "_hub_rows": list(hubs),
            "_character_audit": character,
            "_den_rows": list(dens),
            "_operation_rows": list(operations),
        }
        self.patches = []

    def __enter__(self):
        cs.reset()
        self.patches = [mock.patch.object(cs, "_models", return_value={"stub": object()})]
        self.patches += [
            mock.patch.object(cs, name, return_value=value)
            for name, value in self.values.items()
        ]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()
        cs.reset()


def audit(**stamps):
    return SimpleNamespace(update_timestamps=stamps)


def make_owner(tag):
    corporation = EveCorporationInfo.objects.create(
        corporation_id=98_500_000 + tag,
        corporation_name=f"Ct Corp {tag}",
        corporation_ticker=f"C{tag}",
        member_count=3,
    )
    return Owner.objects.create(corporation=corporation)


def make_den_character(tag):
    user = User.objects.create_user(f"ct_{tag}")
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="holdfast", codename="basic_access")
    )
    character = EveCharacter.objects.create(
        character_id=next(_ids),
        character_name=f"Ct Op {tag}",
        corporation_id=98_000_500,
        corporation_name="Ct Corp",
        corporation_ticker="CT",
    )
    ownership = CharacterOwnership.objects.create(
        user=user, character=character, owner_hash=f"ct-hash-{tag}"
    )
    return DenCharacter.objects.create(character_ownership=ownership)


# --------------------------------------------------------------------------
# Not installed
# --------------------------------------------------------------------------


class NotInstalledTests(TestCase):
    """The default install. It must cost nothing and raise nothing."""

    def setUp(self):
        cs.reset()
        self.addCleanup(cs.reset)

    def test_it_reads_as_unavailable(self):
        self.assertFalse(cs.available())

    def test_no_function_touches_the_database(self):
        owner = make_owner(1)
        den_character = make_den_character(1)
        with self.assertNumQueries(0):
            hubs = cs.hub_copies(owner)
            dens = cs.den_copies(den_character)
            operations = cs.operation_copies(den_character)
        for copies in (hubs, dens, operations):
            self.assertFalse(copies.installed)
            self.assertFalse(copies.usable)

    def test_the_stats_of_an_owner_sync_carry_no_corptools_keys(self):
        owner = make_owner(2)
        with mock.patch.object(Owner, "fetch_token") as fetch:
            stats = esi_sync.update_owner(owner, listings=False)
        fetch.assert_not_called()  # nothing is due: no token, no ESI
        self.assertNotIn("corptools", stats)
        self.assertNotIn("hubs_from_corptools", stats)

    def test_the_stats_of_a_den_sync_carry_no_corptools_keys(self):
        den_character = make_den_character(2)
        with (
            mock.patch.object(DenCharacter, "fetch_token"),
            mock.patch.object(den_sync, "sync_den_listing", return_value=0),
            mock.patch.object(den_sync, "refresh_den_details", return_value=0),
            mock.patch.object(den_sync, "sync_operations", return_value=0),
            mock.patch.object(den_sync, "sync_notifications", return_value=0),
        ):
            stats = den_sync.update_den_character(den_character)
        self.assertEqual(set(stats), {"dens", "den_details", "operations", "new_events"})


class UnexpectedCorptoolsTests(SimpleTestCase):
    """An installed corptools that is not the one we read."""

    def setUp(self):
        cs.reset()
        self.addCleanup(cs.reset)

    def test_a_missing_field_warns_once_and_reads_as_not_available(self):
        class Model:
            class _meta:
                @staticmethod
                def get_field(name):
                    from django.core.exceptions import FieldDoesNotExist

                    raise FieldDoesNotExist(name)

        with (
            mock.patch.object(cs.apps, "is_installed", return_value=True),
            mock.patch.object(cs.apps, "get_model", return_value=Model),
            mock.patch.object(cs, "logger") as log,
        ):
            self.assertFalse(cs.available())
            self.assertFalse(cs.available())
            self.assertFalse(cs.hub_copies(SimpleNamespace(corporation_id=1)).installed)
        self.assertEqual(log.warning.call_count, 1)

    def test_a_missing_model_warns_once_too(self):
        with (
            mock.patch.object(cs.apps, "is_installed", return_value=True),
            mock.patch.object(cs.apps, "get_model", side_effect=LookupError("nope")),
            mock.patch.object(cs, "logger") as log,
        ):
            cs.available()
            cs.available()
        self.assertEqual(log.warning.call_count, 1)


# --------------------------------------------------------------------------
# Hubs
# --------------------------------------------------------------------------


class HubCopyTests(TestCase):
    def setUp(self):
        self.owner = make_owner(10)
        self.now = timezone.now()

    def copies(self, **kw):
        with CorptoolsStub(**kw):
            return cs.hub_copies(self.owner, self.now)

    def test_it_maps_to_the_shape_esi_returns(self):
        copies = self.copies(
            corporation=audit(sovereignty_hubs=iso(timedelta(minutes=25))),
            hubs=[hub_row(upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="low")])],
        )
        self.assertTrue(copies.usable)
        expected = esi_hub(state="Low")
        expected["reagent_bay"]["last_updated"] = copies.rows[7001]["reagent_bay"]["last_updated"]
        self.assertEqual(copies.rows[7001], expected)

    def test_stored_the_same_way_as_a_fetch(self):
        copied = SovHub.objects.create(hub_id=7001, owner=self.owner, solar_system_id=SYSTEM)
        fetched = SovHub.objects.create(hub_id=7002, owner=self.owner, solar_system_id=SYSTEM)
        copies = self.copies(
            corporation=audit(sovereignty_hubs=iso(timedelta(minutes=30))),
            hubs=[hub_row(reagent_last_updated=None)],
        )
        memo = {81_001: None, 81_002: None}
        stamp = copies.stamp
        esi_sync._store_hub_detail(
            copied, copies.rows[7001], memo, observed_at=stamp, corptools_updated_at=stamp
        )
        esi_sync._store_hub_detail(fetched, esi_hub(7002), memo, observed_at=stamp)

        def snapshot(hub):
            hub.refresh_from_db()
            return (
                [
                    getattr(hub, name)
                    for name in (
                        "power_available", "power_allocated", "workforce_available",
                        "workforce_allocated", "vulnerability_start", "vulnerability_end",
                        "reagent_bay_updated_at", "fuel_access_list_id", "workforce_transport",
                    )
                ],
                list(SovHubReagent.objects.filter(hub=hub).values_list("type_id", "amount", "burning_per_hour")),
                list(SovHubUpgrade.objects.filter(hub=hub).values_list("type_id", "power_state")),
            )

        self.assertEqual(snapshot(copied), snapshot(fetched))
        self.assertEqual(copied.corptools_updated_at, stamp)
        self.assertIsNone(fetched.corptools_updated_at)

    def test_not_audited(self):
        copies = self.copies(corporation=None)
        self.assertTrue(copies.installed)
        self.assertFalse(copies.usable)
        self.assertEqual(copies.reason, "not audited by corptools")

    def test_no_update_recorded(self):
        self.assertEqual(self.copies(corporation=audit()).reason, "no update recorded")

    def test_too_old(self):
        copies = self.copies(corporation=audit(sovereignty_hubs=iso(timedelta(minutes=120))))
        self.assertFalse(copies.usable)
        self.assertIn("minutes old (limit 90 minutes)", copies.reason)

    def test_the_limit_itself_is_fine_and_a_minute_past_it_is_not(self):
        ok = self.copies(corporation=audit(sovereignty_hubs=iso(timedelta(minutes=89))))
        self.assertTrue(ok.usable)
        late = self.copies(corporation=audit(sovereignty_hubs=iso(timedelta(minutes=91))))
        self.assertFalse(late.usable)

    def test_a_hub_list_that_has_only_just_finished_is_not_trusted_yet(self):
        """The list time is written before the details are; wait for them."""
        early = self.copies(corporation=audit(sovereignty_hubs=iso(timedelta(minutes=5))))
        self.assertFalse(early.usable)
        self.assertIn("details may still be being written", early.reason)
        settled = self.copies(corporation=audit(sovereignty_hubs=iso(timedelta(minutes=21))))
        self.assertTrue(settled.usable)

    def test_a_time_in_the_future_is_refused_but_small_skew_is_not(self):
        future = self.copies(corporation=audit(sovereignty_hubs=iso(-timedelta(hours=1))))
        self.assertEqual(future.reason, "update time is in the future")
        # Small skew is not "in the future", but it is not settled either.
        skew = self.copies(corporation=audit(sovereignty_hubs=iso(-timedelta(minutes=2))))
        self.assertNotEqual(skew.reason, "update time is in the future")
        self.assertIn("details may still be being written", skew.reason)

    def test_a_naive_or_garbled_time(self):
        naive = (timezone.now() - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
        self.assertTrue(self.copies(corporation=audit(sovereignty_hubs=naive)).usable)
        self.assertEqual(
            self.copies(corporation=audit(sovereignty_hubs="yesterday")).reason,
            "no update recorded",
        )

    def test_one_bad_hub_is_refused_alone(self):
        copies = self.copies(
            corporation=audit(sovereignty_hubs=iso(timedelta(minutes=30))),
            hubs=[
                hub_row(7001),
                hub_row(7002, reagent_rows=[SimpleNamespace(type_name_id=None, amount=1, burning_per_hour=1)]),
                hub_row(7003, upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="Sideways")]),
            ],
        )
        self.assertTrue(copies.usable)
        self.assertEqual(set(copies.rows), {7001})
        self.assertIn("no type", copies.rejected[7002])
        self.assertIn("unknown power state 'Sideways'", copies.rejected[7003])

    def test_a_database_error_reads_as_unusable_and_warns(self):
        with (
            CorptoolsStub(corporation=audit(sovereignty_hubs=iso(timedelta(minutes=30)))),
            mock.patch.object(cs, "_hub_rows", side_effect=OperationalError("no such table")),
            mock.patch.object(cs, "logger") as log,
        ):
            copies = cs.hub_copies(self.owner, self.now)
        self.assertTrue(copies.installed)
        self.assertFalse(copies.usable)
        log.warning.assert_called_once()


class AdoptHubsTests(TestCase):
    """What ``update_owner`` does with the copies."""

    def setUp(self):
        self.owner = make_owner(20)
        self.hub = SovHub.objects.create(
            hub_id=7001,
            owner=self.owner,
            solar_system_id=SYSTEM,
            detail_updated_at=None,
            esi_read_at=timezone.now() - timedelta(hours=1),
        )
        self.fresh = audit(sovereignty_hubs=iso(timedelta(minutes=30)))
        patcher = mock.patch.object(esi_sync, "_resolve_type", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_sync(self, **kw):
        with (
            mock.patch.object(Owner, "fetch_token", return_value=object()) as token,
            mock.patch.object(esi_sync, "esi") as client,
        ):
            detail = (
                client.client.Structures.GetCorporationsStructuresSovereigntyHubsDetail
            )
            detail.return_value.result.return_value.model_dump.return_value = esi_hub(
                state="Online"
            )
            with CorptoolsStub(**kw):
                stats = esi_sync.update_owner(self.owner, listings=False)
        return stats, token, detail

    def test_a_usable_copy_means_no_esi_call_and_no_token(self):
        stats, token, detail = self.run_sync(corporation=self.fresh, hubs=[hub_row()])
        self.assertEqual(detail.call_count, 0)
        token.assert_not_called()
        self.assertEqual(stats["hubs_from_corptools"], 1)
        self.assertEqual(stats["corptools"], "ok")
        self.assertTrue(stats["complete"])
        self.hub.refresh_from_db()
        self.assertEqual(self.hub.power_available, 300)
        self.assertIsNotNone(self.hub.corptools_updated_at)

    def test_a_stale_copy_falls_back_to_esi_and_says_why(self):
        stats, token, detail = self.run_sync(
            corporation=audit(sovereignty_hubs=iso(timedelta(hours=5))), hubs=[hub_row()]
        )
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(stats["hubs_from_corptools"], 0)
        self.assertIn("old (limit", stats["corptools"])
        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.corptools_updated_at)

    def test_no_audit_falls_back(self):
        stats, _token, detail = self.run_sync(corporation=None, hubs=[])
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(stats["corptools"], "not audited by corptools")

    def test_a_hub_corptools_does_not_have_goes_to_esi(self):
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[])
        self.assertEqual(detail.call_count, 1)
        self.assertIn("1 hub(s) left to ESI", stats["corptools"])

    def test_one_refused_hub_does_not_stop_the_other(self):
        other = SovHub.objects.create(
            hub_id=7002,
            owner=self.owner,
            solar_system_id=SYSTEM,
            esi_read_at=timezone.now() - timedelta(hours=1),
        )
        stats, _token, detail = self.run_sync(
            corporation=self.fresh,
            hubs=[
                hub_row(7001),
                hub_row(7002, upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="??")]),
            ],
        )
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(stats["hubs_from_corptools"], 1)
        self.assertIn("unknown power state", stats["corptools"])
        other.refresh_from_db()
        self.hub.refresh_from_db()
        self.assertIsNotNone(self.hub.corptools_updated_at)
        self.assertIsNone(other.corptools_updated_at)

    def test_a_copy_older_than_an_esi_reading_is_not_taken(self):
        """ESI saw Online a moment ago; an older Low must not undo it."""
        SovHubUpgrade.objects.create(
            hub=self.hub, type_id=81_002, power_state="Online", low_since=None
        )
        # ESI reading 55 minutes ago: due again, but newer than the copy.
        SovHub.objects.filter(pk=self.hub.pk).update(
            detail_updated_at=timezone.now() - timedelta(minutes=55),
            esi_read_at=timezone.now() - timedelta(minutes=55),
        )
        old_copy = audit(sovereignty_hubs=iso(timedelta(minutes=70)))
        stats, _token, detail = self.run_sync(
            corporation=old_copy,
            hubs=[hub_row(upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="low")])],
        )
        self.assertEqual(detail.call_count, 1)
        self.assertIn("older than what Holdfast already holds", stats["corptools"])
        upgrade = SovHubUpgrade.objects.get(hub=self.hub)
        self.assertEqual(upgrade.power_state, "Online")
        self.assertIsNone(upgrade.low_since)

    def test_a_newer_list_stamp_over_older_details_is_not_taken_after_an_esi_read(self):
        """The stamp is the list's. ESI read the hub 55 minutes ago and holds a bay
        from 10 minutes ago; a copy whose list finished since, but whose details
        task failed, carries an older bay and must not flip the hub back."""
        held = timezone.now() - timedelta(minutes=10)
        SovHubUpgrade.objects.create(
            hub=self.hub, type_id=81_002, power_state="Online", low_since=None
        )
        SovHub.objects.filter(pk=self.hub.pk).update(
            detail_updated_at=timezone.now() - timedelta(minutes=55),
            esi_read_at=timezone.now() - timedelta(minutes=55),
            reagent_bay_updated_at=held,
        )
        stale = hub_row(
            reagent_last_updated=held - timedelta(hours=2),
            upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="low")],
        )
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[stale])
        self.assertEqual(detail.call_count, 1)
        self.assertIn("older than what Holdfast already holds", stats["corptools"])
        upgrade = SovHubUpgrade.objects.get(hub=self.hub)
        self.assertEqual(upgrade.power_state, "Online")
        self.assertIsNone(upgrade.low_since)

    def test_an_equal_bay_time_with_different_upgrade_states_is_not_taken(self):
        """CCP may leave the bay time alone when power changes; equal is no proof."""
        held = timezone.now() - timedelta(minutes=30)
        SovHubUpgrade.objects.create(
            hub=self.hub, type_id=81_002, power_state="Online", low_since=None
        )
        SovHub.objects.filter(pk=self.hub.pk).update(reagent_bay_updated_at=held)
        copy = hub_row(
            reagent_last_updated=held,
            upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="low")],
        )
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[copy])
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(stats["hubs_from_corptools"], 0)
        self.assertIn("upgrade states disagree", stats["corptools"])

    def test_an_equal_bay_time_with_the_same_upgrade_states_is_taken(self):
        held = timezone.now() - timedelta(minutes=30)
        SovHubUpgrade.objects.create(hub=self.hub, type_id=81_002, power_state="Online")
        SovHub.objects.filter(pk=self.hub.pk).update(reagent_bay_updated_at=held)
        copy = hub_row(reagent_last_updated=held)
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[copy])
        self.assertEqual(detail.call_count, 0)
        self.assertEqual(stats["hubs_from_corptools"], 1)

    def test_a_list_stamp_close_behind_an_esi_read_is_no_evidence_of_newer_details(self):
        """No detail time to compare (ESI had none): the stamp must clear the read
        by the settle period before it counts."""
        SovHub.objects.filter(pk=self.hub.pk).update(
            esi_read_at=timezone.now() - timedelta(minutes=35)
        )
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[hub_row()])
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(stats["hubs_from_corptools"], 0)

    def test_a_304_after_a_corptools_copy_is_replaced_by_a_forced_read(self):
        """Holdfast's ETag is its own: a 304 does not mean the stored copy is current."""
        SovHubUpgrade.objects.create(hub=self.hub, type_id=81_002, power_state="Low")
        SovHub.objects.filter(pk=self.hub.pk).update(
            corptools_updated_at=timezone.now() - timedelta(minutes=40)
        )
        self.hub.refresh_from_db()
        with mock.patch.object(esi_sync, "esi") as client:
            detail = (
                client.client.Structures.GetCorporationsStructuresSovereigntyHubsDetail
            )
            detail.return_value.result.return_value.model_dump.return_value = esi_hub(
                state="Online"
            )
            esi_sync._fetch_sov_hub_detail(self.owner, object(), self.hub, {81_001: None, 81_002: None})
        self.assertIs(detail.return_value.result.call_args.kwargs["force_refresh"], True)
        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.corptools_updated_at)
        self.assertEqual(SovHubUpgrade.objects.get(hub=self.hub).power_state, "Online")

    def test_a_hub_held_from_esi_keeps_the_cheap_304(self):
        with mock.patch.object(esi_sync, "esi") as client:
            detail = (
                client.client.Structures.GetCorporationsStructuresSovereigntyHubsDetail
            )
            detail.return_value.result.side_effect = HTTPNotModified(
                status_code=304, headers={}
            )
            esi_sync._fetch_sov_hub_detail(self.owner, object(), self.hub, {})
        self.assertIs(detail.return_value.result.call_args.kwargs["force_refresh"], False)
        self.hub.refresh_from_db()
        self.assertIsNotNone(self.hub.detail_updated_at)

    def test_a_304_on_a_corptools_copy_is_not_dated_as_an_esi_read(self):
        """Defensive: unreachable with force_refresh, but must not mislabel the copy."""
        before = timezone.now() - timedelta(hours=1)
        SovHub.objects.filter(pk=self.hub.pk).update(
            corptools_updated_at=timezone.now() - timedelta(minutes=40), esi_read_at=before
        )
        self.hub.refresh_from_db()
        with (
            mock.patch.object(esi_sync, "esi") as client,
            mock.patch.object(esi_sync.logger, "warning") as warning,
        ):
            detail = (
                client.client.Structures.GetCorporationsStructuresSovereigntyHubsDetail
            )
            detail.return_value.result.side_effect = HTTPNotModified(
                status_code=304, headers={}
            )
            esi_sync._fetch_sov_hub_detail(self.owner, object(), self.hub, {})
        warning.assert_called_once()
        self.hub.refresh_from_db()
        self.assertEqual(self.hub.esi_read_at, before)
        self.assertIsNotNone(self.hub.detail_updated_at)

    def test_a_copy_that_looks_half_written_is_not_taken(self):
        SovHubReagent.objects.create(hub=self.hub, type_id=81_001, amount=1, burning_per_hour=1)
        stats, _token, detail = self.run_sync(
            corporation=self.fresh, hubs=[hub_row(reagent_rows=[], upgrade_rows=[])]
        )
        self.assertEqual(detail.call_count, 1)
        self.assertIn("mid-write", stats["corptools"])
        self.assertEqual(self.hub.reagents.count(), 1)

    def test_a_copy_with_only_one_list_empty_is_not_taken(self):
        """corptools rewrites reagents, then upgrades; either can be caught alone."""
        SovHubReagent.objects.create(hub=self.hub, type_id=81_001, amount=1, burning_per_hour=1)
        SovHubUpgrade.objects.create(hub=self.hub, type_id=81_002, power_state="Online")
        stats, _token, detail = self.run_sync(
            corporation=self.fresh, hubs=[hub_row(reagent_rows=[])]
        )
        self.assertEqual(detail.call_count, 1)
        self.assertIn("no reagents", stats["corptools"])
        self.assertEqual(self.hub.reagents.count(), 1)
        # The ESI fallback above rewrote the hub; put the starting state back.
        self.hub.reagents.all().delete()
        self.hub.upgrades.all().delete()
        SovHubReagent.objects.create(hub=self.hub, type_id=81_001, amount=1, burning_per_hour=1)
        SovHubUpgrade.objects.create(hub=self.hub, type_id=81_002, power_state="Online")
        SovHub.objects.filter(pk=self.hub.pk).update(
            detail_updated_at=None, esi_read_at=timezone.now() - timedelta(hours=1)
        )
        stats, _token, detail = self.run_sync(
            corporation=self.fresh, hubs=[hub_row(upgrade_rows=[])]
        )
        self.assertEqual(detail.call_count, 1)
        self.assertIn("no upgrades", stats["corptools"])
        self.assertEqual(self.hub.upgrades.count(), 1)

    def test_a_fresh_list_time_over_old_details_still_goes_to_esi(self):
        """corptools' time is the list's: if ESI has not read the hub in 3 hours, ask it."""
        SovHub.objects.filter(pk=self.hub.pk).update(
            esi_read_at=timezone.now() - timedelta(hours=4)
        )
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[hub_row()])
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(stats["hubs_from_corptools"], 0)
        self.assertIn("ESI has not read it recently", stats["corptools"])
        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.corptools_updated_at)
        self.assertGreater(self.hub.esi_read_at, timezone.now() - timedelta(minutes=1))

    def test_a_hub_never_read_from_esi_goes_to_esi_first(self):
        SovHub.objects.filter(pk=self.hub.pk).update(esi_read_at=None)
        stats, _token, detail = self.run_sync(corporation=self.fresh, hubs=[hub_row()])
        self.assertEqual(detail.call_count, 1)
        self.assertIn("ESI has not read it recently", stats["corptools"])

    def test_a_corptools_copy_does_not_restart_the_esi_clock(self):
        before = self.hub.esi_read_at
        self.run_sync(corporation=self.fresh, hubs=[hub_row()])
        self.hub.refresh_from_db()
        self.assertEqual(self.hub.esi_read_at, before)

    def test_a_hub_in_another_system_is_not_taken(self):
        stats, _token, detail = self.run_sync(
            corporation=self.fresh, hubs=[hub_row(system=SYSTEM + 1)]
        )
        self.assertEqual(detail.call_count, 1)
        self.assertIn("another system", stats["corptools"])

    def test_an_esi_reading_clears_the_corptools_stamp(self):
        SovHub.objects.filter(pk=self.hub.pk).update(
            corptools_updated_at=timezone.now() - timedelta(hours=2)
        )
        esi_sync._store_hub_detail(
            self.hub, esi_hub(), {81_001: None, 81_002: None}, observed_at=timezone.now()
        )
        self.hub.refresh_from_db()
        self.assertIsNone(self.hub.corptools_updated_at)

    def test_a_copy_under_an_unchanged_time_is_still_re_read(self):
        """The time is the list's; details written after it must not be missed."""
        self.run_sync(corporation=self.fresh, hubs=[hub_row()])
        SovHub.objects.filter(pk=self.hub.pk).update(
            detail_updated_at=timezone.now() - timedelta(minutes=70)
        )
        stats, _token, detail = self.run_sync(
            corporation=self.fresh, hubs=[hub_row(upgrade_rows=[SimpleNamespace(type_name_id=81_002, power_state="low")])]
        )
        self.assertEqual(detail.call_count, 0)
        self.assertEqual(stats["hubs_from_corptools"], 1)
        self.assertEqual(SovHubUpgrade.objects.get(hub=self.hub).power_state, "Low")


# --------------------------------------------------------------------------
# Dens and operations
# --------------------------------------------------------------------------


class DenSyncTests(TestCase):
    def setUp(self):
        self.den_character = make_den_character(30)
        self.character_audit = audit(
            mercenary_dens=iso(timedelta(minutes=20)),
            mercenary_tactical_operations=iso(timedelta(minutes=10)),
        )
        for patcher in (
            mock.patch.object(DenCharacter, "fetch_token", return_value=object()),
            mock.patch.object(den_sync, "_resolve_planet", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_sync(self, **kw):
        with mock.patch.object(den_sync, "esi") as client:
            with CorptoolsStub(character=kw.pop("character", self.character_audit), **kw):
                stats = den_sync.update_den_character(self.den_character)
        return stats, client

    def test_dens_and_operations_come_from_corptools_and_only_notifications_from_esi(self):
        stats, client = self.run_sync(dens=[den_row()], operations=[operation_row()])
        self.assertEqual(client.client.Structures.mock_calls, [])
        self.assertEqual(client.client.Activities.mock_calls, [])
        self.assertEqual(
            client.client.Character.GetCharactersCharacterIdNotifications.call_count, 1
        )
        self.assertEqual(stats["dens_from"], "corptools")
        self.assertEqual(stats["operations_from"], "corptools")
        self.assertEqual(stats["dens"], 1)
        self.assertEqual(stats["operations"], 1)
        self.assertEqual(stats["den_details"], 0)

    def test_a_den_is_stored_as_a_fetch_would_store_it(self):
        self.run_sync(dens=[den_row(reinforcement_end=timezone.now() + timedelta(hours=1))])
        den = MercenaryDen.objects.get(den_id=6001)
        self.assertEqual(den.den_character, self.den_character)
        self.assertEqual(den.eve_character, self.den_character.character)
        self.assertEqual(
            (den.type_id, den.state, den.development_level, den.development_amount,
             den.anarchy_level, den.anarchy_amount, den.infomorphs, den.skyhook_id,
             den.skyhook_corporation_id, den.planet_id),
            (85_230, "Running", "Level1", 5, "Level2", 7, 3, 9_001, 98_000_501, 40_000_501),
        )
        self.assertIsNotNone(den.reinforce_end)
        self.assertIsNotNone(den.detail_updated_at)

    def test_a_den_that_is_gone_from_the_copy_is_removed(self):
        MercenaryDen.objects.create(
            den_id=6999,
            den_character=self.den_character,
            planet_id=1,
            detail_updated_at=timezone.now() - timedelta(hours=3),
            last_seen_at=timezone.now() - timedelta(hours=3),
        )
        self.run_sync(dens=[den_row()])
        self.assertEqual(
            list(MercenaryDen.objects.values_list("den_id", flat=True)), [6001]
        )

    def test_an_unknown_state_sends_the_dens_to_esi(self):
        with mock.patch.object(den_sync, "sync_den_listing", return_value=0) as listing, \
                mock.patch.object(den_sync, "refresh_den_details", return_value=0):
            stats, _client = self.run_sync(dens=[den_row(state="melting")], operations=[])
        listing.assert_called_once()
        self.assertEqual(stats["dens_from"], "esi")
        self.assertIn("unknown state 'melting'", stats["corptools"])
        self.assertEqual(stats["operations_from"], "corptools")

    def test_a_stale_copy_goes_to_esi(self):
        stale = audit(
            mercenary_dens=iso(timedelta(hours=4)),
            mercenary_tactical_operations=iso(timedelta(hours=4)),
        )
        with (
            mock.patch.object(den_sync, "sync_den_listing", return_value=0) as listing,
            mock.patch.object(den_sync, "refresh_den_details", return_value=0),
            mock.patch.object(den_sync, "sync_operations", return_value=0) as operations,
        ):
            stats, _client = self.run_sync(character=stale, dens=[den_row()])
        listing.assert_called_once()
        operations.assert_called_once()
        self.assertEqual((stats["dens_from"], stats["operations_from"]), ("esi", "esi"))

    def test_not_audited_goes_to_esi(self):
        with (
            mock.patch.object(den_sync, "sync_den_listing", return_value=0),
            mock.patch.object(den_sync, "refresh_den_details", return_value=0),
            mock.patch.object(den_sync, "sync_operations", return_value=0),
        ):
            stats, _client = self.run_sync(character=None)
        self.assertEqual(stats["corptools"], "dens: not audited by corptools; operations: not audited by corptools")

    def test_a_copy_older_than_what_we_hold_does_not_flip_the_den_back(self):
        """ESI saw the den Paused a moment ago; the older copy says Running."""
        MercenaryDen.objects.create(
            den_id=6001,
            den_character=self.den_character,
            planet_id=40_000_501,
            state="Paused",
            detail_updated_at=timezone.now() - timedelta(minutes=2),
            last_seen_at=timezone.now() - timedelta(minutes=2),
        )
        with (
            mock.patch.object(den_sync, "sync_den_listing", return_value=1) as listing,
            mock.patch.object(den_sync, "refresh_den_details", return_value=0),
        ):
            stats, _client = self.run_sync(dens=[den_row(state="running")], operations=[])
        listing.assert_called_once()
        self.assertEqual(stats["dens_from"], "esi")
        self.assertEqual(MercenaryDen.objects.get(den_id=6001).state, "Paused")

    def test_an_empty_copy_while_we_hold_dens_goes_to_esi(self):
        MercenaryDen.objects.create(
            den_id=6001,
            den_character=self.den_character,
            planet_id=1,
            detail_updated_at=timezone.now() - timedelta(hours=3),
            last_seen_at=timezone.now() - timedelta(hours=3),
        )
        with (
            mock.patch.object(den_sync, "sync_den_listing", return_value=1) as listing,
            mock.patch.object(den_sync, "refresh_den_details", return_value=0),
        ):
            stats, _client = self.run_sync(dens=[], operations=[])
        listing.assert_called_once()
        self.assertIn("mid-write", stats["corptools"])

    def test_the_same_copy_twice_writes_nothing(self):
        self.run_sync(dens=[den_row()], operations=[operation_row()])
        with CaptureQueriesContext(connection) as queries:
            stats, _client = self.run_sync(dens=[den_row()], operations=[operation_row()])
        self.assertEqual(stats["dens"], 1)
        self.assertEqual(stats["operations"], 1)
        writes = [
            q["sql"]
            for q in queries
            if q["sql"].split(None, 1)[0] in ("INSERT", "UPDATE", "DELETE")
            and "mercenary" in q["sql"]
        ]
        self.assertEqual(writes, [])

    def test_a_database_error_goes_to_esi(self):
        with (
            mock.patch.object(den_sync, "sync_den_listing", return_value=0) as listing,
            mock.patch.object(den_sync, "refresh_den_details", return_value=0),
            mock.patch.object(den_sync, "sync_operations", return_value=0),
            CorptoolsStub(character=self.character_audit),
            mock.patch.object(cs, "_den_rows", side_effect=OperationalError("gone")),
            mock.patch.object(cs, "logger"),
            mock.patch.object(den_sync, "esi"),
        ):
            stats = den_sync.update_den_character(self.den_character)
        listing.assert_called_once()
        self.assertEqual(stats["dens_from"], "esi")


class OperationIdTests(TestCase):
    """The id is the primary key; the alert log and dismissals hang off it."""

    def setUp(self):
        self.den_character = make_den_character(40)
        self.character_audit = audit(
            mercenary_dens=iso(timedelta(minutes=20)),
            mercenary_tactical_operations=iso(timedelta(minutes=10)),
        )
        patcher = mock.patch.object(DenCharacter, "fetch_token", return_value=object())
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, *operations):
        with (
            mock.patch.object(den_sync, "esi"),
            mock.patch.object(den_sync, "_resolve_planet", return_value=None),
            CorptoolsStub(
                character=self.character_audit, dens=[den_row()], operations=list(operations)
            ),
        ):
            return den_sync.update_den_character(self.den_character)

    def test_another_spelling_of_the_same_uuid_keeps_its_row(self):
        held = "0B0F4F557C1D4A42A3C3111111111111"
        MercenaryTacticalOperation.objects.create(
            operation_id=held,
            den_character=self.den_character,
            mercenary_den_id=6001,
            state="Available",
            dismissed_at=timezone.now(),
            updated_at=timezone.now() - timedelta(hours=1),
        )
        self.sync(operation_row("0b0f4f55-7c1d-4a42-a3c3-111111111111", state="started"))
        row = MercenaryTacticalOperation.objects.get()
        self.assertEqual(row.operation_id, held)
        self.assertEqual(row.state, "Started")
        self.assertIsNotNone(row.dismissed_at)

    def test_a_new_operation_takes_corptools_spelling(self):
        self.sync(operation_row())
        self.assertEqual(
            MercenaryTacticalOperation.objects.get().operation_id,
            "0b0f4f55-7c1d-4a42-a3c3-111111111111",
        )

    def test_an_operation_that_ended_is_removed(self):
        MercenaryTacticalOperation.objects.create(
            operation_id="gone",
            den_character=self.den_character,
            mercenary_den_id=6001,
            updated_at=timezone.now() - timedelta(hours=1),
        )
        self.sync(operation_row())
        self.assertEqual(MercenaryTacticalOperation.objects.count(), 1)

    def test_normalising(self):
        self.assertEqual(
            cs.normalise_operation_id("0B0F4F557C1D4A42A3C3111111111111"),
            cs.normalise_operation_id("0b0f4f55-7c1d-4a42-a3c3-111111111111"),
        )
        self.assertEqual(cs.normalise_operation_id("Not-A-UUID"), "not-a-uuid")
