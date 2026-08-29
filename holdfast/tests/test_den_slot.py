"""Tests for how a den slot decides what is sitting on it.

A slot learns about its den from two places that can disagree: a token, which
is live but only ever covers the operator's own dens, and a note somebody
typed, which covers everyone but goes stale silently. Which one wins decides
whether a slot looks claimable, so it is worth pinning down.
"""

from allianceauth.eveonline.models import EveCorporationInfo
from django.test import TestCase

from ..models import DenSlot, MercenaryDen, Owner, Skyhook


class DenSlotStatusTests(TestCase):
    def setUp(self):
        corporation = EveCorporationInfo.objects.create(
            corporation_id=98000001,
            corporation_name="Invidia Administrative",
            corporation_ticker="INVA",
            member_count=42,
        )
        owner = Owner.objects.create(corporation=corporation)
        self.skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_001,
            owner=owner,
            planet_id=40_000_001,
            effective_workforce=8700,
        )
        self.slot = DenSlot.objects.create(skyhook=self.skyhook)

    def test_an_empty_slot_is_free_and_claimable(self):
        self.assertEqual(self.slot.status, DenSlot.Status.FREE)
        self.assertTrue(self.slot.is_claimable)
        self.assertIsNone(self.slot.holder_name)

    def test_a_friendly_hand_record_is_recorded_not_hostile(self):
        """The common case: we know who runs it, they just have no token here."""
        self.slot.recorded_den = True
        self.slot.recorded_owner_note = "sakuya ly"
        self.slot.recorded_corporation_note = "Golden Fleece"
        self.slot.save()

        self.assertEqual(self.slot.status, DenSlot.Status.RECORDED)
        self.assertEqual(self.slot.holder_name, "sakuya ly")
        self.assertEqual(self.slot.holder_corporation, "Golden Fleece")

    def test_a_hostile_hand_record_reads_as_hostile(self):
        self.slot.recorded_den = True
        self.slot.recorded_hostile = True
        self.slot.save()

        self.assertEqual(self.slot.status, DenSlot.Status.HOSTILE)
        self.assertEqual(self.slot.holder_name, "unknown (hostile)")

    def test_neither_kind_of_record_leaves_the_slot_claimable(self):
        for hostile in (False, True):
            with self.subTest(hostile=hostile):
                self.slot.recorded_den = True
                self.slot.recorded_hostile = hostile
                self.slot.save()
                self.assertFalse(self.slot.is_claimable)

    def test_a_den_we_can_read_outranks_a_note_somebody_typed(self):
        """The whole point of a hand record is that it expires by itself.

        Once its operator registers a token the note is stale by definition,
        and nobody is going to remember to go back and clear it. The live den
        has to win or every imported census would need hand-weeding later.
        """
        self.slot.recorded_den = True
        self.slot.recorded_owner_note = "sakuya ly"
        self.slot.save()

        den_character = self._den_character()
        MercenaryDen.objects.create(
            den_id=2_000_000_000_001,
            den_character=den_character,
            planet_id=self.skyhook.planet_id,
            skyhook_id=self.skyhook.skyhook_id,
            slot=self.slot,
            state=MercenaryDen.State.RUNNING,
        )

        self.assertEqual(self.slot.status, DenSlot.Status.ANCHORED)

    def test_a_reinforced_den_still_counts_as_anchored(self):
        """Paused means reinforced, not gone. The ground is still taken."""
        MercenaryDen.objects.create(
            den_id=2_000_000_000_002,
            den_character=self._den_character(),
            planet_id=self.skyhook.planet_id,
            skyhook_id=self.skyhook.skyhook_id,
            slot=self.slot,
            state=MercenaryDen.State.PAUSED,
        )

        self.assertEqual(self.slot.status, DenSlot.Status.ANCHORED)
        self.assertFalse(self.slot.is_claimable)

    def _den_character(self):
        """A registered operator, built the way Auth builds one.

        A den character hangs off a CharacterOwnership rather than a user
        directly, because that is what proves the token belongs to them.
        """
        from allianceauth.authentication.models import CharacterOwnership
        from allianceauth.eveonline.models import EveCharacter
        from django.contrib.auth.models import User

        from ..models import DenCharacter

        user = User.objects.create_user("operator")
        character = EveCharacter.objects.create(
            character_id=90000001,
            character_name="sakuya ly",
            corporation_id=98000002,
            corporation_name="Golden Fleece",
            corporation_ticker="GF",
        )
        ownership = CharacterOwnership.objects.create(
            user=user, character=character, owner_hash="test-owner-hash"
        )
        return DenCharacter.objects.create(character_ownership=ownership)


