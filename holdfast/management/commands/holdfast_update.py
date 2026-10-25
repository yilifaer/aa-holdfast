"""Run every scheduled job once, in the foreground.

The first run after installing, and debugging. It does what beat does, in the
order the schedule does it -- owners before the workforce tracking that reads
what they refreshed, everything before the alerts that judge it -- so after it
finishes every page shows what the schedule would have shown.
"""

from django.core.management.base import BaseCommand

from holdfast import tasks
from holdfast.app_settings import HOLDFAST_PLANET_RESOLVE_PER_RUN
from holdfast.core import alerts, den_sync, esi_sync
from holdfast.core.scope import inactive_reason
from holdfast.models import DenCharacter, Owner


class Command(BaseCommand):
    help = (
        "Run every scheduled Holdfast job once, in the foreground: owners, den "
        "characters, den slots, workforce, planets, cost indices, campaigns, "
        "the sov map, raidable skyhooks, then alerts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner",
            type=int,
            help=(
                "Only sync this owner PK. Unlike the scheduled sync this reaches "
                "a disabled or out-of-scope owner too, so a token can be checked "
                "without switching the owner on."
            ),
        )
        parser.add_argument("--skip-alerts", action="store_true")
        parser.add_argument(
            "--skip-public",
            action="store_true",
            help="Skip the public routes: cost indices, campaigns, sov map, raidable.",
        )
        parser.add_argument(
            "--skip-dens",
            action="store_true",
            help="Skip the den characters' ESI sync.",
        )
        parser.add_argument(
            "--budget",
            type=int,
            help=(
                "Detail calls to spend per owner this run. Defaults to "
                "HOLDFAST_DETAIL_CALLS_PER_RUN. Raising it past the "
                "corp-structure rate limit (300 per 15 minutes per token) "
                "will just get you throttled."
            ),
        )

    def handle(self, *args, **options):
        self._owners(options)
        if not options["skip_dens"]:
            self._den_characters()

        self._step("Den slots", den_sync.sync_den_slots)
        self._step("Workforce tracking", den_sync.track_workforce_high_water)
        self._step(
            "Planet names",
            lambda: esi_sync.resolve_pending_planets(HOLDFAST_PLANET_RESOLVE_PER_RUN),
        )

        if not options["skip_public"]:
            self._step("System cost indices", esi_sync.update_system_costs)
            self._step("Sovereignty campaigns", esi_sync.update_campaigns)
            self._step("Public sovereignty map", esi_sync.update_sov_systems)
            self._step(
                "Raidable skyhooks",
                lambda: len(esi_sync.refresh_raidable_skyhooks()),
            )

        if not options["skip_alerts"]:
            self._step("Alerts", alerts.run_all_checks)

    def _step(self, label, job):
        """One job, reported; a failure is printed and the rest still run,
        as one failing beat task leaves the others alone."""
        self.stdout.write(f"{label} ...")
        try:
            result = job()
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"  failed: {type(exc).__name__}: {exc}"))
            return
        self.stdout.write(self.style.SUCCESS(f"  {result}"))

    def _owners(self, options):
        owners = Owner.objects.active()
        if options["owner"]:
            # The debugging path: picked from every owner, with a warning
            # rather than a refusal when the schedule would skip it.
            owners = Owner.objects.filter(pk=options["owner"])

        for owner in owners:
            self.stdout.write(f"Syncing {owner} ...")
            reason = inactive_reason(owner)
            if reason is not None:
                self.stdout.write(
                    self.style.WARNING(f"  the scheduled sync skips this owner: {reason}")
                )
            try:
                stats = esi_sync.update_owner(owner, detail_budget=options["budget"])
            except Exception as exc:  # noqa: BLE001
                owner.mark_sync(False, f"{type(exc).__name__}: {exc}")
                self.stderr.write(self.style.ERROR(f"  failed: {exc}"))
                continue

            known = stats["sov_hubs"] + stats["skyhooks"]
            done = stats["hub_details"] + stats["skyhook_details"]
            owner.mark_sync(True, "" if stats["complete"] else "Rotation in progress")
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {stats['sov_hubs']} sov hub(s), {stats['skyhooks']} skyhook(s); "
                    f"details refreshed {done}/{known}"
                )
            )
            if stats["rate_limited"]:
                self.stdout.write(
                    self.style.WARNING(
                        "  hit the ESI rate limit; the rest continues next run"
                    )
                )
            elif not stats["complete"]:
                self.stdout.write(
                    "  budget spent; the rest refreshes on following runs"
                )

    def _den_characters(self):
        # Every enabled one, idle or not: the idle back-off spares the
        # scheduled fan-out, and "sync now" means now. Not the characters of
        # people who lost den access, which the fan-out skips too.
        for den_character in tasks.den_characters_in_use(
            DenCharacter.objects.filter(is_enabled=True).select_related(
                "character_ownership__character"
            )
        ):
            self.stdout.write(f"Syncing den character {den_character} ...")
            try:
                # The task body, run here: the same token and rate-limit
                # handling, recorded on the character the same way.
                stats = tasks.update_den_character(den_character.pk)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f"  failed: {type(exc).__name__}: {exc}"))
                continue
            if stats is None:
                den_character.refresh_from_db()
                self.stdout.write(
                    self.style.WARNING(f"  not synced: {den_character.last_error}")
                )
            else:
                self.stdout.write(self.style.SUCCESS(f"  {stats}"))
