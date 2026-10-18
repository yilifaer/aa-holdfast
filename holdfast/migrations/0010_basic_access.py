"""Merge sov_basic, skyhook_basic and den_basic into basic_access.

Three bottom rungs, one per section, were always meant to be granted together
-- and in practice were not. The alliance's IT handed out den_basic and never
the other two, so members had a den page and no system cost or raid target
page, and nobody could say why. The first outside install looked for the "Can
access" permission every other Auth app declares and found nothing.

One rung now, under the name Auth admins expect. An upgrade must not take
anything away: whoever holds any of the three is given basic_access, and only
then are the three deleted, so they stop cluttering the permission picker.
That widens one thing on purpose: an install that granted only den_basic now
shows those members System Cost and Raid Targets too, both public ESI data.

The labels are rewritten here because Django's post_migrate only ever creates
permissions -- it never renames or removes one -- and the den labels said
"member" for a rung that no longer exists.
"""

from django.conf import settings
from django.db import migrations

MERGED = ("sov_basic", "skyhook_basic", "den_basic")

NEW_LABELS = {
    "basic_access": "Can access Holdfast: system cost, raid targets, own dens and timers",
    "den_member": "Den user: basic access, plus the den list and applying for a den slot",
    "den_officer": "Den officer: user, plus every den page",
    "den_manage": "Den manager: officer, plus approving claims and den settings",
}

OLD_LABELS = {
    "sov_basic": "SOV: can see the system cost page",
    "skyhook_basic": "Skyhook: can see the raid target page",
    "den_basic": "Den member: can see their own dens and timers",
    "den_member": "Den user: member, plus the den list and applying for a den slot",
    "den_officer": "Den officer: user, plus every den page",
    "den_manage": "Den manager: officer, plus approving claims and den settings",
}


def _holders(apps):
    """Everything a permission can be granted to, with its relation name."""
    return (
        (apps.get_model("auth", "Group"), "permissions"),
        (apps.get_model("authentication", "State"), "permissions"),
        (apps.get_model(settings.AUTH_USER_MODEL), "user_permissions"),
    )


def merge_basic(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ours = Permission.objects.filter(content_type__app_label="holdfast")

    old = list(ours.filter(codename__in=MERGED))
    if old:
        basic = ours.filter(codename="basic_access").first()
        if basic is None:
            # post_migrate has not run yet on an upgrading install, so the
            # new permission does not exist until this makes it.
            basic = Permission.objects.create(
                codename="basic_access",
                name=NEW_LABELS["basic_access"],
                content_type=old[0].content_type,
            )
        old_pks = [permission.pk for permission in old]
        for model, relation in _holders(apps):
            for holder in model.objects.filter(**{f"{relation}__in": old_pks}).distinct():
                # By primary key: State in Alliance Auth relates to its own proxy of
                # Permission, and a related manager rejects an instance of the base
                # class, even one for the very same row.
                getattr(holder, relation).add(basic.pk)
        for permission in old:
            permission.delete()

    for codename, label in NEW_LABELS.items():
        ours.filter(codename=codename).update(name=label)


def split_basic(apps, schema_editor):
    """Give all three old rungs back to every holder of basic_access."""
    Permission = apps.get_model("auth", "Permission")
    ours = Permission.objects.filter(content_type__app_label="holdfast")

    basic = ours.filter(codename="basic_access").first()
    if basic is not None:
        old_pks = [
            Permission.objects.get_or_create(
                codename=codename,
                content_type=basic.content_type,
                defaults={"name": OLD_LABELS[codename]},
            )[0].pk
            for codename in MERGED
        ]
        for model, relation in _holders(apps):
            for holder in model.objects.filter(**{relation: basic.pk}).distinct():
                getattr(holder, relation).add(*old_pks)
        basic.delete()

    for codename, label in OLD_LABELS.items():
        ours.filter(codename=codename).update(name=label)


class Migration(migrations.Migration):

    dependencies = [
        ("authentication", "0026_alter_characterownership_user_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("holdfast", "0009_bootstrap_home_alliance"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="general",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    (
                        "basic_access",
                        "Can access Holdfast: system cost, raid targets, own dens and timers",
                    ),
                    ("sov_officer", "SOV: can see every sovereignty page"),
                    ("sov_manage", "SOV: can change sovereignty settings"),
                    ("skyhook_officer", "Skyhook: can see every skyhook page"),
                    ("skyhook_manage", "Skyhook: can change skyhook settings"),
                    (
                        "den_member",
                        "Den user: basic access, plus the den list and applying for a den slot",
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
        migrations.RunPython(merge_basic, split_basic),
    ]
