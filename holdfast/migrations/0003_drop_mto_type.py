"""Stop pretending a dungeon id is an inventory type id.

``dungeon_type_id`` on a tactical operation indexes dungeons. Resolving it
against the type tables returned whatever item happened to share the number --
12367 came back as the skill "Explosive Shield Compensation" -- so the display
name was confidently wrong. No route names a dungeon, so the number is now
reported as a number and the resolved link goes away.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0002_recorded_dens"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="mercenarytacticaloperation",
            name="eve_type",
        ),
    ]
