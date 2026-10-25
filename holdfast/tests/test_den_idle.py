"""charlink ticks our entry for everyone; idle characters make that affordable.

Most characters an alliance links will never anchor a den. Listing each of
them every twenty minutes -- dens, operations, notifications: three calls --
would be the bulk of this app's ESI traffic, spent on nothing. A character
whose sync found nothing sits out the fan-out until idle_until passes; one
that has something again is woken at once.
"""

import ast
import pathlib
from datetime import timedelta
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import Permission, User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .. import tasks
from ..app_settings import HOLDFAST_DEN_IDLE_HOURS
from ..core import den_sync
from ..models import DenCharacter

HOOK = pathlib.Path(__file__).resolve().parent.parent / "thirdparty" / "charlink_hook.py"


def _den_character(tag):
    user = User.objects.create_user(f"idle_{tag}")
    # The fan-out polls only people who may still use dens.
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="holdfast", codename="basic_access")
    )
    character = EveCharacter.objects.create(
        character_id=96_000_000 + tag,
        character_name=f"Idle {tag}",
        corporation_id=98_000_500,
        corporation_name="Idle Corp",
        corporation_ticker="IDLE",
    )
    ownership = CharacterOwnership.objects.create(
        user=user, character=character, owner_hash=f"idle-hash-{tag}"
    )
    return DenCharacter.objects.create(character_ownership=ownership)


class NoteIdleTests(TestCase):
    def test_a_sync_that_found_nothing_backs_the_character_off(self):
        den_character = _den_character(1)
        before = timezone.now()
        den_sync.note_idle(den_character, {"dens": 0, "operations": 0, "new_events": 0})
        den_character.refresh_from_db()
        self.assertIsNotNone(den_character.idle_until)
        expected = before + timedelta(hours=HOLDFAST_DEN_IDLE_HOURS)
        self.assertLess(abs((den_character.idle_until - expected).total_seconds()), 60)

    def test_anything_at_all_wakes_it(self):
        den_character = _den_character(2)
        den_character.idle_until = timezone.now() + timedelta(hours=3)
        den_character.save(update_fields=["idle_until"])
        for stats in ({"dens": 1}, {"operations": 1}, {"new_events": 1}):
            den_sync.note_idle(den_character, {"dens": 0, "operations": 0, "new_events": 0, **stats})
            den_character.refresh_from_db()
            self.assertIsNone(den_character.idle_until, stats)
            den_character.idle_until = timezone.now() + timedelta(hours=3)
            den_character.save(update_fields=["idle_until"])

    def test_an_unchanged_verdict_costs_no_write(self):
        den_character = _den_character(3)
        with mock.patch.object(DenCharacter, "save") as save:
            den_sync.note_idle(den_character, {"dens": 2, "operations": 0, "new_events": 0})
        save.assert_not_called()


class FanOutTests(TestCase):
    """update_all_den_characters queues the due ones and only those."""

    def _queued(self):
        queued = []
        with mock.patch.object(
            tasks.update_den_character, "apply_async", side_effect=lambda args, **_: queued.append(args[0])
        ):
            tasks.update_all_den_characters()
        return set(queued)

    def test_idle_characters_are_skipped_until_their_time_comes(self):
        due = _den_character(4)
        idle = _den_character(5)
        idle.idle_until = timezone.now() + timedelta(hours=1)
        idle.save(update_fields=["idle_until"])
        woken = _den_character(6)
        woken.idle_until = timezone.now() - timedelta(minutes=1)
        woken.save(update_fields=["idle_until"])
        self.assertEqual(self._queued(), {due.pk, woken.pk})

    def test_a_disabled_character_stays_out_regardless(self):
        off = _den_character(7)
        off.is_enabled = False
        off.save(update_fields=["is_enabled"])
        self.assertEqual(self._queued(), set())


class CharlinkDefaultTests(SimpleTestCase):
    def test_the_stored_default_is_flipped_only_where_charlink_exists(self):
        """charlink keeps the tick in its own table and reads our flag once,
        so 0013 flips the stored row. Without charlink -- these settings, and
        any install that never added it -- it must do nothing and raise
        nothing."""
        import importlib

        migration = importlib.import_module("holdfast.migrations.0013_charlink_ticked_by_default")
        # No charlink here, so it must return before it ever looks at the
        # schema editor -- which is why None is a fair stand-in for it.
        migration.tick_by_default(None, None)

    def test_the_entry_is_ticked_by_default(self):
        """A member who links everything in one go links this too. Read as
        text: charlink is optional and absent in CI."""
        tree = ast.parse(HOOK.read_text(encoding="utf-8"))
        values = [
            ast.literal_eval(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "LoginImport"
            for keyword in node.keywords
            if keyword.arg == "default_initial_selection"
        ]
        self.assertEqual(values, [True])
