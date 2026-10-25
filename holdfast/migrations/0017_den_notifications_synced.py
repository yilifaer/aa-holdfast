"""Remember that a den character's notifications were read, apart from its events.

A first sync announces only what is HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES old;
a later one, a day's worth. "First" was "has no DenEvent", and since 0.1.12
prunes those after HOLDFAST_DEN_EVENT_RETENTION_DAYS, a quiet operator went
back to looking new -- and an attack read three hours late, after an outage,
was filed as already alerted. The marker is what retention does not touch.

Every character that has events, or has finished a sync, has had its list
read already; this marks those, so the upgrade itself narrows nobody's window.
"""

from django.db import migrations, models
from django.db.models import Exists, OuterRef
from django.utils import timezone


def mark_synced(apps, schema_editor):
    DenCharacter = apps.get_model("holdfast", "DenCharacter")
    DenEvent = apps.get_model("holdfast", "DenEvent")
    has_events = Exists(DenEvent.objects.filter(den_character=OuterRef("pk")))
    now = timezone.now()
    for den_character in DenCharacter.objects.annotate(has_events=has_events).filter(
        models.Q(has_events=True) | models.Q(last_sync_ok=True)
    ):
        den_character.notifications_synced_at = den_character.last_sync_at or now
        den_character.save(update_fields=["notifications_synced_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0016_claim_history_outlives_slot"),
    ]

    operations = [
        migrations.AddField(
            model_name="dencharacter",
            name="notifications_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_synced, migrations.RunPython.noop),
    ]
