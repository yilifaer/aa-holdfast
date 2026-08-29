"""Let an approval be taken back, and a decision be cleared off.

Two ends of the same workflow. A manager who granted a slot needs a way to
take it back -- people leave, ground gets reassigned -- and that is a
different thing from a rejection: revoked means it was granted once, and
usually means a den has to come down.

At the applicant's end, a claim that was turned down should not follow them
around their own page forever. Dismissing hides it there and nowhere else;
the row stays for anyone reviewing the history.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0003_drop_mto_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="denclaim",
            name="dismissed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="denclaim",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("withdrawn", "Withdrawn"),
                    ("revoked", "Revoked"),
                ],
                db_index=True,
                default="pending",
                max_length=10,
            ),
        ),
    ]
