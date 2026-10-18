"""Tactical operations: an anarchy bar for which ones count, and an Ignore
button for the ones that do.

Both fields are plain columns with defaults, so this is one migration: there
is no data step to leave half-applied.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0010_basic_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="holdfastconfig",
            name="mto_anarchy_threshold",
            field=models.PositiveSmallIntegerField(
                default=2,
                help_text="Tactical operations count, and are announced, only on dens at this anarchy level or above; 0 means every operation.",
            ),
        ),
        migrations.AddField(
            model_name="mercenarytacticaloperation",
            name="dismissed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
