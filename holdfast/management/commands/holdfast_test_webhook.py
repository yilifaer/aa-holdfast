"""Post a dummy alert so you can confirm the Discord side works."""

from dhooks_lite import Field
from django.core.management.base import BaseCommand

from holdfast.core.alerts import COLOR_INFO, SENT, _dispatch, _embed, describe_failure


class Command(BaseCommand):
    help = "Send a test message to every enabled webhook."

    def handle(self, *args, **options):
        delivery = _dispatch(
            _embed(
                title="Holdfast test",
                description="If you can read this, alerts will reach this channel.",
                color=COLOR_INFO,
                fields=[Field(name="Status", value="working", inline=True)],
            )
        )
        if not delivery.outcomes:
            self.stderr.write(self.style.ERROR("Not delivered: no enabled webhook."))
            return
        # One line a webhook. "Sent." as soon as any one of them took it hid
        # the ones Discord refused, or had just been switched off for.
        for hook, outcome in delivery.outcomes:
            if outcome == SENT:
                self.stdout.write(self.style.SUCCESS(f"  sent     {hook.name}"))
            else:
                self.stderr.write(
                    self.style.ERROR(f"  failed   {describe_failure(hook, outcome)}")
                )
        if not delivery.sent:
            self.stderr.write(self.style.ERROR("Not delivered: Discord took none of them."))