class OperationAttentionTests(TestCase):
    """An operation on your own den has to reach you.

    It expires on a clock whether or not anyone logs in, and the first version
    of this counted only operations in the ``Available`` state -- so a member
    who had actually started one saw "All clear" until it ran out.
    """

    def setUp(self):
        from django.contrib.auth.models import Permission, User
        from django.core.cache import cache

        from ..models import DenCharacter, MercenaryDen

        cache.clear()
        corporation = EveCorporationInfo.objects.create(
            corporation_id=98000003,
            corporation_name="Golden Fleece",
            corporation_ticker="GF",
            member_count=10,
        )
        owner = Owner.objects.create(corporation=corporation)
        skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_002,
            owner=owner,
            planet_id=40_000_002,
            effective_workforce=7812,
        )
        self.slot = DenSlot.objects.create(skyhook=skyhook)

        from allianceauth.authentication.models import CharacterOwnership
        from allianceauth.eveonline.models import EveCharacter

        self.user = User.objects.create_user("operator2")
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="holdfast", codename="den_basic"
            )
        )
        self.user = User.objects.get(pk=self.user.pk)  # drop the perm cache
        ownership = CharacterOwnership.objects.create(
            user=self.user,
            character=EveCharacter.objects.create(
                character_id=90000002,
                character_name="operator two",
                corporation_id=98000003,
                corporation_name="Golden Fleece",
                corporation_ticker="GF",
            ),
            owner_hash="test-owner-hash-2",
        )
        self.den_character = DenCharacter.objects.create(character_ownership=ownership)
        self.den = MercenaryDen.objects.create(
            den_id=2_000_000_000_003,
            den_character=self.den_character,
            planet_id=skyhook.planet_id,
            skyhook_id=skyhook.skyhook_id,
            slot=self.slot,
            state=MercenaryDen.State.RUNNING,
        )

    def _count(self):
        from django.core.cache import cache

        from ..core.attention import den_count

        cache.clear()
        return den_count(self.user)

    def _operation(self, state, hours):
        from datetime import timedelta

        from django.utils import timezone

        from ..models import MercenaryTacticalOperation

        return MercenaryTacticalOperation.objects.create(
            operation_id=f"op-{state}-{hours}",
            den_character=self.den_character,
            den=self.den,
            mercenary_den_id=self.den.den_id,
            dungeon_type_id=12367,
            state=state,
            expires=timezone.now() + timedelta(hours=hours),
        )

    def test_a_running_den_with_nothing_on_it_is_quiet(self):
        self.assertEqual(self._count(), 0)

    def test_both_live_states_count(self):
        from ..models import MercenaryTacticalOperation

        for state in (
            MercenaryTacticalOperation.State.AVAILABLE,
            MercenaryTacticalOperation.State.STARTED,
        ):
            with self.subTest(state=state):
                MercenaryTacticalOperation.objects.all().delete()
                self._operation(state, 48)
                self.assertEqual(self._count(), 1)

    def test_finished_and_expired_operations_do_not_count(self):
        from ..models import MercenaryTacticalOperation

        self._operation(MercenaryTacticalOperation.State.COMPLETED, 48)
        self._operation(MercenaryTacticalOperation.State.STARTED, -1)
        self.assertEqual(self._count(), 0)

    def test_a_siphoning_den_of_your_own_counts(self):
        """Anarchy 2 is where a den starts taking from the ground under it."""
        from ..models import EvolutionLevel

        self.den.anarchy_level = EvolutionLevel.L2
        self.den.save()
        self.assertEqual(self._count(), 1)

        self.den.anarchy_level = EvolutionLevel.L1
        self.den.save()
        self.assertEqual(self._count(), 0)

    def test_an_operation_is_named_by_its_number_not_a_wrong_name(self):
        """dungeon_type_id indexes dungeons, so the type tables lie about it."""
        from ..models import MercenaryTacticalOperation

        operation = self._operation(MercenaryTacticalOperation.State.STARTED, 48)
        self.assertEqual(operation.type_name, "Operation #12367")


