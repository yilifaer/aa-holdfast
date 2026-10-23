"""Post a dummy alert so you can confirm the Discord side works."""

from dhooks_lite import Field
from django.core.management.base import BaseCommand

from holdfast.core.alerts import COLOR_INFO, _embed, _send


class Command(BaseCommand):
    help = "Send a test message to every enabled webhook."

    def handle(self, *args, **options):
        delivered = _send(
            _embed(
                title="Holdfast test",
                description="If you can read this, alerts will reach this channel.",
                color=COLOR_INFO,
                fields=[Field(name="Status", value="working", inline=True)],
            )
        )
        if delivered:
            self.stdout.write(self.style.SUCCESS("Sent."))
        else:
            self.stderr.write(
                self.style.ERROR(
                    "Not delivered: no enabled webhook, or Discord refused every "
                    "post. The log says which."
                )
            )
