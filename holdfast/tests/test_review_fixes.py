"""Tests for six bugs a reviewer found that the suite could not have.

Every one of them was invisible here for the same reason: this alliance's
numbers happen to avoid them. 51 hubs and 413 skyhooks split a budget without
rounding to zero; one alliance never notices that the alert loops ignore whose
alliance a hub belongs to; a settings page nobody had toggled looked like it
worked.

So these do not test the fixes so much as the shapes that hid the bugs.
"""

from datetime import timedelta

from allianceauth.eveonline.models import EveCorporationInfo
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from ..core.esi_sync import TOKENS_PER_CALL, DetailBudget
from ..models import HoldfastConfig, Owner, Skyhook, SovHub


class BudgetStarvationTests(TestCase):
    """One hub among hundreds of skyhooks used to get a budget of zero.

    The split was ``round(budget * hubs / total)``, and Python rounds a half to
    even -- so 100 * 1/200 came out as 0, every run, for ever. That hub's fuel
    was never fetched, so it never alerted, and nothing said why.
    """

    def setUp(self):
        corporation = EveCorporationInfo.objects.create(
            corporation_id=98000010,
            corporation_name="Small Alliance Holdings",
            corporation_ticker="SAH",
            member_count=12,
        )
        self.owner = Owner.objects.create(corporation=corporation)

    def _build(self, hubs, skyhooks):
        now = timezone.now()
        for index in range(hubs):
            SovHub.objects.create(
                hub_id=2_000_000 + index,
                owner=self.owner,
                solar_system_id=30000000 + index,
                detail_updated_at=now - timedelta(hours=10),
            )
        for index in range(skyhooks):
            Skyhook.objects.create(
                skyhook_id=3_000_000 + index,
                owner=self.owner,
                planet_id=40000000 + index,
                detail_updated_at=now - timedelta(hours=1),
            )

    def test_the_one_hub_is_not_starved_by_two_hundred_skyhooks(self):
        """The exact shape that produced round(0.5) == 0."""
        from ..core.esi_sync import refresh_details

        self._build(hubs=1, skyhooks=199)
        calls = []

        def fake_hub(owner, token, hub, memo):
            calls.append(("hub", hub.hub_id))

        def fake_hook(owner, token, skyhook, memo):
            calls.append(("skyhook", skyhook.skyhook_id))

        import holdfast.core.esi_sync as module

        original = module._fetch_sov_hub_detail, module._fetch_skyhook_detail
        module._fetch_sov_hub_detail, module._fetch_skyhook_detail = fake_hub, fake_hook
        try:
            hubs_done, _hooks_done = refresh_details(
                self.owner, None, DetailBudget(10 * TOKENS_PER_CALL), {}
            )
        finally:
            module._fetch_sov_hub_detail, module._fetch_skyhook_detail = original

        self.assertEqual(hubs_done, 1, "the single hub never got refreshed")
        self.assertEqual(calls[0][0], "hub", "the stalest row should go first")

    def test_an_alliance_with_no_skyhooks_still_spends_its_budget(self):
        from ..core.esi_sync import refresh_details

        self._build(hubs=4, skyhooks=0)
        import holdfast.core.esi_sync as module

        original = module._fetch_sov_hub_detail
        module._fetch_sov_hub_detail = lambda *a, **k: None
        try:
            hubs_done, hooks_done = refresh_details(
                self.owner, None, DetailBudget(10 * TOKENS_PER_CALL), {}
            )
        finally:
            module._fetch_sov_hub_detail = original
        self.assertEqual((hubs_done, hooks_done), (4, 0))


class BudgetAccountingTests(SimpleTestCase):
    """ESI charges two tokens for a successful call, not one.

    The budget was counting calls and calling them tokens, which put every
    estimate in the README and the comments out by a factor of two -- in the
    direction that gets an install rate limited.
    """

    def test_a_call_costs_two_tokens(self):
        budget = DetailBudget(10)
        self.assertTrue(budget.take())
        self.assertEqual(budget.spent, 2)
        self.assertEqual(budget.remaining, 8)

    def test_a_budget_that_cannot_afford_a_whole_call_refuses_it(self):
        """Spending the last token on half a request helps nobody."""
        budget = DetailBudget(1)
        self.assertFalse(budget.take())
        self.assertEqual(budget.spent, 0)

    def test_the_default_leaves_most_of_the_bucket_for_other_apps(self):
        from ..app_settings import HOLDFAST_DETAIL_CALLS_PER_RUN

        spend = HOLDFAST_DETAIL_CALLS_PER_RUN * TOKENS_PER_CALL + 2 * TOKENS_PER_CALL
        self.assertLess(
            spend,
            300 * 0.5,
            "a default run should stay under half the corp-structure bucket, "
            "which is shared with every other app using the same token",
        )


