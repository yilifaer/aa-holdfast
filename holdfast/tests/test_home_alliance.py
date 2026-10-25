"""The tracked alliance, and everything that follows from an owner being active.

On a production install a member registered a Goonswarm corporation with a
Goon alt. Nothing refused it: the alliances this app tracked were read off the
registered corporations, so Goonswarm became tracked, its whole sovereignty
map came down, 104 low-ADM systems landed on the dashboard and the ADM check
posted one Discord message per system.

The fix is a tracked alliance the install actually knows about, a registration
guard on both paths in front of it, and one definition of "active owner" that
every page, badge and alert goes through. These tests pin each of those down,
mostly on the shape that hid the bug: a second alliance.
"""

import importlib
import inspect
import sys
import types
from datetime import timedelta
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import (
    EveAllianceInfo,
    EveCharacter,
    EveCorporationInfo,
)
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve
from django.utils import timezone
from esi.exceptions import HTTPNotModified

from .. import tasks
from ..core import alerts, attention, den_alerts, den_sync, esi_sync, scope
from ..models import (
    AlertLog,
    DenCharacter,
    DenSlot,
    EvolutionLevel,
    HoldfastConfig,
    MercenaryDen,
    Owner,
    PowerState,
    Skyhook,
    SovHub,
    SovHubUpgrade,
    SovSystem,
    SystemCostIndex,
    Webhook,
)
from ..views import den as den_views
from ..views import owners as owners_views
from ..views import skyhook as skyhook_views
from ..views import sov as sov_views
from ..views.common import (
    visible_alliance_ids,
    visible_hubs,
    visible_owners,
    visible_slots,
)

HOME, FRIEND, FOREIGN = 99000100, 99000101, 99000102
NAMES = {HOME: "Home Alliance", FRIEND: "Friend Alliance", FOREIGN: "Foreign Alliance"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def alliance(alliance_id):
    info, _ = EveAllianceInfo.objects.get_or_create(
        alliance_id=alliance_id,
        defaults={
            "alliance_name": NAMES[alliance_id],
            "alliance_ticker": NAMES[alliance_id][:4].upper(),
            "executor_corp_id": alliance_id - 1000000,
        },
    )
    return info


def corporation(corporation_id, name, alliance_id=None):
    return EveCorporationInfo.objects.create(
        corporation_id=corporation_id,
        corporation_name=name,
        corporation_ticker=name[:4].upper(),
        member_count=5,
        alliance=alliance(alliance_id) if alliance_id else None,
    )


def owner(corporation_id, name, alliance_id=None, enabled=True):
    return Owner.objects.create(
        corporation=corporation(corporation_id, name, alliance_id), is_enabled=enabled
    )


def set_home(alliance_id):
    config = HoldfastConfig.get_solo()
    config.home_alliance_id = alliance_id
    config.save()


def character_for(user, character_id, corporation, alliance_id=None, main=True):
    affiliation = (
        {"alliance_id": alliance_id, "alliance_name": NAMES[alliance_id]}
        if alliance_id
        else {}
    )
    character = EveCharacter.objects.create(
        character_id=character_id,
        character_name=f"Pilot {character_id}",
        corporation_id=corporation.corporation_id,
        corporation_name=corporation.corporation_name,
        corporation_ticker=corporation.corporation_ticker,
        **affiliation,
    )
    CharacterOwnership.objects.create(
        user=user, character=character, owner_hash=f"hash-{character_id}"
    )
    if main:
        profile = user.profile
        profile.main_character = character
        profile.save()
    return character


def user_with(username, *codenames, superuser=False):
    if superuser:
        return User.objects.create_superuser(username, f"{username}@example.invalid", "x")
    user = User.objects.create_user(username)
    if codenames:
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="holdfast", codename__in=codenames
            )
        )
    return User.objects.get(pk=user.pk)  # fresh: no permission cache


def request_for(user, url="/", method="post", data=None):
    request = getattr(RequestFactory(), method)(url, data or {})
    request.user = user
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    return request


def messages_of(request):
    return [(m.level_tag, m.message) for m in get_messages(request)]


class Token:
    """All either registration path reads off a token."""

    def __init__(self, character_id):
        self.character_id = character_id


def extras(*alliance_ids):
    return mock.patch.object(scope, "HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS", list(alliance_ids))


def stub_corporation_refresh(test):
    """Every registration refreshes the corporation from ESI before judging
    it. Stubbed to a no-op for the test's lifetime -- the row is left exactly
    as the fixture made it -- and handed back so a test can count the calls."""
    patcher = mock.patch.object(EveCorporationInfo, "update_corporation", autospec=True)
    test.addCleanup(patcher.stop)
    return patcher.start()


# --------------------------------------------------------------------------
# The tracked set
# --------------------------------------------------------------------------


class TrackedSetTests(TestCase):
    def test_nothing_is_tracked_on_a_fresh_install_and_nothing_is_created(self):
        """Reading the tracked set must not create the config row: the
        empty-install tests assert the row does not exist until asked for."""
        self.assertEqual(scope.tracked_alliance_ids(), set())
        self.assertIsNone(scope.home_alliance_name())
        self.assertEqual(HoldfastConfig.objects.count(), 0)

    def test_home_and_the_extras_from_local_py_together(self):
        set_home(HOME)
        with extras(FRIEND):
            self.assertEqual(scope.tracked_alliance_ids(), {HOME, FRIEND})
        self.assertEqual(scope.tracked_alliance_ids(), {HOME})

    def test_extras_alone_are_tracked_with_no_home(self):
        with extras(FRIEND):
            self.assertEqual(scope.tracked_alliance_ids(), {FRIEND})

    def test_the_name_comes_from_auth_when_it_knows_the_alliance(self):
        set_home(HOME)
        self.assertEqual(scope.home_alliance_name(), str(HOME))
        alliance(HOME)
        self.assertEqual(scope.home_alliance_name(), "Home Alliance")

    def test_it_is_no_longer_derived_from_the_owners(self):
        """The whole bug: an owner in another alliance made it tracked."""
        set_home(HOME)
        owner(98000201, "Foreign Holdings", FOREIGN)
        self.assertEqual(scope.tracked_alliance_ids(), {HOME})

    def test_an_extra_that_is_not_a_number_is_skipped_not_fatal(self):
        """Before 0.1.9 the list was used uncast, so a ticker or a None in
        it was harmless. It must stay harmless: this is read on every page."""
        set_home(HOME)
        with extras("FRND", None, str(FRIEND)), self.assertLogs(
            scope.logger, "WARNING"
        ) as logged:
            self.assertEqual(scope.tracked_alliance_ids(), {HOME, FRIEND})
        self.assertEqual(len(logged.records), 2)


# --------------------------------------------------------------------------
# Active owners
# --------------------------------------------------------------------------


