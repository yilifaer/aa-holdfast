"""Fire one sample alert per category so routing can be checked.

Goes through the same delivery path as a real alert, so a pass here means the
routing, the channel and the enable switches are all genuinely working -- not
that a separate test-only code path happens to run.
"""

from django.core.management.base import BaseCommand

from holdfast.core.alerts import send_test, send_test_for_section
from holdfast.models import CATEGORY_SECTIONS, AlertCategory


class Command(BaseCommand):
    help = "Send a test Discord alert for every alert category, or one section."

    def add_arguments(self, parser):
        parser.add_argument(
            "--section",
            choices=sorted(set(CATEGORY_SECTIONS.values())),
            help="Only test one section's categories.",
        )
        parser.add_argument(
            "--category",
            choices=[str(c) for c in AlertCategory],
            help="Only test one category.",
        )

    def handle(self, *args, **options):
        if options["category"]:
            self._report(options["category"], send_test(options["category"]))
            return

        sections = (
            [options["section"]]
            if options["section"]
            else sorted(set(CATEGORY_SECTIONS.values()))
        )
        for section in sections:
            self.stdout.write(f"--- {section} ---")
            for category, delivery in send_test_for_section(section).items():
                self._report(category, delivery)

    def _report(self, category, delivery):
        # "No channel configured" covered a refusal from Discord too, which
        # sent people to the routing table when the webhook was the problem.
        why = "; ".join(delivery.failures)
        if delivery.sent and not why:
            self.stdout.write(self.style.SUCCESS(f"  sent     {category}"))
        elif delivery.sent:
            self.stdout.write(self.style.WARNING(f"  partly   {category} ({why})"))
        elif delivery.outcomes:
            self.stdout.write(self.style.ERROR(f"  failed   {category} ({why})"))
        else:
            self.stdout.write(self.style.WARNING(f"  skipped  {category} ({why})"))
