"""Who sees what, and what Django admin may still do.

From the review of 0.1.10. A den officer could read an install-wide directory
mapping every registered character to its main -- including the hundreds
charlink registers that will never run a den -- and the people who had left
the alliance stayed in it, their tokens still polled. A user who could only
register corporation tokens had no way to reach the page that does it. Django
admin offered 84 permissions nobody used, and let a claim be decided without
telling the applicant.
"""

import importlib
from datetime import timedelta
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership, State
from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .. import tasks
from ..auth_hooks import SovMenu
from ..models import (
    DenCharacter,
    DenClaim,
    DenOperator,
    DenSlot,
    General,
    MercenaryDen,
    Owner,
    Skyhook,
)
from ..views import den as den_views
from ..views.common import den_can_enter, den_entrant_ids
from .test_home_alliance import (
    FOREIGN,
    HOME,
    character_for,
    corporation,
    owner,
    set_home,
    user_with,
)

cleanup = importlib.import_module("holdfast.migrations.0015_permission_cleanup")

_ids = iter(range(95_100_000, 95_200_000))


def _ours(codename):
    return Permission.objects.get(content_type__app_label="holdfast", codename=codename)


# --------------------------------------------------------------------------
# The den operator directory
# --------------------------------------------------------------------------


class DenDirectoryTests(TestCase):
    def setUp(self):
        set_home(HOME)
        self.home_corp = corporation(98_100_001, "Home Corp", HOME)
        home = Owner.objects.create(corporation=self.home_corp)
        landlord = owner(98_100_002, "Landlord", FOREIGN)
        self.ours = self._slot(home, 7_100_001)
        self.also_ours = self._slot(home, 7_100_002)
        self.theirs = self._slot(landlord, 7_100_003)

        self.officer = user_with("officer", "den_officer")
        character_for(self.officer, next(_ids), self.home_corp, HOME)

    def _slot(self, owner, skyhook_id):
        skyhook = Skyhook.objects.create(
            skyhook_id=skyhook_id, owner=owner, planet_id=skyhook_id + 33_000_000
        )
        return DenSlot.objects.create(skyhook=skyhook)

    def _operator(self, username, *codenames):
        """A user with one registered den character, which is their main."""
        user = user_with(username, *codenames)
        character = character_for(user, next(_ids), self.home_corp, HOME)
        den_character = DenCharacter.objects.create(
            character_ownership=CharacterOwnership.objects.get(character=character)
        )
        return user, den_character

    def _den(self, den_character, slot, reinforced=False):
        return MercenaryDen.objects.create(
            den_id=next(_ids),
            den_character=den_character,
            planet_id=slot.skyhook.planet_id,
            skyhook_id=slot.skyhook.skyhook_id,
            slot=slot,
            reinforce_end=timezone.now() + timedelta(hours=5) if reinforced else None,
        )

    def _claim(self, den_character, slot, status=DenClaim.Status.PENDING):
        return DenClaim.objects.create(
            slot=slot,
            user=den_character.user,
            character=den_character.character,
            status=status,
        )

    def _directory(self):
        self.client.force_login(self.officer)
        response = self.client.get(reverse("holdfast:den_admin"))
        self.assertEqual(response.status_code, 200)
        return {entry["username"] for entry in response.context["operators"]}

    def test_only_operators_and_claimants_on_our_ground_are_listed(self):
        _user, running = self._operator("running", "basic_access")
        self._den(running, self.ours)
        _user, applying = self._operator("applying", "den_member")
        self._claim(applying, self.also_ours)
        _user, granted = self._operator("granted", "den_member")
        self._claim(granted, self.also_ours, DenClaim.Status.APPROVED)
        # charlink registers every linked character; most never run a den.
        self._operator("linked_only", "basic_access")
        _user, turned_down = self._operator("turned_down", "den_member")
        self._claim(turned_down, self.ours, DenClaim.Status.REJECTED)
        # Another alliance's ground is not ours to keep a directory of.
        _user, abroad = self._operator("abroad", "basic_access")
        self._den(abroad, self.theirs)
        _user, applying_abroad = self._operator("applying_abroad", "den_member")
        self._claim(applying_abroad, self.theirs)

        self.assertEqual(self._directory(), {"running", "applying", "granted"})

    def test_someone_who_has_left_drops_out(self):
        _user, departed = self._operator("departed")
        self._den(departed, self.ours)
        self.assertEqual(self._directory(), set())

    def test_a_claim_follows_the_person_who_made_it(self):
        """A character that changed hands keeps its old claim rows; they are
        the previous owner's, not the new one's."""
        previous, den_character = self._operator("previous", "den_member")
        self._claim(den_character, self.ours)
        newcomer = user_with("newcomer", "den_member")
        DenClaim.objects.filter(user=previous).update(user=newcomer)
        self.assertEqual(self._directory(), set())

    def test_opening_it_writes_nothing(self):
        _user, running = self._operator("running", "basic_access")
        self._den(running, self.ours)
        self._directory()
        self.assertFalse(DenOperator.objects.exists())

    def test_contact_details_come_from_the_operator_s_own_row(self):
        user, running = self._operator("running", "basic_access")
        self._den(running, self.ours)
        DenOperator.objects.create(user=user, timezone="EUTZ", contact_note="evenings")
        self.client.force_login(self.officer)
        response = self.client.get(reverse("holdfast:den_admin"))
        [entry] = response.context["operators"]
        self.assertEqual(entry["note"], "evenings")
        self.assertTrue(entry["timezone"])
        self.assertEqual(entry["main"], running.character)
        self.assertEqual(entry["characters"], [running.character.character_name])
        self.assertEqual(len(entry["dens"]), 1)

    def test_the_directory_costs_the_same_for_one_operator_as_for_five(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                den_views._operator_directory([self.ours.pk, self.also_ours.pk])
            return len(captured)

        _user, first = self._operator("first", "basic_access")
        self._den(first, self.ours)
        one = queries()
        for index in range(4):
            _user, more = self._operator(f"more{index}", "den_member")
            self._den(more, self.also_ours)
        self.assertEqual(queries(), one)

    def test_timers_for_everyone_means_everyone_on_our_ground(self):
        _user, home_den = self._operator("home", "basic_access")
        self._den(home_den, self.ours, reinforced=True)
        _user, abroad = self._operator("abroad", "basic_access")
        self._den(abroad, self.theirs, reinforced=True)
        self.client.force_login(self.officer)
        response = self.client.get(reverse("holdfast:den_timers") + "?scope=all")
        self.assertTrue(response.context["scope_all"])
        details = {entry["detail"] for entry in response.context["entries"]}
        self.assertEqual(details, {str(home_den)})

    def test_an_officer_s_own_den_abroad_stays_on_their_timers(self):
        own = DenCharacter.objects.create(
            character_ownership=CharacterOwnership.objects.get(
                character=self.officer.profile.main_character
            )
        )
        self._den(own, self.theirs, reinforced=True)
        self.client.force_login(self.officer)
        response = self.client.get(reverse("holdfast:den_timers") + "?scope=all")
        details = {entry["detail"] for entry in response.context["entries"]}
        self.assertEqual(details, {str(own)})


# --------------------------------------------------------------------------
# Departed users
# --------------------------------------------------------------------------


@override_settings(
    # What a real install runs; the test settings leave Django's default,
    # which knows nothing of states.
    AUTHENTICATION_BACKENDS=["allianceauth.authentication.backends.StateBackend"]
)
class DenEntrantTests(TestCase):
    """The directory and the den fan-out ask den_can_enter of many users at
    once. One query must give the answer the permission backend gives."""

    def test_the_set_answer_matches_the_per_user_one(self):
        direct = user_with("direct", "basic_access")
        officer = user_with("officer", "den_officer")
        unrelated = user_with("unrelated", "sov_officer")
        nobody = user_with("nobody")
        inactive = user_with("inactive", "den_member")
        inactive.is_active = False
        inactive.save()
        root = user_with("root", superuser=True)

        grouped = user_with("grouped")
        group = Group.objects.create(name="Den operators")
        group.permissions.add(_ours("den_member"))
        grouped.groups.add(group)

        member = user_with("member")
        main = character_for(member, next(_ids), corporation(98_100_010, "Member Corp"))
        state = State.objects.create(name="Members", priority=150)
        state.permissions.add(_ours("basic_access").pk)  # by key: a proxy relation
        state.member_characters.add(main)
        member.profile.refresh_from_db()
        self.assertEqual(member.profile.state, state, "the fixture must put them in it")

        users = [direct, officer, unrelated, nobody, inactive, root, grouped, member]
        expected = {
            user.pk for user in users if den_can_enter(User.objects.get(pk=user.pk))
        }
        self.assertEqual(
            expected, {direct.pk, officer.pk, root.pk, grouped.pk, member.pk}
        )
        self.assertEqual(den_entrant_ids([user.pk for user in users]), expected)


class DepartedFanOutTests(TestCase):
    """Someone who loses access -- leaves the alliance, drops to Guest --
    keeps their DenCharacter rows, so coming back needs no new token. Their
    characters just stop being polled."""

    def _den_character(self, username, *codenames):
        user = user_with(username, *codenames)
        character = character_for(user, next(_ids), corporation(next(_ids), username))
        return DenCharacter.objects.create(
            character_ownership=CharacterOwnership.objects.get(character=character)
        )

    def test_only_characters_of_people_who_may_still_use_dens_are_synced(self):
        staying = self._den_character("staying", "basic_access")
        gone = self._den_character("gone")
        queued = []
        with mock.patch.object(
            tasks.update_den_character,
            "apply_async",
            side_effect=lambda args, **_: queued.append(args[0]),
        ):
            tasks.update_all_den_characters()
        self.assertEqual(queued, [staying.pk])
        self.assertTrue(DenCharacter.objects.filter(pk=gone.pk).exists(), "kept, not deleted")


# --------------------------------------------------------------------------
# A user who only registers tokens
# --------------------------------------------------------------------------


class TokensOnlyTests(TestCase):
    def setUp(self):
        self.registrar = user_with("registrar", "manage_owners")
        character_for(self.registrar, next(_ids), corporation(98_100_020, "Reg Corp"))

    def test_the_sov_entry_is_shown_to_them(self):
        self.assertTrue(SovMenu.predicate(self.registrar))
        self.assertFalse(SovMenu.predicate(user_with("nobody")))

    def test_the_sov_home_sends_them_to_the_tokens_page(self):
        self.client.force_login(self.registrar)
        response = self.client.get(reverse("holdfast:sov_home"))
        self.assertRedirects(response, reverse("holdfast:owners"))

    def test_a_member_still_lands_on_system_cost(self):
        member = user_with("member", "basic_access", "manage_owners")
        character_for(member, next(_ids), corporation(98_100_021, "Mem Corp"))
        self.client.force_login(member)
        response = self.client.get(reverse("holdfast:sov_home"))
        self.assertRedirects(
            response, reverse("holdfast:sov_system_cost"), fetch_redirect_response=False
        )

    def test_nobody_else_gets_in(self):
        nobody = user_with("nobody")
        character_for(nobody, next(_ids), corporation(98_100_022, "No Corp"))
        self.client.force_login(nobody)
        self.assertEqual(self.client.get(reverse("holdfast:sov_home")).status_code, 403)


# --------------------------------------------------------------------------
# Django admin
# --------------------------------------------------------------------------


class DenClaimAdminTests(TestCase):
    """A decision made on the den admin page notifies the applicant and
    rejects everyone else queued on the site. Editing the status in Django
    admin did neither."""

    def setUp(self):
        self.request = RequestFactory().get("/admin/")
        self.request.user = user_with("root", superuser=True)

    def test_status_is_read_only(self):
        model_admin = admin.site._registry[DenClaim]
        self.assertIn("status", model_admin.get_readonly_fields(self.request))
        description = model_admin.get_fieldsets(self.request)[0][1]["description"]
        self.assertIn("Den admin page", description)

    def test_status_is_read_only_inside_a_slot_too(self):
        slot_admin = admin.site._registry[DenSlot]
        [inline] = [
            inline for inline in slot_admin.get_inline_instances(self.request)
            if inline.model is DenClaim
        ]
        self.assertIn("status", inline.get_readonly_fields(self.request))


class DefaultPermissionTests(TestCase):
    """Django gives every model add/change/delete/view. Holdfast's pages
    check none of them, and 84 extra rows in the group permission picker
    buried the nine that matter."""

    def test_no_model_declares_them(self):
        for model in apps.get_app_config("holdfast").get_models():
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.default_permissions, ())

    def test_none_exist_after_migrating(self):
        self.assertEqual(
            sorted(
                Permission.objects.filter(content_type__app_label="holdfast")
                .exclude(content_type=ContentType.objects.get_for_model(General))
                .values_list("codename", flat=True)
            ),
            [],
        )

    def test_a_superuser_still_has_the_whole_admin(self):
        root = user_with("root", superuser=True)
        self.client.force_login(root)
        for name in ("owner", "webhook", "denclaim"):
            with self.subTest(model=name):
                response = self.client.get(reverse(f"admin:holdfast_{name}_changelist"))
                self.assertEqual(response.status_code, 200)