class ActiveOwnerTests(TestCase):
    def setUp(self):
        set_home(HOME)
        self.ours = owner(98000210, "Ours", HOME)
        self.off = owner(98000211, "Ours Off", HOME, enabled=False)
        self.foreign = owner(98000212, "Foreign", FOREIGN)
        self.holding = owner(98000213, "Holding")

    def test_active_is_enabled_and_in_scope(self):
        self.assertEqual(set(Owner.objects.active()), {self.ours, self.holding})

    def test_an_alliance_less_corporation_is_in_scope_on_any_install(self):
        set_home(None)
        self.assertIn(self.holding, Owner.objects.in_scope())
        self.assertNotIn(self.ours, Owner.objects.in_scope())

    def test_re_enabling_needs_no_resync(self):
        self.off.is_enabled = True
        self.off.save()
        self.assertIn(self.off, Owner.objects.active())

    def test_extras_count_as_in_scope(self):
        friend = owner(98000214, "Friend", FRIEND)
        with extras(FRIEND):
            self.assertIn(friend, Owner.objects.active())
        self.assertNotIn(friend, Owner.objects.active())

    def test_the_reason_an_owner_is_skipped_says_what_to_do(self):
        alliance(HOME)
        self.assertIsNone(scope.inactive_reason(self.ours))
        self.assertEqual(scope.inactive_reason(self.off), "Disabled on the Tokens page.")
        reason = scope.inactive_reason(self.foreign)
        for expected in (
            "Foreign Alliance",
            str(FOREIGN),
            "Home Alliance",
            "not synced",
            "SOV settings page",
            "HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS",
        ):
            self.assertIn(expected, reason)

    def test_with_no_tracked_alliance_the_reason_says_to_set_one(self):
        set_home(None)
        self.assertIn("No tracked alliance is set", scope.inactive_reason(self.ours))
        self.assertNotIn("None", scope.inactive_reason(self.ours))

    def test_the_fan_out_queues_only_active_owners(self):
        queued = []
        with mock.patch.object(
            tasks.update_owner, "apply_async", side_effect=lambda args, **_: queued.append(args[0])
        ):
            tasks.update_all_owners()
        self.assertEqual(set(queued), {self.ours.pk, self.holding.pk})

    def test_a_queued_sync_for_an_inactive_owner_records_why_and_calls_no_esi(self):
        with mock.patch.object(esi_sync, "update_owner") as sync:
            self.assertIsNone(tasks.update_owner(self.foreign.pk))
        sync.assert_not_called()
        self.foreign.refresh_from_db()
        self.assertFalse(self.foreign.last_sync_ok)
        self.assertIn("outside the tracked alliance", self.foreign.last_error)


# --------------------------------------------------------------------------
# What people see
# --------------------------------------------------------------------------


class VisibilityTests(TestCase):
    def setUp(self):
        set_home(HOME)
        self.ours = owner(98000220, "Ours", HOME)
        self.off = owner(98000221, "Ours Off", HOME, enabled=False)
        self.foreign = owner(98000222, "Foreign", FOREIGN)
        self.holding = owner(98000223, "Holding")
        self.hubs = {}
        for index, an_owner in enumerate((self.ours, self.off, self.foreign, self.holding)):
            self.hubs[an_owner] = SovHub.objects.create(
                hub_id=6_000_000 + index,
                owner=an_owner,
                solar_system_id=30001000 + index,
                fuel_expires_at=timezone.now() + timedelta(hours=12),
            )
        self.root = user_with("root", superuser=True)
        character_for(self.root, 93000001, self.ours.corporation, HOME)
        self.member = user_with("member", "sov_officer", "den_officer")
        character_for(self.member, 93000002, self.ours.corporation, HOME)

    def test_a_superuser_sees_exactly_the_active_owners(self):
        self.assertEqual(set(visible_owners(self.root)), {self.ours, self.holding})
        self.assertEqual(
            set(visible_hubs(self.root)), {self.hubs[self.ours], self.hubs[self.holding]}
        )

    def test_a_superuser_s_alliances_are_the_tracked_set(self):
        with extras(FRIEND):
            self.assertEqual(visible_alliance_ids(self.root), {HOME, FRIEND})

    def test_a_home_member_sees_an_alliance_less_owner_s_hub(self):
        self.assertIn(self.hubs[self.holding], visible_hubs(self.member))
        self.assertNotIn(self.hubs[self.foreign], visible_hubs(self.member))
        self.assertEqual(visible_alliance_ids(self.member), {HOME})

    def test_an_outsider_sees_nothing(self):
        outsider = user_with("outsider", "sov_officer")
        character_for(outsider, 93000003, self.foreign.corporation, FOREIGN)
        self.assertEqual(list(visible_owners(outsider)), [])
        self.assertEqual(visible_alliance_ids(outsider), set())

    def test_a_main_with_no_alliance_sees_its_own_corporation(self):
        solo = user_with("solo", "sov_officer")
        character_for(solo, 93000004, self.holding.corporation, None)
        self.assertEqual(set(visible_owners(solo)), {self.holding})

    def test_the_sov_badge_ignores_a_foreign_owner_s_hub(self):
        """Twelve hours of fuel on our hub and on the holding corp's; the
        foreign and the disabled ones sit at the same twelve hours."""
        attention.invalidate(self.root)
        self.assertEqual(attention.sov_count(self.root), 2)

    def test_a_disabled_owner_s_den_slot_goes_and_comes_back(self):
        skyhook = Skyhook.objects.create(
            skyhook_id=7_000_001, owner=self.off, planet_id=40_000_101
        )
        slot = DenSlot.objects.create(skyhook=skyhook)
        self.assertNotIn(slot, visible_slots(self.root))
        self.off.is_enabled = True
        self.off.save()
        self.assertIn(slot, visible_slots(self.root))
        self.assertTrue(DenSlot.objects.filter(pk=slot.pk).exists(), "never deleted")

    def test_the_two_stray_skyhook_reads_go_through_the_scoped_queryset(self):
        """The den-siphon note on the SOV dashboard and the "ours" flag on the
        raid list read every skyhook regardless of owner."""
        self.assertIn("visible_skyhooks(user)", inspect.getsource(sov_views.dashboard))
        self.assertIn(
            "visible_skyhooks(request.user)", inspect.getsource(skyhook_views.raid_targets)
        )
        for module in (sov_views, skyhook_views):
            self.assertNotIn("Skyhook.objects", inspect.getsource(module))

    def test_den_slot_sync_is_left_alone(self):
        """Narrowing the create would put every inactive owner's slot into the
        stale delete, and that cascades its claim history. Scope is applied
        where slots are read, never where they are made."""
        from ..core import den_sync

        source = inspect.getsource(den_sync.sync_den_slots)
        self.assertNotIn("active()", source)
        self.assertNotIn("visible_", source)


