"""Fold den_claim into den_member.

Five den permissions was two too many to hand out. Applying for a slot was a
permission of its own and nothing above it implied it, so every group that
could reach the claim button -- officers and managers included -- had to be
granted it separately, and the README carried a warning that it did nothing
without den_member beside it. The first alliance to run this in production
declined to hand them out like that.

Now dens are one ladder like the other sections, each rung including the ones
below it: member, user, officer, manager.

An upgrade must not take anything away. Whoever held den_claim without a rung
that now includes it is given den_member, and only then is den_claim deleted,
so it stops cluttering the permission picker. The labels are rewritten here
because Django's post_migrate only ever creates permissions -- it never renames
or removes one.
"""

from django.conf import settings
from django.db import migrations

RUNGS_THAT_CLAIM = ("den_member", "den_officer", "den_manage")

CLAIM_LABEL = "Den: can apply for a den slot"

NEW_LABELS = {
    "den_basic": "Den member: can see their own dens and timers",
    "den_member": "Den user: member, plus the den list and applying for a den slot",
    "den_officer": "Den officer: user, plus every den page",
    "den_manage": "Den manager: officer, plus approving claims and den settings",
}

OLD_LABELS = {
    "den_basic": "Den: can see their own dens and timers",
    "den_member": "Den: can also see the den list and their dashboard",
    "den_officer": "Den: can see every den page",
    "den_manage": "Den: can approve claims and change den settings",
}


def _holders(apps):
    """Everything a permission can be granted to, with its relation name."""
    return (
        (apps.get_model("auth", "Group"), "permissions"),
        (apps.get_model("authentication", "State"), "permissions"),
        (apps.get_model(settings.AUTH_USER_MODEL), "user_permissions"),
    )


def fold_claim(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ours = Permission.objects.filter(content_type__app_label="holdfast")

    claim = ours.filter(codename="den_claim").first()
    if claim is not None:
        member = ours.filter(codename="den_member").first()
        if member is None:
            member = Permission.objects.create(
                codename="den_member",
                name=NEW_LABELS["den_member"],
                content_type=claim.content_type,
            )
        rungs = list(
            ours.filter(codename__in=RUNGS_THAT_CLAIM).values_list("pk", flat=True)
        )
        for model, relation in _holders(apps):
            for holder in model.objects.filter(**{relation: claim.pk}).distinct():
                granted = getattr(holder, relation)
                if not granted.filter(pk__in=rungs).exists():
                    # By primary key: State in Alliance Auth relates to its own proxy of
                    # Permission, and a related manager rejects an instance of the base
                    # class, even one for the very same row.
                    granted.add(member.pk)
        claim.delete()

    for codename, label in NEW_LABELS.items():
        ours.filter(codename=codename).update(name=label)


def unfold_claim(apps, schema_editor):
    """Give den_claim back to every rung that could claim while it was gone."""
    Permission = apps.get_model("auth", "Permission")
    ours = Permission.objects.filter(content_type__app_label="holdfast")

    member = ours.filter(codename="den_member").first()
    if member is not None:
        claim, _created = Permission.objects.get_or_create(
            codename="den_claim",
            content_type=member.content_type,
            defaults={"name": CLAIM_LABEL},
        )
        rungs = list(
            ours.filter(codename__in=RUNGS_THAT_CLAIM).values_list("pk", flat=True)
        )
        for model, relation in _holders(apps):
            for holder in model.objects.filter(**{f"{relation}__in": rungs}).distinct():
                getattr(holder, relation).add(claim.pk)

    for codename, label in OLD_LABELS.items():
        ours.filter(codename=codename).update(name=label)


class Migration(migrations.Migration):

    dependencies = [
        ("authentication", "0026_alter_characterownership_user_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("holdfast", "0006_webhook_ping_groups"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="general",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    ("sov_basic", "SOV: can see the system cost page"),
                    ("sov_officer", "SOV: can see every sovereignty page"),
                    ("sov_manage", "SOV: can change sovereignty settings"),
                    ("skyhook_basic", "Skyhook: can see the raid target page"),
                    ("skyhook_officer", "Skyhook: can see every skyhook page"),
                    ("skyhook_manage", "Skyhook: can change skyhook settings"),
                    ("den_basic", "Den member: can see their own dens and timers"),
                    (
                        "den_member",
                        "Den user: member, plus the den list and applying for a den slot",
                    ),
                    ("den_officer", "Den officer: user, plus every den page"),
                    (
                        "den_manage",
                        "Den manager: officer, plus approving claims and den settings",
                    ),
                    ("manage_owners", "Can register corporation and character tokens"),
                ),
            },
        ),
        migrations.RunPython(fold_claim, unfold_claim),
    ]
