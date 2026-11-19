"""The den dashboard: sovereignty upgrades a den has knocked offline, and a
time on every row.

Flo's report was "rig is offline" -- a sovereignty upgrade that went dark
because a den in the same system is taking the skyhook's workforce. The den
admin page already said so; the dashboard, where an officer actually looks,
did not. Every row under "Across the alliance" also had no time, or the wrong
kind of one: "3 hours ago" next to a siphon reads as if something happened
three hours ago, not as something that has been going on for three hours.
"""

import importlib
import itertools
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.apps import apps as django_apps
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from eveuniverse.models import EveConstellation, EveRegion, EveSolarSystem

from ..core import esi_sync
from ..core.siphon import siphon_since, starved_upgrades_in_system
from ..models import (
    AlertLog,
    DenCharacter,
    DenClaim,
    DenEvent,
    DenSlot,
    EvolutionLevel,
    HoldfastConfig,
    MercenaryDen,
    Owner,
    PowerState,
    Skyhook,
    SovHub,
    SovHubUpgrade,
)
from ..templatetags.holdfast_tags import relative_moment

_ids = itertools.count(97_000_001)
SYSTEM_ID = 30_000_401


def plain(text):
    """timesince joins its units with a non-breaking space."""
    return str(text).replace(" ", " ")


def make_user(username, *codenames, superuser=False):
    user = User.objects.create_user(username)
    if codenames:
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="holdfast", codename__in=codenames
            )
        )
    character = EveCharacter.objects.create(
        character_id=next(_ids),
        character_name=f"Main of {username}",
        corporation_id=98000400,
        corporation_name="Dash Corp",
        corporation_ticker="DSH",
    )
    CharacterOwnership.objects.create(
        user=user, character=character, owner_hash=f"main-{username}"
    )
    profile = user.profile
    profile.main_character = character
    profile.save()
    if superuser:
        # Both corporations below are visible to a superuser; an ordinary
        # member of one would not see the other, which is not what these
        # tests are about.
        user.is_superuser = True
        user.save()
    return User.objects.get(pk=user.pk)


class DashboardFixture(TestCase):
    def setUp(self):
        cache.clear()
        HoldfastConfig.get_solo()
        region = EveRegion.objects.create(id=10_000_401, name="Dashland")
        constellation = EveConstellation.objects.create(
            id=20_000_401, name="Dashcon", eve_region=region
        )
        self.system = EveSolarSystem.objects.create(
            id=SYSTEM_ID,
            name="D-ASH1",
            eve_constellation=constellation,
            security_status=-0.4,
        )
        self.skyhook_owner = self._owner(98000401, "Hook Corp", "HOOK")
        self.hub_owner = self._owner(98000402, "Hub Corp", "HUB")
        self.skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_004_001,
            owner=self.skyhook_owner,
            planet_id=40_004_001,
            eve_solar_system=self.system,
            effective_workforce=7605,
            workforce_siphon_percent=10.0,
            workforce_base=8450,
        )
        self.slot = DenSlot.objects.create(skyhook=self.skyhook)
        self.hub = SovHub.objects.create(
            hub_id=1_000_000_004_100,
            owner=self.hub_owner,
            solar_system_id=SYSTEM_ID,
            eve_solar_system=self.system,
        )
        self.officer = make_user(
            "officer", "basic_access", "den_officer", superuser=True
        )

    def _owner(self, corporation_id, name, ticker):
        corporation = EveCorporationInfo.objects.create(
            corporation_id=corporation_id,
            corporation_name=name,
            corporation_ticker=ticker,
            member_count=5,
        )
        return Owner.objects.create(corporation=corporation)

    def upgrade(self, state=PowerState.LOW, type_id=81_000, low_since=None, hub=None):
        return SovHubUpgrade.objects.create(
            hub=hub or self.hub,
            type_id=type_id,
            power_state=state,
            low_since=low_since,
        )

    def officer_items(self, user=None):
        self.client.force_login(user or self.officer)
        response = self.client.get(reverse("holdfast:den_dashboard"))
        self.assertEqual(response.status_code, 200)
        return response.context["officer_items"]

    @staticmethod
    def offline_rows(items):
        return [i for i in items if "sovereignty upgrade" in i["kind"].lower()]