class DenGroundTests(TestCase):
    """A slot is made for every temperate skyhook on the install, and a den
    is linked to its slot by structure id alone. So a member's den on a
    disabled or foreign owner's skyhook has a slot -- and used to count as
    sitting on our ground, eating our workforce, on that member's own pages.
    Whose ground it is gets decided where the slot is read, never where it is
    made: narrowing the sync would cascade claim history."""

    def setUp(self):
        set_home(HOME)
        self.landlord = owner(98000230, "Landlord", FOREIGN)
        skyhook = Skyhook.objects.create(
            skyhook_id=7_000_230, owner=self.landlord, planet_id=40_000_230
        )
        self.slot = DenSlot.objects.create(skyhook=skyhook)
        self.member = user_with("member", "basic_access")
        ours = corporation(98000231, "Ours", HOME)
        character = character_for(self.member, 93000230, ours, HOME)
        operator = DenCharacter.objects.create(
            character_ownership=CharacterOwnership.objects.get(character=character)
        )
        self.den = MercenaryDen.objects.create(
            den_id=8_000_230,
            den_character=operator,
            planet_id=40_000_230,
            skyhook_id=skyhook.skyhook_id,
            anarchy_level=EvolutionLevel.L2,
            state=MercenaryDen.State.RUNNING,
        )
        den_sync.link_den_to_slot(self.den)
        self.den.refresh_from_db()
        self.assertEqual(self.den.slot, self.slot, "linked by structure id, as before")

    def _badge(self):
        attention.invalidate(self.member)
        return attention.den_count(self.member)

    def test_a_foreign_owner_s_skyhook_is_not_our_ground(self):
        self.assertFalse(self.den.is_on_our_ground)
        self.assertEqual(self._badge(), 0)

    def test_it_becomes_ours_when_the_owner_is_active(self):
        set_home(FOREIGN)
        self.assertTrue(self.den.is_on_our_ground)
        self.assertEqual(self._badge(), 1)

    def test_a_disabled_owner_s_ground_is_not_ours_either(self):
        set_home(FOREIGN)
        self.landlord.is_enabled = False
        self.landlord.save()
        self.assertFalse(self.den.is_on_our_ground)
        self.assertEqual(self._badge(), 0)

    def test_the_dashboard_reads_the_slot_through_the_same_rule(self):
        source = inspect.getsource(den_views.dashboard)
        self.assertIn("is_on_our_ground", source)
        self.assertNotIn("if den.slot", source)


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


class AlertScopeTests(TestCase):
    def setUp(self):
        set_home(HOME)
        self.ours = owner(98000230, "Ours", HOME)
        self.off = owner(98000231, "Ours Off", HOME, enabled=False)
        self.foreign = owner(98000232, "Foreign", FOREIGN)
        config = HoldfastConfig.get_solo()
        config.workforce_drop_grace_hours = 1
        config.save()
        Webhook.objects.create(
            name="test", url="https://discord.com/api/webhooks/1/x", is_enabled=True
        )
        AlertLog.objects.all().delete()

    def _run(self, check):
        """One pass with delivery stubbed to succeed. den_alerts imports the
        sender by name, so both modules are patched."""
        titles = []

        def send(embed, category=None):
            titles.append(embed.title)
            return True

        with (
            mock.patch.object(alerts, "_send", send),
            mock.patch.object(den_alerts, "_send", send),
        ):
            check()
        return titles

    def _hub(self, an_owner, index, **fields):
        return SovHub.objects.create(
            hub_id=8_000_000 + index,
            owner=an_owner,
            solar_system_id=30002000 + index,
            **fields,
        )

    def _skyhook(self, an_owner, index, **fields):
        return Skyhook.objects.create(
            skyhook_id=9_000_000 + index, owner=an_owner, planet_id=40_002_000 + index, **fields
        )

    def test_a_foreign_owner_s_hub_produces_no_fuel_alert(self):
        soon = timezone.now() + timedelta(hours=12)
        self._hub(self.foreign, 1, fuel_expires_at=soon)
        self._hub(self.off, 2, fuel_expires_at=soon)
        self._hub(self.ours, 3, fuel_expires_at=soon)
        titles = self._run(alerts.check_hub_fuel)
        self.assertEqual(len(titles), 1)

    def test_a_disabled_owner_s_starved_upgrade_is_disarmed_then_fires_on_enable(self):
        hub = self._hub(self.off, 4)
        SovHubUpgrade.objects.create(hub=hub, type_id=1, power_state=PowerState.LOW)
        alerts._mark_sent(f"upglow:{hub.hub_id}")
        self.assertEqual(self._run(alerts.check_hub_upgrades), [])
        self.assertFalse(AlertLog.objects.filter(key=f"upglow:{hub.hub_id}").exists())
        self.off.is_enabled = True
        self.off.save()
        self.assertEqual(len(self._run(alerts.check_hub_upgrades)), 1)

    def test_a_foreign_reinforced_skyhook_is_quiet_and_its_key_goes(self):
        skyhook = self._skyhook(self.foreign, 1, state="ArmorReinforced")
        alerts._mark_sent(f"reinf:{skyhook.skyhook_id}:ArmorReinforced")
        self.assertEqual(self._run(alerts.check_skyhook_attacks), [])
        self.assertFalse(AlertLog.objects.filter(key__startswith="reinf:").exists())

    def test_the_adm_check_reads_only_tracked_systems(self):
        SovSystem.objects.create(
            solar_system_id=30003001, alliance_id=FOREIGN, activity_defense_multiplier=1.0
        )
        SovSystem.objects.create(
            solar_system_id=30003002, alliance_id=HOME, activity_defense_multiplier=1.0
        )
        alerts._mark_sent("adm:30003001")
        titles = self._run(alerts.check_adm)
        self.assertEqual(len(titles), 1)
        self.assertFalse(AlertLog.objects.filter(key="adm:30003001").exists())

    def _campaign(self, structure_id):
        from ..models import SovCampaign

        return SovCampaign.objects.create(
            campaign_id=structure_id,
            structure_id=structure_id,
            solar_system_id=30002000,
            event_type=SovCampaign.EventType.IHUB,
            start_time=timezone.now(),
        )

    def test_hub_reinforced_knows_only_active_owners_hubs(self):
        self._hub(self.foreign, 5)
        self._hub(self.ours, 6)
        for hub_id in (8_000_005, 8_000_006):
            self._campaign(hub_id)
        self.assertEqual(len(self._run(alerts.check_hub_reinforced)), 1)

    def test_hub_reinforced_is_silent_when_no_hub_is_active(self):
        """No active hubs means nothing is ours -- not everything. The old
        fallback alerted on every IHUB campaign once the set came up empty,
        which the Disable button made a one-click state to reach."""
        self._hub(self.off, 7)
        self._campaign(8_000_007)
        self.assertEqual(self._run(alerts.check_hub_reinforced), [])
        self.ours.delete()
        self.foreign.delete()
        self.off.delete()
        self._campaign(8_000_008)
        self.assertEqual(self._run(alerts.check_hub_reinforced), [])
        self.assertFalse(AlertLog.objects.filter(key__startswith="sovreinf:").exists())

    def test_workforce_siphon_ignores_inactive_owners_and_clears_their_keys(self):
        long_ago = timezone.now() - timedelta(hours=5)
        for an_owner, index in ((self.foreign, 2), (self.off, 3), (self.ours, 4)):
            self._skyhook(
                an_owner,
                index,
                effective_workforce=8000,
                workforce_high_water=9000,
                workforce_dropped_at=long_ago,
            )
        alerts._mark_sent("siphon:9000002")
        alerts._mark_sent("siphon:9000003")
        titles = self._run(den_alerts.check_workforce_siphon)
        self.assertEqual(len(titles), 1)
        self.assertEqual(
            set(AlertLog.objects.values_list("key", flat=True)), {"siphon:9000004"}
        )

    def test_the_fingerprint_check_does_the_same(self):
        for an_owner, index in ((self.foreign, 5), (self.ours, 6)):
            self._skyhook(
                an_owner,
                index,
                effective_workforce=7605,
                workforce_base=8450,
                workforce_siphon_percent=10.0,
            )
        alerts._mark_sent("siphonfp:9000005:10.0")
        titles = self._run(den_alerts.check_siphoned_skyhooks)
        self.assertEqual(len(titles), 1)
        self.assertEqual(
            set(AlertLog.objects.values_list("key", flat=True)), {"siphonfp:9000006:10.0"}
        )


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


