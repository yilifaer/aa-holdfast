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