class HolderLabelTests(TestCase):
    """One string for "who is on this slot", shared by the pings and the boards.

    A Discord embed that says "no den of ours is anchored here" next to a field
    naming the operator reads as a contradiction. Both now ask the slot.
    """

    def setUp(self):
        corporation = EveCorporationInfo.objects.create(
            corporation_id=98000004,
            corporation_name="Invidia Administrative",
            corporation_ticker="INVA",
            member_count=42,
        )
        owner = Owner.objects.create(corporation=corporation)
        skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_004,
            owner=owner,
            planet_id=40_000_004,
            effective_workforce=8316,
        )
        self.slot = DenSlot.objects.create(skyhook=skyhook)

    def test_nobody_on_it_reads_as_unknown(self):
        self.assertEqual(self.slot.holder_label, "unknown")
        self.assertEqual(self.slot.holder_source, "")

    def test_a_hand_record_is_marked_manual(self):
        self.slot.recorded_den = True
        self.slot.recorded_owner_note = "Nah vi"
        self.slot.recorded_corporation_note = "Ether Element"
        self.slot.save()

        self.assertEqual(self.slot.holder_label, "Nah vi -- Ether Element (manual)")

    def test_a_record_without_a_corporation_still_names_the_person(self):
        self.slot.recorded_den = True
        self.slot.recorded_owner_note = "Nah vi"
        self.slot.save()

        self.assertEqual(self.slot.holder_label, "Nah vi (manual)")


class ClaimLifecycleTests(TestCase):
    """Revoking and dismissing: the two ends of a decision that is not final."""

    def setUp(self):
        from allianceauth.authentication.models import CharacterOwnership
        from allianceauth.eveonline.models import EveCharacter
        from django.contrib.auth.models import User

        from ..models import DenCharacter, DenClaim, MercenaryDen

        self.DenClaim = DenClaim
        self.MercenaryDen = MercenaryDen
        corporation = EveCorporationInfo.objects.create(
            corporation_id=98000005,
            corporation_name="Golden Fleece",
            corporation_ticker="GF",
            member_count=10,
        )
        owner = Owner.objects.create(corporation=corporation)
        self.skyhook = Skyhook.objects.create(
            skyhook_id=1_000_000_000_005,
            owner=owner,
            planet_id=40_000_005,
            effective_workforce=9200,
        )
        self.slot = DenSlot.objects.create(skyhook=self.skyhook)
        self.user = User.objects.create_user("applicant")
        self.character = EveCharacter.objects.create(
            character_id=90000005,
            character_name="rmand A",
            corporation_id=98000005,
            corporation_name="Golden Fleece",
            corporation_ticker="GF",
        )
        self.ownership = CharacterOwnership.objects.create(
            user=self.user, character=self.character, owner_hash="test-hash-5"
        )
        self.den_character = DenCharacter.objects.create(
            character_ownership=self.ownership
        )
        self.claim = DenClaim.objects.create(
            slot=self.slot, user=self.user, character=self.character
        )

    def test_a_pending_claim_is_not_dismissable(self):
        """Still open means withdraw, not clear."""
        self.assertTrue(self.claim.is_open)
        self.assertFalse(self.claim.is_dismissable)

    def test_a_rejected_claim_can_be_cleared_once(self):
        self.claim.status = self.DenClaim.Status.REJECTED
        self.claim.save()
        self.assertTrue(self.claim.is_dismissable)

        from django.utils import timezone

        self.claim.dismissed_at = timezone.now()
        self.claim.save()
        self.assertFalse(self.claim.is_dismissable)

    def test_revoking_frees_the_slot_when_nothing_is_anchored(self):
        self.claim.status = self.DenClaim.Status.APPROVED
        self.claim.save()
        self.assertEqual(self.slot.status, DenSlot.Status.ASSIGNED)

        self.claim.status = self.DenClaim.Status.REVOKED
        self.claim.save()
        self.assertEqual(self.slot.status, DenSlot.Status.FREE)
        self.assertTrue(self.slot.is_claimable)

    def test_revoking_does_not_unanchor_a_den_that_is_still_there(self):
        """Only the operator can take a den down, so the slot keeps saying so."""
        self.claim.status = self.DenClaim.Status.REVOKED
        self.claim.save()
        self.MercenaryDen.objects.create(
            den_id=2_000_000_000_005,
            den_character=self.den_character,
            planet_id=self.skyhook.planet_id,
            skyhook_id=self.skyhook.skyhook_id,
            slot=self.slot,
            state=self.MercenaryDen.State.RUNNING,
        )

        self.assertEqual(self.slot.status, DenSlot.Status.ANCHORED)
        self.assertFalse(self.slot.is_claimable)
        self.assertTrue(self.slot.awaiting_removal)

    def test_an_approved_slot_with_a_den_is_not_awaiting_removal(self):
        self.claim.status = self.DenClaim.Status.APPROVED
        self.claim.save()
        self.MercenaryDen.objects.create(
            den_id=2_000_000_000_006,
            den_character=self.den_character,
            planet_id=self.skyhook.planet_id,
            skyhook_id=self.skyhook.skyhook_id,
            slot=self.slot,
            state=self.MercenaryDen.State.RUNNING,
        )

        self.assertFalse(self.slot.awaiting_removal)