class RegistrationGuardTests(TestCase):
    """The shared helper, on every case the two registration paths hand it."""

    def setUp(self):
        self.refresh = stub_corporation_refresh(self)
        self.manager = user_with("manager", "manage_owners", "basic_access")
        self.home_corp = corporation(98000240, "Home Corp", HOME)
        self.main = character_for(self.manager, 94000001, self.home_corp, HOME)

    def _alt(self, character_id, corp, alliance_id):
        return character_for(self.manager, character_id, corp, alliance_id, main=False)

    def test_the_first_registration_decides_the_tracked_alliance(self):
        self.assertIsNone(scope.registration_refusal(self.home_corp, self.main, self.manager))
        self.assertEqual(scope.home_alliance_id(), HOME)

    def test_an_alliance_in_the_extras_does_not_become_home(self):
        """A friend-alliance corporation registered first: the extra is
        passed over, and the registrant -- a home member -- decides."""
        friend_corp = corporation(98000241, "Friend Corp", FRIEND)
        friend = self._alt(94000002, friend_corp, FRIEND)
        with extras(FRIEND):
            self.assertIsNone(scope.registration_refusal(friend_corp, friend, self.manager))
            self.assertEqual(scope.home_alliance_id(), HOME)
            self.assertIsNone(scope.registration_refusal(self.home_corp, self.main, self.manager))
        self.assertEqual(scope.home_alliance_id(), HOME)

    def test_a_friend_registering_first_with_no_other_candidate_leaves_home_unset(self):
        friend_corp = corporation(98000248, "Friend Corp", FRIEND)
        outsider = user_with("friend", "manage_owners")
        friend = character_for(outsider, 94000010, friend_corp, FRIEND)
        with extras(FRIEND):
            self.assertIsNone(scope.registration_refusal(friend_corp, friend, outsider))
        self.assertIsNone(scope.home_alliance_id())

    def test_an_alliance_less_first_registration_adopts_the_registrant_s_alliance(self):
        """Fresh install, holding corp first. Left unset, the registrant could
        not see the structures they had just registered and nothing told
        them why; their own alliance is the obvious answer."""
        holding = corporation(98000249, "Holding Corp")
        alt = self._alt(94000011, holding, None)
        self.assertIsNone(scope.registration_refusal(holding, alt, self.manager))
        self.assertEqual(scope.home_alliance_id(), HOME)

    def test_a_foreign_corporation_is_refused_with_the_whole_story(self):
        set_home(HOME)
        foreign_corp = corporation(98000242, "Foreign Corp", FOREIGN)
        alt = self._alt(94000003, foreign_corp, FOREIGN)
        refusal = scope.registration_refusal(foreign_corp, alt, self.manager)
        for expected in (
            "Foreign Corp",
            "Foreign Alliance",
            "Home Alliance",
            "SOV settings page",
            f"add {FOREIGN} to HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS in local.py",
        ):
            self.assertIn(expected, refusal)

    def test_home_and_extra_corporations_are_allowed(self):
        set_home(HOME)
        friend_corp = corporation(98000243, "Friend Corp", FRIEND)
        friend = self._alt(94000004, friend_corp, FRIEND)
        self.assertIsNotNone(scope.registration_refusal(friend_corp, friend, self.manager))
        with extras(FRIEND):
            self.assertIsNone(scope.registration_refusal(friend_corp, friend, self.manager))
        self.assertIsNone(scope.registration_refusal(self.home_corp, self.main, self.manager))

    def test_an_alliance_less_corporation_needs_a_home_member(self):
        set_home(HOME)
        holding = corporation(98000244, "Holding Corp")
        alt = self._alt(94000005, holding, None)
        self.assertIsNone(scope.registration_refusal(holding, alt, self.manager))

        outsider = user_with("outsider", "manage_owners")
        foreign_corp = corporation(98000245, "Foreign Corp", FOREIGN)
        character_for(outsider, 94000006, foreign_corp, FOREIGN)
        theirs = character_for(outsider, 94000007, holding, None, main=False)
        refusal = scope.registration_refusal(holding, theirs, outsider)
        self.assertIn("Holding Corp has no alliance", refusal)
        self.assertIn("Home Alliance", refusal)
        self.assertIn(f"add {FOREIGN} to", refusal)

    def test_a_stale_corporation_row_is_refreshed_before_judging(self):
        """Auth set the corporation's alliance when it first saw the corp and
        refreshes it on its own schedule; the character's alliance is newer.
        When they differ, the corporation is refreshed so that scope -- which
        reads the corporation -- agrees with the guard."""
        set_home(HOME)
        stale = corporation(98000246, "Moved Corp", FOREIGN)
        alt = self._alt(94000008, stale, HOME)

        def refresh(corp):
            corp.alliance = alliance(HOME)
            corp.save()

        self.refresh.side_effect = refresh
        self.assertIsNone(scope.registration_refusal(stale, alt, self.manager))
        self.refresh.assert_called_once()
        stale.refresh_from_db()
        self.assertEqual(stale.alliance.alliance_id, HOME)

    def test_a_failed_refresh_still_judges_on_both_sources(self):
        set_home(HOME)
        unknown = corporation(98000247, "Unknown Corp")  # Auth has no alliance for it
        alt = self._alt(94000009, unknown, FOREIGN)  # but the character does
        self.refresh.side_effect = OSError("esi down")
        with self.assertLogs(scope.logger, "WARNING"):
            refusal = scope.registration_refusal(unknown, alt, self.manager)
        self.assertIsNotNone(refusal)
        # Auth has no row for the alliance, so it is named by id.
        self.assertIn(f"alliance {FOREIGN}", refusal)

    def test_every_registration_refreshes_the_corporation_once(self):
        """Agreeing sources are no proof: a row made before the corporation
        joined an alliance and a character row that lags say None and None
        together, and that admitted a Goon corporation as alliance-less."""
        set_home(HOME)
        scope.registration_refusal(self.home_corp, self.main, self.manager)
        self.refresh.assert_called_once()

    def test_a_stale_null_row_is_refreshed_and_the_refusal_names_the_real_alliance(self):
        set_home(HOME)
        moved = corporation(98000300, "Moved Corp")
        alt = self._alt(94000012, moved, None)

        def refresh(corp):
            corp.alliance = alliance(FOREIGN)
            corp.save()

        self.refresh.side_effect = refresh
        refusal = scope.registration_refusal(moved, alt, self.manager)
        self.assertIsNotNone(refusal)
        self.assertIn("Foreign Alliance", refusal)

    def test_a_stale_character_row_does_not_refuse_a_home_corporation(self):
        """The refresh just made the corporation row authoritative; a
        character row that still says the old alliance is noise, not a
        second opinion."""
        set_home(HOME)
        alt = self._alt(94000013, self.home_corp, FOREIGN)
        self.assertIsNone(scope.registration_refusal(self.home_corp, alt, self.manager))
        self.refresh.assert_called_once()


