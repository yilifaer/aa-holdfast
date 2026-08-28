"""Run a full sync in the foreground. Handy for the first run and for debugging."""

from django.core.management.base import BaseCommand

from holdfast.core import alerts, esi_sync
from holdfast.models import Owner


class Command(BaseCommand):
    help = "Sync all owners, refresh the public routes and evaluate alerts."

    def add_arguments(self, parser):
        parser.add_argument("--owner", type=int, help="Only sync this owner PK")
        parser.add_argument("--skip-alerts", action="store_true")
        parser.add_argument("--skip-public", action="store_true")
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
        owners = Owner.objects.filter(is_enabled=True)
        if options["owner"]:
            owners = owners.filter(pk=options["owner"])

        for owner in owners:
            self.stdout.write(f"Syncing {owner} ...")
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

        if not options["skip_public"]:
            self.stdout.write("Refreshing public sovereignty map ...")
            count = esi_sync.update_sov_systems()
            self.stdout.write(self.style.SUCCESS(f"  {count} tracked system(s)"))

            self.stdout.write("Refreshing raidable skyhooks ...")
            rows = esi_sync.get_raidable_skyhooks(force_refresh=True)
            self.stdout.write(self.style.SUCCESS(f"  {len(rows)} raidable"))

        if not options["skip_alerts"]:
            self.stdout.write("Evaluating alerts ...")
            results = alerts.run_all_checks()
            self.stdout.write(self.style.SUCCESS(f"  {results}"))