class SettingsActuallyApplyTests(TestCase):
    """Eight of sixteen settings were write-only.

    A switch that says "off" and changes nothing is worse than no switch: it
    spends someone's afternoon looking for the alert they think they disabled.
    """

    def setUp(self):
        self.config = HoldfastConfig.get_solo()

    def test_turning_off_the_upgrade_warning_turns_it_off(self):
        from ..core.alerts import check_hub_upgrades

        self.config.notify_upgrade_offline = False
        self.config.save()
        self.assertEqual(check_hub_upgrades(), 0)

    def test_clearing_the_adm_threshold_disables_the_check(self):
        from ..core.alerts import check_adm

        self.config.adm_alert_threshold = None
        self.config.save()
        self.assertEqual(check_adm(), 0)

    def test_turning_off_the_siphon_warning_turns_both_of_them_off(self):
        from ..core.den_alerts import check_siphoned_skyhooks, check_workforce_siphon

        self.config.notify_den_skyhook_impact = False
        self.config.save()
        self.assertEqual(check_workforce_siphon(), 0)
        self.assertEqual(check_siphoned_skyhooks(), 0)

    def test_the_theft_lead_time_comes_from_the_settings_page(self):
        """It used to come from local.py, so the page did nothing."""
        import inspect

        from ..core import alerts

        source = inspect.getsource(alerts.check_skyhook_theft)
        self.assertIn("config.skyhook_theft_lead_minutes", source)
        self.assertNotIn("HOLDFAST_SKYHOOK_THEFT_LEAD_MINUTES", source)


class CrossAllianceWriteTests(TestCase):
    """The den write paths looked up rows by primary key alone.

    The list pages filtered by alliance all along, so the isolation looked
    complete from the outside. Anyone who could guess an id could claim,
    approve, revoke or overwrite a site belonging to somebody else -- which is
    why a review found this and a single-alliance install never would.
    """

    def setUp(self):
        from allianceauth.authentication.models import CharacterOwnership
        from allianceauth.eveonline.models import (
            EveAllianceInfo,
            EveCharacter,
        )
        from django.contrib.auth.models import Permission, User

        from ..models import DenSlot

        def alliance(alliance_id, corporation_id, name):
            info = EveAllianceInfo.objects.create(
                alliance_id=alliance_id,
                alliance_name=f"{name} Alliance",
                alliance_ticker=name[:4].upper(),
                executor_corp_id=corporation_id,
            )
            corporation = EveCorporationInfo.objects.create(
                corporation_id=corporation_id,
                corporation_name=f"{name} Corp",
                corporation_ticker=name[:4].upper(),
                member_count=5,
                alliance=info,
            )
            owner = Owner.objects.create(corporation=corporation)
            skyhook = Skyhook.objects.create(
                skyhook_id=5_000_000 + corporation_id,
                owner=owner,
                planet_id=41_000_000 + corporation_id,
            )
            return info, corporation, DenSlot.objects.create(skyhook=skyhook)

        _ours, our_corp, self.our_slot = alliance(99000010, 98000020, "Ours")
        _theirs, _their_corp, self.their_slot = alliance(99000011, 98000021, "Theirs")

        self.user = User.objects.create_user("insider")
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="holdfast")
        )
        self.user = type(self.user).objects.get(pk=self.user.pk)
        character = EveCharacter.objects.create(
            character_id=92_000_001,
            character_name="Insider",
            corporation_id=our_corp.corporation_id,
            corporation_name=our_corp.corporation_name,
            corporation_ticker=our_corp.corporation_ticker,
            alliance_id=99000010,
            alliance_name="Ours Alliance",
        )
        CharacterOwnership.objects.create(
            user=self.user, character=character, owner_hash="insider-hash"
        )
        profile = self.user.profile
        profile.main_character = character
        profile.save()

    def _post(self, url, data=None):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory
        from django.urls import resolve

        match = resolve(url)
        request = RequestFactory().post(url, data or {})
        request.user = self.user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        return match.func(request, *match.args, **match.kwargs)

    def test_our_own_slot_is_reachable(self):
        """The control: without this, a 404 below would prove nothing."""
        response = self._post(
            f"/holdfast/den/slot/{self.our_slot.pk}/record/",
            {"present": "1", "owner_note": "someone"},
        )
        self.assertEqual(response.status_code, 302)
        self.our_slot.refresh_from_db()
        self.assertTrue(self.our_slot.recorded_den)

    def test_recording_a_den_on_another_alliance_s_ground_is_refused(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self._post(
                f"/holdfast/den/slot/{self.their_slot.pk}/record/",
                {"present": "1", "owner_note": "not mine to say"},
            )
        self.their_slot.refresh_from_db()
        self.assertFalse(self.their_slot.recorded_den)

    def test_the_scoped_queryset_excludes_another_alliance(self):
        """The boundary itself, which is what every write path looks through.

        ``claim_slot`` is checked here rather than by POSTing to it: it sits
        behind ``@token_required``, so a request without an EVE token is
        redirected to SSO long before it reaches the lookup, and a test that
        drove it end to end would be testing django-esi.
        """
        import inspect

        from ..views import den
        from ..views.common import visible_slots

        visible = set(visible_slots(self.user).values_list("pk", flat=True))
        self.assertIn(self.our_slot.pk, visible)
        self.assertNotIn(self.their_slot.pk, visible)

        source = inspect.getsource(den.claim_slot)
        self.assertIn("visible_slots(request.user)", source)
        self.assertNotIn("get_object_or_404(DenSlot", source)

    def test_deciding_another_alliance_s_claim_is_refused(self):
        from allianceauth.eveonline.models import EveCharacter
        from django.contrib.auth.models import User
        from django.http import Http404

        from ..models import DenClaim

        outsider = User.objects.create_user("outsider")
        character = EveCharacter.objects.create(
            character_id=92_000_002,
            character_name="Outsider",
            corporation_id=98000021,
            corporation_name="Theirs Corp",
            corporation_ticker="THEI",
        )
        claim = DenClaim.objects.create(
            slot=self.their_slot, user=outsider, character=character
        )
        with self.assertRaises(Http404):
            self._post(f"/holdfast/den/claim/{claim.pk}/approve/")
        claim.refresh_from_db()
        self.assertEqual(claim.status, DenClaim.Status.PENDING)