class TokensPageRegistrationTests(TestCase):
    """The Tokens page path, driven past its SSO decorator.

    ``add_owner`` sits behind ``@token_required``; ``inspect.unwrap`` reaches
    the view itself, which only needs a token with a character id.
    """

    def setUp(self):
        stub_corporation_refresh(self)
        set_home(HOME)
        self.manager = user_with("manager", "manage_owners")
        self.home_corp = corporation(98000250, "Home Corp", HOME)
        self.main = character_for(self.manager, 94000101, self.home_corp, HOME)
        self.view = inspect.unwrap(owners_views.add_owner)

    def _add(self, character):
        request = request_for(self.manager, "/holdfast/owners/add/", "get")
        with mock.patch.object(tasks.update_owner, "delay") as queued:
            response = self.view(request, Token(character.character_id))
        return response, messages_of(request), queued

    def test_a_foreign_alt_creates_no_owner_and_says_why(self):
        foreign_corp = corporation(98000251, "Foreign Corp", FOREIGN)
        alt = character_for(self.manager, 94000102, foreign_corp, FOREIGN, main=False)
        response, notes, queued = self._add(alt)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Owner.objects.filter(corporation=foreign_corp).exists())
        queued.assert_not_called()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "error")
        self.assertIn("Foreign Corp", notes[0][1])

    def test_a_home_corporation_is_registered_and_synced(self):
        response, notes, queued = self._add(self.main)
        self.assertEqual(response.status_code, 302)
        registered = Owner.objects.get(corporation=self.home_corp)
        self.assertTrue(registered.is_enabled)
        queued.assert_called_once_with(registered.pk)
        self.assertEqual(notes[0][0], "success")

    def test_re_registering_a_disabled_owner_leaves_it_disabled(self):
        Owner.objects.create(corporation=self.home_corp, is_enabled=False)
        _response, notes, _queued = self._add(self.main)
        self.assertFalse(Owner.objects.get(corporation=self.home_corp).is_enabled)
        self.assertIn("disabled", notes[0][1])
        self.assertNotIn('"is_enabled"', inspect.getsource(self.view))


