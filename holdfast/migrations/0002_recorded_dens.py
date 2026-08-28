"""Generalise the hand-written den record from hostile-only to any den.

ESI shows a character only its own dens, so the app originally assumed the
dens it could not read were hostile ones. In practice most of them are
friendly: an alliance knows exactly who is running a den long before that
person registers a token here. The record is the same either way, so the
fields lose the ``hostile_`` prefix and gain a flag saying which kind it is.

Written by hand rather than generated: the autodetector cannot tell a rename
from a drop-and-add without being asked, and answering wrong silently discards
whatever anyone had already recorded.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def mark_existing_as_hostile(apps, schema_editor):
    """Everything recorded under the old field was a hostile den by definition."""
    DenSlot = apps.get_model("holdfast", "DenSlot")
    DenSlot.objects.filter(recorded_den=True).update(recorded_hostile=True)


def unmark(apps, schema_editor):
    """Going back, only the hostile records have a field to live in."""
    DenSlot = apps.get_model("holdfast", "DenSlot")
    DenSlot.objects.filter(recorded_hostile=False).update(recorded_den=False)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("holdfast", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="denslot",
            old_name="hostile_den_recorded",
            new_name="recorded_den",
        ),
        migrations.RenameField(
            model_name="denslot",
            old_name="hostile_owner_note",
            new_name="recorded_owner_note",
        ),
        migrations.RenameField(
            model_name="denslot",
            old_name="hostile_recorded_by",
            new_name="recorded_by",
        ),
        migrations.RenameField(
            model_name="denslot",
            old_name="hostile_recorded_at",
            new_name="recorded_at",
        ),
        migrations.AddField(
            model_name="denslot",
            name="recorded_hostile",
            field=models.BooleanField(
                default=False,
                help_text="The recorded den belongs to someone outside the alliance.",
            ),
        ),
        migrations.AddField(
            model_name="denslot",
            name="recorded_corporation_note",
            field=models.CharField(
                blank=True, help_text="Their corporation, if known.", max_length=200
            ),
        ),
        migrations.AlterField(
            model_name="denslot",
            name="recorded_den",
            field=models.BooleanField(
                default=False,
                help_text="A den is sitting here that ESI does not show us.",
            ),
        ),
        migrations.AlterField(
            model_name="denslot",
            name="recorded_owner_note",
            field=models.CharField(
                blank=True,
                help_text="Who is running it, as far as anyone knows.",
                max_length=200,
            ),
        ),
        migrations.AlterField(
            model_name="denslot",
            name="recorded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="denslot",
            name="recorded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_as_hostile, unmark),
    ]
