"""Let a den character with nothing to watch sit out the fan-out.

charlink ticks this app's entry for every character by default from 0.1.10,
and most characters will never anchor a den. Three ESI calls every twenty
minutes for each of them would be the bulk of this app's traffic, for nothing.
A sync that finds no dens, no operations and no notifications sets this; the
scheduled fan-out skips the character until it passes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0011_mto_threshold_and_dismiss"),
    ]

    operations = [
        migrations.AddField(
            model_name="dencharacter",
            name="idle_until",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