def _load_charlink_hook():
    """Import the hook with charlink stubbed.

    charlink is not in the test INSTALLED_APPS -- importing its real modules
    would trip Django on a model outside the app registry -- and the hook only
    needs the two names it imports from it.
    """
    utils = types.ModuleType("charlink.app_imports.utils")

    class LoginImport:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AppImport:
        def __init__(self, *args, **kwargs):
            pass

    utils.LoginImport, utils.AppImport = LoginImport, AppImport
    charlink_utils = types.ModuleType("charlink.utils")
    charlink_utils.users_with_permissions = lambda *args, **kwargs: None
    stubs = {
        "charlink": types.ModuleType("charlink"),
        "charlink.app_imports": types.ModuleType("charlink.app_imports"),
        "charlink.app_imports.utils": utils,
        "charlink.utils": charlink_utils,
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("holdfast.thirdparty.charlink_hook", None)
        return importlib.import_module("holdfast.thirdparty.charlink_hook")


class CharlinkRegistrationTests(TestCase):
    """The charlink path. It must not raise on a refusal: charlink turns any
    exception into "Failed to add character", which drops the reason and,
    since the den half succeeded first, is not even true."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hook = _load_charlink_hook()

    def setUp(self):
        stub_corporation_refresh(self)
        set_home(HOME)
        self.manager = user_with("manager", "manage_owners", "basic_access")
        self.home_corp = corporation(98000260, "Home Corp", HOME)
        self.main = character_for(self.manager, 94000201, self.home_corp, HOME)

    def _add(self, user, character):
        request = request_for(user, "/charlink/login/", "get")
        with (
            mock.patch.object(tasks.update_owner, "delay") as owner_queued,
            mock.patch.object(tasks.update_den_character, "delay"),
        ):
            self.hook._add_all(request, Token(character.character_id))
        return messages_of(request), owner_queued

    def test_a_foreign_alt_gets_its_dens_registered_and_a_warning_and_no_owner(self):
        from ..models import DenCharacter

        foreign_corp = corporation(98000261, "Foreign Corp", FOREIGN)
        alt = character_for(self.manager, 94000202, foreign_corp, FOREIGN, main=False)
        notes, owner_queued = self._add(self.manager, alt)
        self.assertFalse(Owner.objects.filter(corporation=foreign_corp).exists())
        owner_queued.assert_not_called()
        self.assertTrue(
            DenCharacter.objects.filter(character_ownership__character=alt).exists()
        )
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "warning")
        self.assertIn("Foreign Corp", notes[0][1])

    def test_a_home_corporation_is_registered(self):
        _notes, owner_queued = self._add(self.manager, self.main)
        registered = Owner.objects.get(corporation=self.home_corp)
        owner_queued.assert_called_once_with(registered.pk)

    def test_re_registering_a_disabled_owner_leaves_it_disabled(self):
        Owner.objects.create(corporation=self.home_corp, is_enabled=False)
        notes, _queued = self._add(self.manager, self.main)
        self.assertFalse(Owner.objects.get(corporation=self.home_corp).is_enabled)
        self.assertTrue(any("disabled" in text for _level, text in notes))

    def test_without_manage_owners_no_corporation_is_touched(self):
        operator = user_with("operator", "basic_access")
        character = character_for(operator, 94000203, self.home_corp, HOME)
        notes, owner_queued = self._add(operator, character)
        self.assertEqual(notes, [])
        owner_queued.assert_not_called()
        self.assertFalse(Owner.objects.exists())

    def test_linking_an_alt_leaves_a_registered_corporation_s_token_alone(self):
        """charlink links every character a person has, and a director's alt
        in the same corporation usually lacks the Station Manager role. It
        used to take over the corporation's token, and the next sync 403'd."""
        station_manager = CharacterOwnership.objects.get(character=self.main)
        registered = Owner.objects.create(
            corporation=self.home_corp, character_ownership=station_manager
        )
        alt = character_for(self.manager, 94000204, self.home_corp, HOME, main=False)
        notes, owner_queued = self._add(self.manager, alt)
        registered.refresh_from_db()
        self.assertEqual(registered.character_ownership, station_manager)
        owner_queued.assert_not_called()
        self.assertEqual(len(notes), 1)
        level, text = notes[0]
        self.assertEqual(level, "info")
        self.assertIn(self.main.character_name, text)
        self.assertIn("Tokens page", text)

    def test_relinking_the_registered_character_refreshes_its_sync(self):
        station_manager = CharacterOwnership.objects.get(character=self.main)
        registered = Owner.objects.create(
            corporation=self.home_corp, character_ownership=station_manager
        )
        notes, owner_queued = self._add(self.manager, self.main)
        owner_queued.assert_called_once_with(registered.pk)
        self.assertEqual(notes, [])

    def test_an_owner_whose_character_was_unlinked_takes_the_new_one(self):
        """Nothing to replace: the corporation has no token character at all,
        and its sync fails until somebody supplies one."""
        orphan = Owner.objects.create(corporation=self.home_corp, character_ownership=None)
        alt = character_for(self.manager, 94000205, self.home_corp, HOME, main=False)
        _notes, owner_queued = self._add(self.manager, alt)
        orphan.refresh_from_db()
        self.assertEqual(orphan.character_ownership.character, alt)
        owner_queued.assert_called_once_with(orphan.pk)

    def test_a_character_owned_by_someone_else_registers_nothing(self):
        """The manual path only ever uses the requesting user's own link; this
        one looked the ownership up by character alone."""
        from ..models import DenCharacter

        stranger = user_with("stranger", "basic_access")
        theirs = character_for(stranger, 94000206, self.home_corp, HOME)
        request = request_for(self.manager, "/charlink/login/", "get")
        with (
            mock.patch.object(tasks.update_owner, "delay"),
            mock.patch.object(tasks.update_den_character, "delay"),
            self.assertRaises(CharacterOwnership.DoesNotExist),
        ):
            self.hook._add_all(request, Token(theirs.character_id))
        self.assertFalse(DenCharacter.objects.exists())
        self.assertFalse(Owner.objects.exists())
        self.assertEqual(messages_of(request)[0][0], "error")

    def test_a_manager_without_den_access_registers_the_corporation_only(self):
        """The den fan-out skips a user without den access, and the Tokens
        page will not add a den character for one. charlink did, synced it
        once, and left it enabled and never synced again."""
        from allianceauth.eveonline.models import EveCharacter

        from ..models import DenCharacter

        director = user_with("director", "manage_owners")
        main = character_for(director, 94000207, self.home_corp, HOME)
        request = request_for(director, "/charlink/login/", "get")
        with (
            mock.patch.object(tasks.update_owner, "delay") as owner_queued,
            mock.patch.object(tasks.update_den_character, "delay") as den_queued,
        ):
            self.hook._add_all(request, Token(main.character_id))
        self.assertFalse(DenCharacter.objects.exists())
        den_queued.assert_not_called()
        registered = Owner.objects.get(corporation=self.home_corp)
        owner_queued.assert_called_once_with(registered.pk)

        # And charlink shows the character as linked, by the token it gave.
        self.assertTrue(self.hook._added(main))
        annotated = EveCharacter.objects.annotate(added=self.hook._ADDED_ANNOTATION)
        self.assertTrue(annotated.get(pk=main.pk).added)
        self.assertFalse(annotated.get(pk=self.main.pk).added)

    def test_the_dead_entry_points_are_gone(self):
        """Neither was wired to charlink; a guard on them would have guarded
        nothing while the live entry stayed open."""
        self.assertFalse(hasattr(self.hook, "_add_corporation"))
        self.assertFalse(hasattr(self.hook, "_add_den_character"))


# --------------------------------------------------------------------------
# The upgrade
# --------------------------------------------------------------------------


class BootstrapMigrationTests(TestCase):
    """0008 picks the tracked alliance from what is already registered, so an
    upgrade needs no configuration. Run against the historical models the
    migration is actually handed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical = MigrationLoader(connection).project_state().apps
        cls.column = importlib.import_module("holdfast.migrations.0008_home_alliance")
        cls.migration = importlib.import_module(
            "holdfast.migrations.0009_bootstrap_home_alliance"
        )

    def _bootstrap(self, extra_ids=()):
        return scope.bootstrap_home_alliance(
            self.historical.get_model("holdfast", "Owner"),
            self.historical.get_model("holdfast", "HoldfastConfig"),
            extra_ids,
        )

    def _home(self):
        return (
            HoldfastConfig.objects.filter(pk=1)
            .values_list("home_alliance_id", flat=True)
            .first()
        )

    def test_a_tie_goes_to_the_earliest_owner_with_an_alliance(self):
        """By primary key, not by the model's alphabetical ordering."""
        owner(98000270, "Zulu Holding")  # earliest, but alliance-less
        owner(98000271, "Yankee Corp", HOME)
        owner(98000272, "Alpha Corp", FOREIGN)  # sorts first by name
        self.migration.bootstrap(self.historical, None)
        self.assertEqual(self._home(), HOME)

    def test_the_alliance_most_corporations_are_in_wins_even_if_registered_last(self):
        """The install this was written for: one foreign corporation among
        the alliance's own, registration order unknown. Earliest-wins would
        have made the foreign alliance home and stopped every real owner."""
        owner(98000273, "Alt Holding", FOREIGN)  # registered first
        owner(98000274, "Ours One", HOME)
        owner(98000275, "Ours Two", HOME)
        self.migration.bootstrap(self.historical, None)
        self.assertEqual(self._home(), HOME)
        self.assertEqual(self._bootstrap(), None, "already set; left alone")

    def test_the_live_helper_counts_the_same_way(self):
        owner(98000276, "Alt Holding", FOREIGN)
        owner(98000277, "Ours One", HOME)
        owner(98000278, "Ours Two", HOME)
        self.assertEqual(self._bootstrap(), HOME)

    def test_zero_owners_leaves_it_unset_and_creates_no_row(self):
        self.migration.bootstrap(self.historical, None)
        self.assertEqual(HoldfastConfig.objects.count(), 0)

    def test_an_alliance_already_chosen_is_untouched(self):
        set_home(FRIEND)
        owner(98000273, "Home Corp", HOME)
        self.migration.bootstrap(self.historical, None)
        self.assertEqual(self._home(), FRIEND)

    def test_the_token_character_stands_in_for_a_missing_corporation_alliance(self):
        registrant = user_with("registrant")
        holding = corporation(98000274, "Unrefreshed Corp")
        character = character_for(registrant, 94000301, holding, HOME)
        Owner.objects.create(
            corporation=holding,
            character_ownership=CharacterOwnership.objects.get(character=character),
        )
        self.assertEqual(self._bootstrap(), HOME)
        self.assertEqual(self._home(), HOME)

    def test_disabled_owners_and_extras_are_passed_over(self):
        owner(98000275, "Off Corp", FOREIGN, enabled=False)
        owner(98000276, "Friend Corp", FRIEND)
        owner(98000277, "Home Corp", HOME)
        self.assertEqual(self._bootstrap(extra_ids=[FRIEND]), HOME)

    def test_the_migration_reads_the_extras_from_settings_at_run_time(self):
        """The migration carries its own copy of the loop and reads the
        extras through django.conf.settings, so it neither imports the live
        app nor freezes a value at import time."""
        owner(98000278, "Friend Corp", FRIEND)
        owner(98000279, "Home Corp", HOME)
        with override_settings(HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS=[FRIEND]):
            self.migration.bootstrap(self.historical, None)
        self.assertEqual(self._home(), HOME)

    def test_the_migration_and_the_settings_page_agree(self):
        """Two copies of one rule. This pins them to the same answer on the
        one case with a wrong answer available: an alliance-less owner first."""
        owner(98000284, "Zulu Holding")
        owner(98000285, "Home Corp", HOME)
        self.migration.bootstrap(self.historical, None)
        from_migration = self._home()
        set_home(None)
        self.assertEqual(self._bootstrap(), from_migration)
        self.assertEqual(from_migration, HOME)

    def test_a_non_numeric_extra_does_not_abort_the_migration(self):
        """Before 0.1.9 the extras were used uncast, so a local.py with a
        ticker in the list upgraded fine. It still has to."""
        owner(98000286, "Friend Corp", FRIEND)
        owner(98000287, "Home Corp", HOME)
        with override_settings(HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS=["FRND", str(FRIEND)]):
            self.migration.bootstrap(self.historical, None)
        self.assertEqual(self._home(), HOME)

    def test_the_migration_depends_on_the_apps_whose_rows_it_reads(self):
        self.assertIn(("holdfast", "0008_home_alliance"), self.migration.Migration.dependencies)
        apps_named = {app for app, _ in self.migration.Migration.dependencies}
        self.assertEqual(apps_named, {"holdfast", "eveonline", "authentication"})

    def test_the_column_and_the_data_step_are_separate_migrations(self):
        """On MySQL a column is committed the moment it is added. A data step
        failing in the same migration leaves the column present and the
        migration unrecorded, and the retry dies on a duplicate column."""
        from django.db import migrations

        column_ops = self.column.Migration.operations
        self.assertTrue(all(isinstance(op, migrations.AddField) for op in column_ops))
        self.assertEqual({op.name for op in column_ops}, {"home_alliance_id"})
        data_ops = self.migration.Migration.operations
        self.assertTrue(all(isinstance(op, migrations.RunPython) for op in data_ops))
        self.assertEqual(len(data_ops), 1)


# --------------------------------------------------------------------------
# The Tokens page switch
# --------------------------------------------------------------------------


class ToggleOwnerTests(TestCase):
    def setUp(self):
        set_home(HOME)
        self.ours = owner(98000280, "Ours", HOME)
        self.manager = user_with("manager", "manage_owners")
        # Auth wraps every hook URL in main_character_required, so a user
        # without a main is sent to the dashboard before the view runs.
        character_for(self.manager, 94000402, self.ours.corporation, HOME)
        self.url = f"/holdfast/owners/{self.ours.pk}/toggle/"

    def _call(self, user, url, method="post"):
        match = resolve(url)
        request = request_for(user, url, method)
        response = match.func(request, *match.args, **match.kwargs)
        return response, request

    def test_post_flips_it_and_says_so(self):
        response, request = self._call(self.manager, self.url)
        self.assertEqual(response.status_code, 302)
        self.ours.refresh_from_db()
        self.assertFalse(self.ours.is_enabled)
        self.assertEqual(messages_of(request)[0][0], "success")
        response, _request = self._call(self.manager, self.url)
        self.ours.refresh_from_db()
        self.assertTrue(self.ours.is_enabled)

    def test_get_is_not_allowed(self):
        response, _request = self._call(self.manager, self.url, "get")
        self.assertEqual(response.status_code, 405)

    def test_without_the_permission_it_is_forbidden(self):
        nobody = user_with("nobody", "sov_manage")
        character_for(nobody, 94000403, self.ours.corporation, HOME)
        with self.assertRaises(PermissionDenied):
            self._call(nobody, self.url)

    def test_an_unknown_owner_is_a_404(self):
        with self.assertRaises(Http404):
            self._call(self.manager, "/holdfast/owners/424242/toggle/")

    def test_re_enabling_an_owner_outside_the_tracked_alliance_is_allowed(self):
        """The badge and last_error already say it will not sync; refusing
        would only dictate the order of two clicks."""
        foreign = owner(98000281, "Foreign", FOREIGN, enabled=False)
        response, _request = self._call(self.manager, f"/holdfast/owners/{foreign.pk}/toggle/")
        self.assertEqual(response.status_code, 302)
        foreign.refresh_from_db()
        self.assertTrue(foreign.is_enabled)
        self.assertNotIn(foreign, Owner.objects.active())

    def test_the_tokens_page_shows_what_is_tracked_and_who_is_outside(self):
        alliance(HOME)
        owner(98000282, "Foreign", FOREIGN)
        root = user_with("root", superuser=True)
        character_for(root, 94000401, self.ours.corporation, HOME)
        response, _request = self._call(root, "/holdfast/owners/", "get")
        if hasattr(response, "render"):
            response.render()
        page = response.content.decode()
        self.assertIn("Home Alliance", page)
        self.assertIn("not synced", page)
        self.assertIn(f"/holdfast/owners/{self.ours.pk}/toggle/", page)

    def test_an_owner_that_fell_out_of_scope_shows_why_not_its_stale_ok(self):
        """The fan-out skips an inactive owner, so nothing ever writes the
        reason to last_error after a settings change; the row used to keep
        yesterday's green OK beside the "not synced" badge."""
        alliance(HOME)
        stale = owner(98000283, "Left Us", FOREIGN)
        stale.mark_sync(True)
        root = user_with("root", superuser=True)
        character_for(root, 94000404, self.ours.corporation, HOME)
        response, _request = self._call(root, "/holdfast/owners/", "get")
        if hasattr(response, "render"):
            response.render()
        page = response.content.decode()
        self.assertIn("Change the tracked alliance on the SOV settings page", page)
        self.assertNotIn(">OK<", page)


# --------------------------------------------------------------------------
# The public map follows the tracked set
# --------------------------------------------------------------------------


def _not_modified():
    return HTTPNotModified(status_code=304, headers={})


class SovMapPruneTests(TestCase):
    def setUp(self):
        # The Pi's test run shares a Redis with itself: forget what an
        # earlier test fetched the map for.
        cache.delete(esi_sync.SOV_MAP_TRACKED_KEY)

    def _fetch(self, *solar_systems):
        """One map update against a stubbed ESI. Returns the mock ``result``
        so a test can see whether the fetch was forced."""
        with mock.patch.object(esi_sync, "esi") as client:
            operation = client.client.Sovereignty.GetSovereigntySystems.return_value
            operation.result.return_value = mock.Mock(solar_systems=list(solar_systems))
            esi_sync.update_sov_systems()
        return operation.result

    def _systems(self, *alliance_ids):
        for index, alliance_id in enumerate(alliance_ids):
            SovSystem.objects.create(
                solar_system_id=30004000 + index,
                alliance_id=alliance_id,
                activity_defense_multiplier=5.0,
            )

    def test_an_empty_tracked_set_clears_the_board_without_asking_esi(self):
        self._systems(HOME, FOREIGN)
        with mock.patch.object(esi_sync, "esi") as client:
            self.assertEqual(esi_sync.update_sov_systems(), 0)
        client.client.Sovereignty.GetSovereigntySystems.assert_not_called()
        self.assertEqual(SovSystem.objects.count(), 0)

    def test_an_unchanged_map_still_drops_alliances_no_longer_tracked(self):
        set_home(HOME)
        self._systems(HOME, FOREIGN)
        with mock.patch.object(esi_sync, "esi") as client:
            client.client.Sovereignty.GetSovereigntySystems.return_value.result.side_effect = (
                _not_modified()
            )
            self.assertEqual(esi_sync.update_sov_systems(), 1)
        self.assertEqual(
            list(SovSystem.objects.values_list("alliance_id", flat=True)), [HOME]
        )

    def test_stale_rows_force_a_fresh_fetch(self):
        """A changed tracked alliance must not wait for the map's ETag."""
        set_home(HOME)
        self._systems(FOREIGN)
        result = self._fetch()
        result.assert_called_once_with(force_refresh=True)
        self.assertEqual(SovSystem.objects.count(), 0)

    def test_a_newly_tracked_alliance_with_no_rows_yet_forces_a_fetch(self):
        """The growth direction. Rows cannot tell: the new alliance has none
        yet, so a 304 would leave the board empty until the ETag moved."""
        set_home(HOME)
        self._fetch().assert_called_once_with(force_refresh=True)

    def test_a_new_extra_forces_a_fetch_too(self):
        set_home(HOME)
        self._systems(HOME)
        self._fetch().assert_called_once_with(force_refresh=True)
        with extras(FRIEND):
            self._fetch().assert_called_once_with(force_refresh=True)

    def test_an_unchanged_tracked_set_does_not_force(self):
        """An alliance holding no sovereignty has no rows either, and must
        not turn every run into a full fetch of the cluster."""
        set_home(HOME)
        self._fetch()
        self._fetch().assert_called_once_with(force_refresh=False)
        self.assertEqual(SovSystem.objects.count(), 0)

    def test_the_cost_table_is_cleared_with_the_board(self):
        SystemCostIndex.objects.create(solar_system_id=30004000, alliance_id=HOME)
        with mock.patch.object(esi_sync, "esi") as client:
            self.assertEqual(esi_sync.update_system_costs(), 0)
        client.client.Industry.GetIndustrySystems.assert_not_called()
        self.assertEqual(SystemCostIndex.objects.count(), 0)


# --------------------------------------------------------------------------
# The settings page
# --------------------------------------------------------------------------


class SettingsPageTests(TestCase):
    FORM = {
        "fuel_warning_days": "7",
        "fuel_danger_days": "3",
        "fuel_critical_days": "1",
        "adm_alert_threshold": "3",
    }

    def setUp(self):
        self.root = user_with("root", superuser=True)
        home_corp = corporation(98000290, "Home Corp", HOME)
        character_for(self.root, 94000501, home_corp, HOME)

    def _save(self, **fields):
        url = "/holdfast/sov/settings/save/"
        match = resolve(url)
        request = request_for(self.root, url, "post", {**self.FORM, **fields})
        with mock.patch.object(tasks.update_sov_map, "delay") as queued:
            response = match.func(request, *match.args, **match.kwargs)
        return response, messages_of(request), queued

    def test_choosing_an_alliance_saves_it_and_refreshes_the_board(self):
        _response, notes, queued = self._save(home_alliance_id=str(HOME))
        self.assertEqual(HoldfastConfig.get_solo().home_alliance_id, HOME)
        self.assertEqual(notes[0][0], "success")
        queued.assert_called_once()

    def test_saving_the_same_alliance_leaves_the_board_alone(self):
        set_home(HOME)
        self._save(home_alliance_id=str(HOME))
        _response, _notes, queued = self._save(home_alliance_id=str(HOME))
        queued.assert_not_called()

    def test_blank_with_owners_present_picks_the_earliest_now(self):
        """The blank option promises the first registered corporation
        decides. With one already registered that has to happen at once, or
        every owner drops out of scope and the next map update empties the
        board."""
        set_home(FOREIGN)
        alliance(HOME)
        owner(98000291, "Ours", HOME)
        _response, notes, queued = self._save(home_alliance_id="")
        self.assertEqual(HoldfastConfig.get_solo().home_alliance_id, HOME)
        self.assertTrue(any("Home Alliance" in text for _level, text in notes))
        queued.assert_called_once()

    def test_blank_with_nothing_registered_stays_blank(self):
        _response, _notes, _queued = self._save(home_alliance_id="")
        self.assertIsNone(HoldfastConfig.get_solo().home_alliance_id)

    def test_rubbish_is_refused_and_nothing_changes(self):
        set_home(HOME)
        _response, notes, queued = self._save(home_alliance_id="goons")
        self.assertEqual(HoldfastConfig.get_solo().home_alliance_id, HOME)
        self.assertEqual(notes[0][0], "error")
        queued.assert_not_called()

    def test_the_form_offers_the_current_value_even_when_auth_never_saw_it(self):
        set_home(FOREIGN)
        alliance(HOME)
        url = "/holdfast/sov/settings/"
        match = resolve(url)
        response = match.func(request_for(self.root, url, "get"))
        if hasattr(response, "render"):
            response.render()
        page = response.content.decode()
        self.assertIn(f'<option value="{FOREIGN}" selected>', page)
        self.assertIn(f'<option value="{HOME}" >Home Alliance ({HOME})</option>', page)
