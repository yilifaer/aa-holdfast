"""Keep den claims when their slot goes, named by a snapshot of the slot.

A slot is deleted with its skyhook whenever the skyhook drops out of a
corporation's listing, and the claim cascaded with it: the record of who was
granted which ground went with the ground. ``DenClaim.slot`` is SET_NULL now,
and each claim keeps the planet and system name it was made on so it still
reads as something once the slot is gone. This names the claims that exist.

Going back, a claim without a slot cannot be held by a non-null column; those
are dropped first, which is what the cascade would have done to them.
"""

import django.db.models.deletion
from django.db import migrations, models


def snapshot_slot_names(apps, schema_editor):
    DenClaim = apps.get_model("holdfast", "DenClaim")
    claims = DenClaim.objects.filter(slot__isnull=False, slot_planet_name="").select_related(
        "slot__skyhook__eve_planet", "slot__skyhook__eve_solar_system"
    )
    for claim in claims:
        skyhook = claim.slot.skyhook
        # Skyhook.planet_name and system_name, which a historical model lacks.
        claim.slot_planet_name = (
            skyhook.eve_planet.name if skyhook.eve_planet else str(skyhook.planet_id)
        )[:100]
        claim.slot_system_name = (
            skyhook.eve_solar_system.name if skyhook.eve_solar_system else "?"
        )[:100]
        claim.save(update_fields=["slot_planet_name", "slot_system_name"])


def drop_orphaned_claims(apps, schema_editor):
    DenClaim = apps.get_model("holdfast", "DenClaim")
    DenClaim.objects.filter(slot__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0015_permission_cleanup"),
    ]

    operations = [
        migrations.AddField(
            model_name="denclaim",
            name="slot_planet_name",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="denclaim",
            name="slot_system_name",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="denclaim",
            name="slot",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claims",
                to="holdfast.denslot",
            ),
        ),
        migrations.RunPython(snapshot_slot_names, drop_orphaned_claims),
    ]
