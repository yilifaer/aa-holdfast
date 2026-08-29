"""Make the theft-window horizon a setting rather than a hardcoded day.

Twenty-four hours suits an alliance whose haulers can be anywhere within a
day. One that stages further out, or plans in longer blocks, wants to see
further; the number was in two places in the code and nowhere in the UI.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0004_claim_revoke_and_dismiss"),
    ]

    operations = [
        migrations.AddField(
            model_name="holdfastconfig",
            name="skyhook_theft_horizon_hours",
            field=models.IntegerField(
                default=24,
                help_text="How far ahead to list theft windows, in hours.",
            ),
        ),
    ]
