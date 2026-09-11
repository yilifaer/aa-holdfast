"""The den ladder, and the sidebar entries that lead into it.

Both came from the first alliance to run this in production, and both are
about how the app sits inside Auth rather than what it computes.

Five den permissions was two too many to hand out: applying for a slot was a
permission of its own that nothing above it implied, so every group that could
reach the claim button needed it separately. It is part of den_member now, and
the migration that removed it must not take anyone's access away on the way.

And the sidebar folder an admin filed these entries under snapped shut on every
Holdfast page. Auth highlights by exact URL name or whole namespace; these
entries gave it a prefix, which matches nothing.
"""

import ast
import importlib
import pathlib

from allianceauth.authentication.models import State
from allianceauth.thirdparty.navhelper.templatetags.navactive import navactive
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from .. import urls
from ..auth_hooks import DenMenu, SkyhookMenu, SovMenu
from ..models import General
from ..views.common import den_can_claim

ROOT = pathlib.Path(__file__).resolve().parent.parent
fold = importlib.import_module("holdfast.migrations.0007_fold_den_claim")


def _ours(codename):
    return Permission.objects.get(content_type__app_label="holdfast", codename=codename)


class SidebarHighlightTests(SimpleTestCase):
    def setUp(self):
        self.menus = {"sov": SovMenu(), "skyhook": SkyhookMenu(), "den": DenMenu()}

    def test_every_page_lights_exactly_one_entry(self):
        # SSO hand-offs redirect before rendering anything to highlight.
        redirects = {"add_owner", "add_den_character"}
        for pattern in urls.urlpatterns:
            if not pattern.name or pattern.name in redirects:
                continue
            lit = [
                section
                for section, menu in self.menus.items()
                if f"holdfast:{pattern.name}" in menu.navactive
            ]
            self.assertEqual(len(lit), 1, f"{pattern.name} lights {lit or 'nothing'}")

    def test_a_page_deep_in_a_section_is_highlighted(self):
        """Every section's home redirects, so no rendered page is ever a home
        page -- which is why matching only the entry's own URL lit nothing."""
        request = RequestFactory().get(reverse("holdfast:skyhook_raid"))
        lit = {
            section: navactive(request, " ".join(menu.navactive))
            for section, menu in self.menus.items()
        }
        self.assertEqual(lit, {"sov": "", "skyhook": "active", "den": ""})


class DenLadderTests(TestCase):
    def _user_with(self, codename):
        user = User.objects.create_user(f"holder_{codename}")
        user.user_permissions.add(_ours(codename))
        return User.objects.get(pk=user.pk)  # a fresh object: no permission cache

    def test_claim_is_no_longer_a_permission(self):
        self.assertFalse(
            Permission.objects.filter(
                content_type__app_label="holdfast", codename="den_claim"
            ).exists()
        )

    def test_the_user_rung_and_everything_above_it_may_claim(self):
        self.assertFalse(den_can_claim(self._user_with("den_basic")))
        for codename in ("den_member", "den_officer", "den_manage"):
            self.assertTrue(den_can_claim(self._user_with(codename)), codename)

    def test_the_claim_views_are_gated_by_the_ladder(self):
        tree = ast.parse((ROOT / "views" / "den.py").read_text(encoding="utf-8"))
        gates = {
            node.name: ast.unparse(node.decorator_list[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name in ("claim_slot", "withdraw_claim")
        }
        self.assertEqual(
            gates,
            {
                "claim_slot": "require_any(*DEN_LIST)",
                "withdraw_claim": "require_any(*DEN_LIST)",
            },
        )


class FoldDenClaimMigrationTests(TestCase):
    """The upgrade itself. On the day it runs, groups and states on a live Auth
    hold den_claim; afterwards each must be able to do what it could before.

    The migration is handed what an upgrading install hands it -- every app's
    models at their latest -- rather than the live registry. Alliance Auth's
    State relates to its own proxy of Permission, so adding a plain Permission
    to a state raises TypeError. The first version of this migration did just
    that, and would have stopped the upgrade on any install with den_claim on a
    state. The install it was first run on had none, so migrating it proved
    nothing.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical = MigrationLoader(connection).project_state().apps

    def setUp(self):
        content_type = ContentType.objects.get_for_model(General)
        self.claim = Permission.objects.create(
            codename="den_claim", name=fold.CLAIM_LABEL, content_type=content_type
        )
        # post_migrate already wrote the new labels; put an old one back, or
        # the rename would pass without ever running.
        Permission.objects.filter(pk=_ours("den_member").pk).update(
            name=fold.OLD_LABELS["den_member"]
        )

    def _codenames(self, holder, relation="permissions"):
        return set(
            getattr(holder, relation)
            .filter(content_type__app_label="holdfast")
            .values_list("codename", flat=True)
        )

    def test_a_group_that_could_only_claim_is_given_the_user_rung(self):
        group = Group.objects.create(name="claimers")
        group.permissions.add(self.claim)
        fold.fold_claim(self.historical, None)
        self.assertEqual(self._codenames(group), {"den_member"})

    def test_a_rung_that_already_claims_gets_nothing_extra(self):
        group = Group.objects.create(name="den officers")
        group.permissions.add(_ours("den_officer"), self.claim)
        fold.fold_claim(self.historical, None)
        self.assertEqual(self._codenames(group), {"den_officer"})

    def test_states_and_users_are_carried_over_too(self):
        state = State.objects.create(name="Blues", priority=4242)
        state.permissions.add(self.claim.pk)  # by key: the relation is to a proxy
        user = User.objects.create_user("granted_directly")
        user.user_permissions.add(self.claim)
        fold.fold_claim(self.historical, None)
        self.assertEqual(self._codenames(state), {"den_member"})
        self.assertEqual(self._codenames(user, "user_permissions"), {"den_member"})

    def test_the_claim_permission_goes_and_the_labels_name_their_rung(self):
        fold.fold_claim(self.historical, None)
        self.assertFalse(Permission.objects.filter(pk=self.claim.pk).exists())
        self.assertTrue(_ours("den_member").name.startswith("Den user:"))

    def test_a_fresh_install_has_nothing_to_fold(self):
        self.claim.delete()
        fold.fold_claim(self.historical, None)
        self.assertFalse(
            Permission.objects.filter(
                content_type__app_label="holdfast", codename="den_claim"
            ).exists()
        )

    def test_rolling_back_gives_claiming_back_to_every_rung_that_had_it(self):
        operators = Group.objects.create(name="operators")
        operators.permissions.add(_ours("den_member"))
        members = Group.objects.create(name="members")
        members.permissions.add(_ours("den_basic"))
        officers = State.objects.create(name="Officers", priority=4243)
        officers.permissions.add(_ours("den_officer").pk)
        fold.fold_claim(self.historical, None)
        fold.unfold_claim(self.historical, None)
        self.assertEqual(self._codenames(operators), {"den_member", "den_claim"})
        self.assertEqual(self._codenames(members), {"den_basic"})
        self.assertEqual(self._codenames(officers), {"den_officer", "den_claim"})
        self.assertEqual(_ours("den_member").name, fold.OLD_LABELS["den_member"])
