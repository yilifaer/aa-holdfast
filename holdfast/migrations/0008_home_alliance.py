"""Give the install a tracked alliance of its own.

Until now the alliances whose sovereignty this app tracked were read off the
registered corporations: whichever alliances they happened to be in. One
member registering a corporation from another alliance -- a Station Manager alt
was all it took -- made that alliance tracked, pulled its whole sovereignty map
down and posted an ADM alert for every low system it held.

The tracked alliance is a setting now. This adds the column and nothing else;
picking a value for it is 0009. The two are separate on purpose: on MySQL a
column is committed the moment it is added, so a data step failing in the
same migration would leave the column present and the migration unrecorded,
and the retry would die on a duplicate column.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0007_fold_den_claim"),
    ]

    operations = [
        migrations.AddField(
            model_name="holdfastconfig",
            name="home_alliance_id",
            field=models.IntegerField(
                blank=True,
                help_text=(
                    "The alliance whose sovereignty is tracked. Corporations outside it "
                    "cannot be registered and are not synced. Blank until the first "
                    "corporation is registered, which decides it."
                ),
                null=True,
            ),
        ),
    ]