class OfficerOfflineUpgradeTests(DashboardFixture):
    def test_a_siphon_beside_an_unpowered_upgrade_gets_its_own_row(self):
        """The hub belongs to another corporation than the skyhook -- the way
        an alliance's sov and its skyhooks are usually split."""
        since = timezone.now() - timedelta(hours=5)
        self.upgrade(low_since=since)

        rows = self.offline_rows(self.officer_items())

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["severity"], "danger")
        self.assertEqual(row["kind"], "Sovereignty upgrade offline")
        self.assertEqual(row["where"], "D-ASH1")
        self.assertIn("81000", row["detail"])
        self.assertIn("(10%)", row["detail"])
        self.assertEqual(row["when"], since)
        self.assertTrue(row["since"])

    def test_several_upgrades_are_counted_in_the_kind(self):
        self.upgrade(type_id=81_000)
        self.upgrade(type_id=81_001)
        rows = self.offline_rows(self.officer_items())
        self.assertEqual(rows[0]["kind"], "2 sovereignty upgrades offline")

    def test_a_siphon_with_every_upgrade_powered_adds_nothing(self):
        self.upgrade(state=PowerState.ONLINE)
        self.assertEqual(self.offline_rows(self.officer_items()), [])

    def test_an_unpowered_upgrade_with_no_siphon_adds_nothing(self):
        """Nobody hauled fuel is a sov-dashboard problem, not a den one."""
        self.upgrade()
        Skyhook.objects.update(workforce_siphon_percent=None, workforce_base=None)
        self.assertEqual(self.offline_rows(self.officer_items()), [])

    def test_a_den_member_does_not_get_the_alliance_section(self):
        self.upgrade()
        member = make_user("member", "basic_access", "den_member")
        self.client.force_login(member)
        response = self.client.get(reverse("holdfast:den_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["officer_items"], [])

    def test_the_notification_switch_does_not_hide_it(self):
        """The den admin page shows the same thing unconditionally, and the
        siphon row beside it ignores ``notify_den_skyhook_impact``. The switch
        sits under "Notifications" and only decides what goes to Discord."""
        config = HoldfastConfig.get_solo()
        config.notify_den_sov_impact = False
        config.save()
        self.upgrade()
        self.assertEqual(len(self.offline_rows(self.officer_items())), 1)

    def test_an_inactive_owners_hub_is_ignored(self):
        self.upgrade()
        self.hub_owner.is_enabled = False
        self.hub_owner.save()
        self.assertEqual(self.offline_rows(self.officer_items()), [])


class SiphonTimeTests(DashboardFixture):
    def siphon_row(self):
        rows = [
            i
            for i in self.officer_items()
            if i["kind"] == "Den siphoning our workforce"
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_measured_siphon_is_timed_from_when_it_was_first_seen(self):
        since = timezone.now() - timedelta(hours=7)
        Skyhook.objects.update(workforce_siphon_since=since)
        row = self.siphon_row()
        self.assertEqual(row["when"], since)
        self.assertTrue(row["since"])

    def test_an_inferred_siphon_is_timed_from_the_drop(self):
        dropped = timezone.now() - timedelta(hours=9)
        Skyhook.objects.update(
            effective_workforce=8280,
            workforce_siphon_percent=None,
            workforce_base=None,
            workforce_high_water=9200,
            workforce_dropped_at=dropped,
            workforce_siphon_since=timezone.now(),  # must not be the one used
        )
        skyhook = Skyhook.objects.get()
        self.assertEqual(skyhook.siphon_estimate[2], "inferred")
        self.assertEqual(skyhook.siphon_since, dropped)
        self.assertEqual(self.siphon_row()["when"], dropped)

    def test_an_unknown_start_stays_unknown(self):
        """No time at all beats a wrong one."""
        self.assertIsNone(self.siphon_row()["when"])


class PendingClaimTests(DashboardFixture):
    def test_the_oldest_claim_sets_the_time_and_the_name(self):
        now = timezone.now()
        for index, age in enumerate((timedelta(hours=1), timedelta(hours=30))):
            character = EveCharacter.objects.create(
                character_id=next(_ids),
                character_name=f"Applicant {index}",
                corporation_id=98000400,
                corporation_name="Dash Corp",
                corporation_ticker="DSH",
            )
            DenClaim.objects.create(
                slot=self.slot,
                character=character,
                user=self.officer,
                status=DenClaim.Status.PENDING,
                created_at=now - age,
            )
        rows = [
            i for i in self.officer_items() if "awaiting a decision" in i["kind"]
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "2 claim(s) awaiting a decision")
        self.assertEqual(rows[0]["when"], now - timedelta(hours=30))
        self.assertTrue(rows[0]["since"])
        self.assertIn("Applicant 1", rows[0]["detail"])


class OperatorRowTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        self.operator = make_user("operator", "basic_access", "den_member")
        ownership = CharacterOwnership.objects.get(
            user=self.operator, character=self.operator.profile.main_character
        )
        den_character = DenCharacter.objects.create(character_ownership=ownership)
        self.den = MercenaryDen.objects.create(
            den_id=2_000_000_004_001,
            den_character=den_character,
            planet_id=self.skyhook.planet_id,
            skyhook_id=self.skyhook.skyhook_id,
            slot=self.slot,
            state=MercenaryDen.State.RUNNING,
            anarchy_level=EvolutionLevel.L2,
        )

    def row(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("holdfast:den_dashboard"))
        rows = [
            i
            for i in response.context["items"]
            if i["kind"] == "Your den is taking our own workforce"
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_it_says_an_upgrade_went_down_without_naming_it(self):
        """The sovereignty pages are not the operator's to read, so the row
        carries a count and nothing that identifies what is down."""
        since = timezone.now() - timedelta(hours=3)
        Skyhook.objects.update(workforce_siphon_since=since)
        self.upgrade(type_id=81_777)
        row = self.row()
        self.assertEqual(row["severity"], "danger")
        self.assertIn("sovereignty upgrade(s)", row["detail"])
        self.assertIn("1 sovereignty upgrade(s)", row["detail"])
        self.assertNotIn("81777", row["detail"])
        self.assertEqual(row["when"], since)
        self.assertTrue(row["since"])

    def test_an_attack_row_is_a_past_event_shown_as_since(self):
        happened = timezone.now() - timedelta(hours=2)
        DenEvent.objects.create(
            notification_id=9_200_000_001,
            den_character=self.den.den_character,
            den=self.den,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=happened,
        )
        self.client.force_login(self.operator)
        response = self.client.get(reverse("holdfast:den_dashboard"))
        rows = [i for i in response.context["items"] if i["kind"] == "Your den was attacked"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["when"], happened)
        self.assertTrue(rows[0]["since"])

    def test_without_an_upgrade_down_it_stays_a_warning(self):
        row = self.row()
        self.assertEqual(row["severity"], "warning")
        self.assertNotIn("sovereignty upgrade", row["detail"])


class RelativeMomentTests(SimpleTestCase):
    def test_a_past_moment_reads_as_a_duration_in_since_mode(self):
        value = timezone.now() - timedelta(hours=3, seconds=30)
        self.assertEqual(plain(relative_moment(value, "since")), "for 3 hours")

    def test_a_future_moment_is_never_shown_as_a_deadline_in_since_mode(self):
        value = timezone.now() + timedelta(hours=2)
        self.assertEqual(plain(relative_moment(value, "since")), "for 0 minutes")

    def test_the_default_mode_is_unchanged(self):
        past = timezone.now() - timedelta(hours=3, seconds=30)
        future = timezone.now() + timedelta(days=2, minutes=1)
        self.assertEqual(plain(relative_moment(past)), "3 hours ago")
        self.assertEqual(plain(relative_moment(future)), "in 2 days")
        self.assertEqual(relative_moment(None, "since"), "")


class FeedRenderingTests(SimpleTestCase):
    def render(self, **item):
        base = {
            "severity": "warning",
            "kind": "Thing",
            "where": "Somewhere",
            "system": None,
            "detail": "",
        }
        base.update(item)
        return render_to_string("holdfast/partials/_feed.html", {"items": [base]})

    def test_a_since_item_says_since(self):
        when = datetime(2026, 11, 16, 9, 0, tzinfo=dt_timezone.utc)
        html = " ".join(self.render(when=when, since=True).split())
        self.assertIn("for ", html)
        self.assertIn("since 11-16 09:00 EVE", html)
        self.assertNotIn(" ago", html)

    def test_a_deadline_item_does_not(self):
        html = self.render(when=timezone.now() + timedelta(hours=5))
        self.assertIn("in 4 hours", plain(html))
        self.assertNotIn("since", html)
        self.assertIn(" EVE", html)


class StoreHubDetailTests(DashboardFixture):
    """``low_since`` is the first time Holdfast read the upgrade as Low."""

    def store(self, state, observed_at, type_id=81_000):
        data = {"upgrades": [{"type_id": type_id, "power_state": state}]}
        esi_sync._store_hub_detail(
            self.hub, data, {type_id: None}, observed_at=observed_at
        )
        return SovHubUpgrade.objects.get(hub=self.hub, type_id=type_id)

    def test_online_to_low_records_when_it_was_seen(self):
        t0 = timezone.now() - timedelta(hours=2)
        self.store("Online", t0 - timedelta(hours=1))
        self.assertIsNone(self.store("Online", t0).low_since)
        self.assertEqual(self.store("Low", t0).low_since, t0)

    def test_low_to_low_keeps_the_first_time(self):
        t0 = timezone.now() - timedelta(hours=2)
        self.store("Low", t0)
        again = self.store("Low", t0 + timedelta(hours=1))
        self.assertEqual(again.low_since, t0)

    def test_low_to_online_clears_it(self):
        self.store("Low", timezone.now() - timedelta(hours=2))
        self.assertIsNone(self.store("Online", timezone.now()).low_since)

    def test_a_new_upgrade_that_is_already_low_starts_now(self):
        now = timezone.now()
        self.assertEqual(self.store("Low", now).low_since, now)

    def test_an_upgrade_low_before_the_column_existed_stays_unknown(self):
        """The backfill leaves it NULL when no alert recorded it; a later read
        must not invent a start time for something already down."""
        self.upgrade(low_since=None)
        self.assertIsNone(self.store("Low", timezone.now()).low_since)


class SiphonSincePureTests(SimpleTestCase):
    NOW = timezone.now()
    EARLIER = NOW - timedelta(hours=4)

    def test_no_siphon_has_no_start(self):
        self.assertIsNone(siphon_since(10.0, self.EARLIER, None, self.NOW))

    def test_a_new_siphon_starts_now(self):
        self.assertEqual(siphon_since(None, None, 10.0, self.NOW), self.NOW)

    def test_a_change_of_rate_does_not_restart_the_clock(self):
        self.assertEqual(siphon_since(10.0, self.EARLIER, 20.0, self.NOW), self.EARLIER)

    def test_a_siphon_of_unknown_age_stays_unknown(self):
        self.assertIsNone(siphon_since(10.0, None, 10.0, self.NOW))


class StarvedUpgradesTests(DashboardFixture):
    def test_it_finds_another_active_corporations_hub(self):
        self.upgrade()
        found = starved_upgrades_in_system(self.skyhook)
        self.assertEqual([u.type_id for u in found], [81_000])

    def test_it_ignores_an_inactive_owner(self):
        self.upgrade()
        self.hub_owner.is_enabled = False
        self.hub_owner.save()
        self.assertEqual(starved_upgrades_in_system(self.skyhook), [])

    def test_a_skyhook_with_no_known_system_finds_nothing(self):
        self.upgrade()
        self.skyhook.eve_solar_system = None
        self.assertEqual(starved_upgrades_in_system(self.skyhook), [])

    def test_the_admin_page_uses_the_same_search(self):
        self.upgrade()
        admin = make_user("admin", "basic_access", "den_manage", superuser=True)
        self.client.force_login(admin)
        response = self.client.get(reverse("holdfast:den_admin"))
        self.assertEqual(response.status_code, 200)
        impacted = response.context["sov_impacted"]
        self.assertEqual(len(impacted), 1)
        self.assertEqual(impacted[0]["upgrades"][0].type_id, 81_000)


class BackfillTests(DashboardFixture):
    """0018 gives the siphons and outages already under way a start time from
    the first alert sent about them."""

    def run_backfill(self):
        migration = importlib.import_module("holdfast.migrations.0018_since_times")
        migration.backfill_since(django_apps, None)

    def test_the_earliest_alert_becomes_the_start(self):
        early = timezone.now() - timedelta(days=2)
        late = timezone.now() - timedelta(days=1)
        # An older rate and a newer one for the same skyhook: the first wins.
        AlertLog.objects.create(key=f"siphonfp:{self.skyhook.skyhook_id}:10.0", sent_at=late)
        AlertLog.objects.create(key=f"siphonfp:{self.skyhook.skyhook_id}:20.0", sent_at=early)
        self.upgrade()
        AlertLog.objects.create(key=f"upglow:{self.hub.hub_id}", sent_at=early)

        self.run_backfill()

        self.skyhook.refresh_from_db()
        self.assertEqual(self.skyhook.workforce_siphon_since, early)
        self.assertEqual(SovHubUpgrade.objects.get().low_since, early)

    def test_nothing_on_record_stays_unknown(self):
        self.upgrade()
        self.run_backfill()
        self.skyhook.refresh_from_db()
        self.assertIsNone(self.skyhook.workforce_siphon_since)
        self.assertIsNone(SovHubUpgrade.objects.get().low_since)

    def test_a_cleared_siphon_is_not_given_a_start(self):
        AlertLog.objects.create(key=f"siphonfp:{self.skyhook.skyhook_id}:10.0")
        Skyhook.objects.update(workforce_siphon_percent=None)
        self.run_backfill()
        self.skyhook.refresh_from_db()
        self.assertIsNone(self.skyhook.workforce_siphon_since)

    def test_it_does_not_overwrite_a_known_start(self):
        known = timezone.now() - timedelta(hours=1)
        Skyhook.objects.update(workforce_siphon_since=known)
        AlertLog.objects.create(
            key=f"siphonfp:{self.skyhook.skyhook_id}:10.0",
            sent_at=timezone.now() - timedelta(days=3),
        )
        self.run_backfill()
        self.skyhook.refresh_from_db()
        self.assertEqual(self.skyhook.workforce_siphon_since, known)


class SiphonApplyToTests(SimpleTestCase):
    def test_apply_to_keeps_the_start_in_step(self):
        from ..core.siphon import apply_to

        skyhook = Skyhook(effective_workforce=7605)
        with mock.patch("holdfast.core.siphon.timezone.now", return_value=SiphonSincePureTests.NOW):
            self.assertTrue(apply_to(skyhook))
        self.assertEqual(skyhook.workforce_siphon_since, SiphonSincePureTests.NOW)

        skyhook.effective_workforce = 7600
        self.assertTrue(apply_to(skyhook))
        self.assertIsNone(skyhook.workforce_siphon_since)


class DashboardOrderTests(SimpleTestCase):
    """Rows with a start in the past and rows with a deadline share a severity."""

    def test_an_imminent_deadline_comes_before_a_long_running_row(self):
        from ..views.common import sort_dashboard_items

        now = timezone.now()
        items = [
            {"severity": "warning", "when": None, "kind": "untimed"},
            {"severity": "warning", "when": now - timedelta(days=2), "since": True, "kind": "since"},
            {"severity": "warning", "when": now + timedelta(hours=1), "kind": "deadline"},
            {"severity": "critical", "when": now - timedelta(days=9), "since": True, "kind": "worse"},
        ]
        sort_dashboard_items(items, now)
        self.assertEqual(
            [i["kind"] for i in items], ["worse", "deadline", "since", "untimed"]
        )
