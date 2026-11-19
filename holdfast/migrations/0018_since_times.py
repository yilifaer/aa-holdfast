"""When a siphon and an unpowered upgrade began, for the den dashboard.

The dashboard's "Across the alliance" rows had no time, or a misleading one.
A siphon and an outage are things that have been going on, so what an officer
wants is "for 3 hours", which needs a start. Two nullable columns hold it:
``Skyhook.workforce_siphon_since`` (the fingerprint's first sighting) and
``SovHubUpgrade.low_since`` (the first read of Low).

Both are already true on a live install, so the data step dates them from the
first alert sent about each -- the alert log is the only record of when
Holdfast first noticed. Where there is none the column stays NULL and the
dashboard shows a dash: unknown is more honest than a time we made up.

Two more, ``SovHub.corptools_updated_at`` and ``SovHub.esi_read_at``, belong to
reading hub details from allianceauth-corptools when it is installed. Neither
needs a backfill: NULL in the first means "the details came from ESI", which is
what every existing row is, and NULL in the second only makes the next run ask
ESI once, which then sets it.

Like every data migration here it uses only the historical models it is handed.
"""

from django.db import migrations, models


def backfill_since(apps, schema_editor):
    AlertLog = apps.get_model("holdfast", "AlertLog")
    Skyhook = apps.get_model("holdfast", "Skyhook")
    SovHubUpgrade = apps.get_model("holdfast", "SovHubUpgrade")

    # siphonfp:<skyhook_id>:<percent> -- one row per rate it has been seen at,
    # so the earliest of them is when the siphon began.
    first_siphon = {}
    for key, sent_at in AlertLog.objects.filter(key__startswith="siphonfp:").values_list(
        "key", "sent_at"
    ):
        parts = key.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        skyhook_id = int(parts[1])
        if skyhook_id not in first_siphon or sent_at < first_siphon[skyhook_id]:
            first_siphon[skyhook_id] = sent_at
    for skyhook_id, sent_at in first_siphon.items():
        Skyhook.objects.filter(
            skyhook_id=skyhook_id,
            workforce_siphon_percent__isnull=False,
            workforce_siphon_since__isnull=True,
        ).update(workforce_siphon_since=sent_at)

    # upglow:<hub_id> -- armed while any upgrade on the hub is Low.
    for key, sent_at in AlertLog.objects.filter(key__startswith="upglow:").values_list(
        "key", "sent_at"
    ):
        parts = key.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        SovHubUpgrade.objects.filter(
            hub_id=int(parts[1]), power_state="Low", low_since__isnull=True
        ).update(low_since=sent_at)


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0017_den_notifications_synced"),
    ]

    operations = [
        migrations.AddField(
            model_name="skyhook",
            name="workforce_siphon_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sovhubupgrade",
            name="low_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sovhub",
            name="corptools_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sovhub",
            name="esi_read_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_since, migrations.RunPython.noop),
    ]