class PermissionCleanupMigrationTests(TestCase):
    """0015, against the models an upgrading install hands it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical = MigrationLoader(connection).project_state().apps

    def setUp(self):
        owner_type = ContentType.objects.get_for_model(Owner)
        self.stale = [
            Permission.objects.create(
                codename=f"{action}_owner", name=f"Can {action} owner", content_type=owner_type
            )
            for action in ("add", "change", "delete", "view")
        ]
        self.group = Group.objects.create(name="admins")
        self.group.permissions.add(*self.stale, _ours("sov_manage"))
        # Another app's default permission, granted the same way.
        self.theirs = Permission.objects.get(
            content_type__app_label="auth", codename="view_group"
        )
        self.group.permissions.add(self.theirs)

    def test_ours_go_and_everything_else_stays(self):
        cleanup.drop_default_permissions(self.historical, None)
        self.assertFalse(
            Permission.objects.filter(pk__in=[p.pk for p in self.stale]).exists()
        )
        self.assertEqual(
            set(self.group.permissions.values_list("codename", flat=True)),
            {"sov_manage", "view_group"},
        )
        self.assertTrue(_ours("manage_owners"))

    def test_the_token_permission_is_relabelled_and_back(self):
        Permission.objects.filter(pk=_ours("manage_owners").pk).update(
            name=cleanup.OLD_LABEL
        )
        cleanup.relabel_manage_owners(self.historical, None)
        self.assertEqual(_ours("manage_owners").name, cleanup.NEW_LABEL)
        self.assertNotIn("character", cleanup.NEW_LABEL)
        cleanup.restore_manage_owners_label(self.historical, None)
        self.assertEqual(_ours("manage_owners").name, cleanup.OLD_LABEL)

    def test_the_model_label_matches_the_migration(self):
        declared = dict(General._meta.permissions)
        self.assertEqual(declared["manage_owners"], cleanup.NEW_LABEL)
