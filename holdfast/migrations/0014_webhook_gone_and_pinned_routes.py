"""A webhook Discord has rejected for good, and a route that stays routed.

``Webhook.disabled_reason`` / ``disabled_at`` record why Holdfast switched a
webhook off after Discord answered 404 or 401, so the Webhooks page can say so.

``AlertRoute.is_routed`` remembers that somebody chose channels for a
category, which the ticks alone cannot: deleting a webhook deletes them. A
routed category whose channels are all gone now goes nowhere instead of to
every enabled webhook. The data step marks every route that ticks a webhook
today, so a category routed only to a switched-off webhook stops falling back
from this upgrade on. A route whose webhooks were deleted before it cannot be
told from one nobody touched, and keeps falling back.
"""

from django.db import migrations, models


def mark_routed(apps, schema_editor):
    AlertRoute = apps.get_model("holdfast", "AlertRoute")
    AlertRoute.objects.filter(webhooks__isnull=False).update(is_routed=True)


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0013_charlink_ticked_by_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="webhook",
            name="disabled_reason",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Why Holdfast switched this webhook off. Cleared when it is enabled again.",
                max_length=200,
            ),
        ),
        migrations.AddField(
            model_name="webhook",
            name="disabled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="alertroute",
            name="is_routed",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Somebody chose channels for this category. With none of them "
                    "left working it goes nowhere instead of to every enabled webhook."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="alertroute",
            name="webhooks",
            field=models.ManyToManyField(
                blank=True,
                help_text="Leave empty to use every enabled webhook, until a channel is ticked.",
                related_name="routes",
                to="holdfast.webhook",
            ),
        ),
        # Nothing to undo: reversing drops the column.
        migrations.RunPython(mark_routed, migrations.RunPython.noop),
    ]
