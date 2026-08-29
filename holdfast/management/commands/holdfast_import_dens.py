"""Load a hand-kept den census into the den slots.

Most alliances know who is running their dens long before any of those people
register a token here -- the knowledge lives in a spreadsheet somebody keeps
updating. Typing it back in one slot at a time through the web form is a poor
use of an evening, so it can be imported instead.

The file is CSV with a header row and these columns:

    planet        required, must match a den slot exactly, e.g. "IPX-H5 IV"
    owner         who is running it
    corporation   their corporation, optional
    hostile       "1"/"yes"/"true" if they are not one of ours, optional

Rows whose planet does not match a slot are reported and skipped rather than
guessed at: planet names are full of characters that survey sheets get wrong
(``0`` for ``O``, ``O`` for ``Q``), and silently attaching a record to the
wrong planet is worse than not importing it.

A record imported here is superseded automatically the day its operator
registers a token -- ``DenSlot.status`` prefers a den it can actually read.
"""

import csv

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ...models import DenSlot

TRUE_VALUES = {"1", "y", "yes", "true", "t", "hostile"}


class Command(BaseCommand):
    help = "Import a hand-kept den census from CSV into the den slots."

    def add_arguments(self, parser):
        parser.add_argument("path", help="CSV file to read.")
        parser.add_argument(
            "--user",
            help=(
                "Auth username to credit the records to. Defaults to leaving "
                "the recorder blank."
            ),
        )
        parser.add_argument(
            "--clear-missing",
            action="store_true",
            help=(
                "Clear hand records on slots the file does not mention. Use "
                "this when the file is a full census rather than a patch."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        path = options["path"]
        recorder = None
        if options["user"]:
            try:
                recorder = get_user_model().objects.get(username=options["user"])
            except get_user_model().DoesNotExist as error:
                raise CommandError(f"No such user: {options['user']}") from error

        try:
            with open(path, newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        except OSError as error:
            raise CommandError(f"Cannot read {path}: {error}") from error

        if not rows:
            raise CommandError("The file has no rows.")
        if "planet" not in rows[0]:
            raise CommandError(
                "The file needs a 'planet' column. Found: "
                + ", ".join(rows[0].keys())
            )

        slots = {
            slot.planet_name: slot
            for slot in DenSlot.objects.select_related(
                "skyhook__eve_planet", "skyhook__eve_solar_system"
            )
        }

        recorded, cleared, unmatched, skipped = [], [], [], []
        seen = set()

        for row in rows:
            planet = (row.get("planet") or "").strip()
            if not planet:
                continue
            slot = slots.get(planet)
            if slot is None:
                unmatched.append(planet)
                continue
            seen.add(planet)

            owner = (row.get("owner") or "").strip()
            if not owner:
                # An empty owner is how a census marks free ground. Leave the
                # slot alone; --clear-missing is the way to empty one.
                skipped.append(planet)
                continue

            slot.recorded_den = True
            slot.recorded_hostile = (
                row.get("hostile") or ""
            ).strip().lower() in TRUE_VALUES
            slot.recorded_owner_note = owner[:200]
            slot.recorded_corporation_note = (
                row.get("corporation") or ""
            ).strip()[:200]
            slot.recorded_by = recorder
            slot.recorded_at = timezone.now()
            recorded.append(slot)

        if options["clear_missing"]:
            for planet, slot in slots.items():
                if planet in seen or not slot.recorded_den:
                    continue
                slot.recorded_den = False
                slot.recorded_hostile = False
                slot.recorded_owner_note = ""
                slot.recorded_corporation_note = ""
                slot.recorded_by = None
                slot.recorded_at = None
                cleared.append(slot)

        fields = [
            "recorded_den",
            "recorded_hostile",
            "recorded_owner_note",
            "recorded_corporation_note",
            "recorded_by",
            "recorded_at",
        ]
        if not options["dry_run"]:
            with transaction.atomic():
                DenSlot.objects.bulk_update(recorded + cleared, fields)

        prefix = "would record" if options["dry_run"] else "recorded"
        self.stdout.write(self.style.SUCCESS(f"{prefix} {len(recorded)} den(s)"))
        if cleared:
            verb = "would clear" if options["dry_run"] else "cleared"
            self.stdout.write(f"{verb} {len(cleared)} stale record(s)")
        if skipped:
            self.stdout.write(f"left {len(skipped)} slot(s) alone (no owner given)")
        if unmatched:
            self.stdout.write(
                self.style.WARNING(f"{len(unmatched)} row(s) matched no slot:")
            )
            for planet in unmatched:
                self.stdout.write(f"    {planet}")
