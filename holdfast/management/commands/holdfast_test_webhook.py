"""Post a dummy alert so you can confirm the Discord side works."""

from django.core.management.base import BaseCommand
from dhooks_lite import Field

from holdfast.core.alerts import COLOR_INFO, _embed, _send


class Command(BaseCommand):
    help = "Send a test message to every enabled webhook."

    def handle(self, *args, **options):
        _send(
            _embed(
                title="SOV Monitor test",
                description="If you can read this, alerts will reach this channel.",
                color=COLOR_INFO,
                fields=[Field(name="Status", value="working", inline=True)],
            )
        )
        self.stdout.write(self.style.SUCCESS("Sent."))