class FirstSyncWindowTests(TestCase):
    """The first thing that happens to a new operator's den must still reach them.

    Suppressing every notification on an operator's first sync kept days of
    history out of Discord, and took the live event with it -- the one they
    registered to hear about.
    """

    def setUp(self):
        from allianceauth.authentication.models import CharacterOwnership
        from allianceauth.eveonline.models import EveCharacter
        from django.contrib.auth.models import User

        from ..models import DenCharacter

        user = User.objects.create_user("operator3")
        ownership = CharacterOwnership.objects.create(
            user=user,
            character=EveCharacter.objects.create(
                character_id=90000003,
                character_name="operator three",
                corporation_id=98000006,
                corporation_name="Golden Fleece",
                corporation_ticker="GF",
            ),
            owner_hash="test-owner-hash-3",
        )
        self.den_character = DenCharacter.objects.create(character_ownership=ownership)

    def _cutoff(self):
        """The cutoff sync_notifications would compute for this operator."""
        from datetime import timedelta

        from django.utils import timezone

        from ..app_settings import (
            HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES,
            HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS,
        )
        from ..models import DenEvent

        now = timezone.now()
        cutoff = now - timedelta(hours=HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS)
        if not DenEvent.objects.filter(den_character=self.den_character).exists():
            cutoff = max(
                cutoff,
                now - timedelta(minutes=HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES),
            )
        return now, cutoff

    def test_an_attack_happening_now_survives_the_first_sync(self):
        now, cutoff = self._cutoff()
        self.assertLess(cutoff, now, "a live event must fall inside the window")

    def test_yesterdays_history_is_still_suppressed_on_a_first_sync(self):
        from datetime import timedelta

        now, cutoff = self._cutoff()
        self.assertGreater(
            cutoff, now - timedelta(hours=3), "first sync must stay a narrow window"
        )

    def test_the_window_widens_once_the_operator_has_history(self):
        from datetime import timedelta

        from django.utils import timezone

        from ..models import DenEvent

        DenEvent.objects.create(
            notification_id=123456,
            den_character=self.den_character,
            kind=DenEvent.Kind.ATTACKED,
            timestamp=timezone.now(),
        )
        now, cutoff = self._cutoff()
        self.assertLess(
            cutoff, now - timedelta(hours=3), "an established operator gets the full age"
        )
