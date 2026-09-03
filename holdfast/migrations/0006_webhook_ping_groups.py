"""Let a webhook mention Auth groups instead of @here.

@here and @everyone were the only two pings a webhook had, and on an alliance
Discord both reach far more people than care. The first install outside this
alliance asked in its first week: "our skyhook team is the only one which
needs a ping in the proper channel. Here or everyone gets spammy for others."

Groups map to Discord roles by name through Auth's own Discord service, which
is also how aa-structures does it -- so the two agree on what "ping the
skyhook team" means.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("holdfast", "0005_theft_horizon"),
    ]

    operations = [
        migrations.AddField(
            model_name="webhook",
            name="ping_groups",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Auth groups to mention on every post to this webhook. Each "
                    "needs a Discord role of the same name -- Auth's Discord "
                    "service creates those -- so the skyhook team can be pinged "
                    "without @here."
                ),
                related_name="+",
                to="auth.group",
            ),
        ),
    ]
