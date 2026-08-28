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
            ok = send_test(options["category"])
            self._report(options["category"], ok)
            return

        sections = (
            [options["section"]]
            if options["section"]
            else sorted(set(CATEGORY_SECTIONS.values()))
        )
        for section in sections:
            self.stdout.write(f"--- {section} ---")
            for category, ok in send_test_for_section(section).items():
                self._report(category, ok)

    def _report(self, category, ok):
        if ok:
            self.stdout.write(self.style.SUCCESS(f"  sent     {category}"))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"  skipped  {category} "
                    "(no channel configured, or the category or section is switched off)"
                )
            )
