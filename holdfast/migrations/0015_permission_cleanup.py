"""Drop Django's default permissions, and say what manage_owners does.

Every model had Django's add/change/delete/view: 84 rows in the group
permission picker that no Holdfast page checks, burying the nine that matter.
The models declare none now, and this deletes the rows already made. Only
for this app's content types, and only the four defaults of each model --
never another app's, never one of the nine. Deleting a permission takes its
grants with it, which is the point; superusers keep all of Django admin,
since a superuser needs no permission row. Reversing deletes nothing back:
Django's post_migrate re-creates them if the options ever return.

``manage_owners`` was labelled "corporation and character tokens", but a den
character is registered by its own player under basic_access. The label is
rewritten here because post_migrate only ever creates permissions -- it never
renames one.
"""

from django.conf import settings
from django.db import migrations

DEFAULT_ACTIONS = ("add", "change", "delete", "view")

OLD_LABEL = "Can register corporation and character tokens"
NEW_LABEL = "Can register and switch corporation tokens"

# Each model's options as they stand; AlterModelOptions replaces them whole.
MODEL_OPTIONS = {
    "alertlog": {},
    "alertroute": {"ordering": ["category"], "verbose_name": "alert route"},
    "dencharacter": {
        "ordering": ["character_ownership__character__character_name"],
        "verbose_name": "den character",
    },
    "denclaim": {"ordering": ["-created_at"], "verbose_name": "den claim"},
    "denevent": {"ordering": ["-timestamp"], "verbose_name": "den event"},
    "denoperator": {"verbose_name": "den operator"},
    "denslot": {"ordering": ["skyhook__eve_planet__name"], "verbose_name": "den slot"},
    "holdfastconfig": {
        "verbose_name": "configuration",
        "verbose_name_plural": "configuration",
    },
    "mercenaryden": {"ordering": ["eve_planet__name"], "verbose_name": "mercenary den"},
    "mercenarytacticaloperation": {
        "ordering": ["expires"],
        "verbose_name": "mercenary tactical operation",
    },
    "owner": {"ordering": ["corporation__corporation_name"]},
    "reagentthreshold": {
        "ordering": ["eve_type__name", "type_id"],
        "verbose_name": "reagent alert threshold",
    },
    "skyhook": {"ordering": ["theft_start"]},
    "skyhookreagent": {"ordering": ["type_id"]},
    "sovcampaign": {"ordering": ["start_time"], "verbose_name": "sovereignty campaign"},
    "sovhub": {"ordering": ["fuel_expires_at"], "verbose_name": "sovereignty hub"},
    "sovhubreagent": {"ordering": ["type_id"]},
    "sovhubupgrade": {"ordering": ["type_id"]},
    "sovsystem": {
        "ordering": ["activity_defense_multiplier"],
        "verbose_name": "sovereignty system",
    },
    "systemcostindex": {
        "ordering": ["manufacturing"],
        "verbose_name": "system cost index",
        "verbose_name_plural": "system cost indices",
    },
    "webhook": {},
}


def drop_default_permissions(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    # Per content type, so a model removed in an earlier release loses its
    # leftovers too, and nothing but the exact default codenames is touched.
    for content_type in ContentType.objects.filter(app_label="holdfast"):
        Permission.objects.filter(
            content_type=content_type,
            codename__in=[f"{action}_{content_type.model}" for action in DEFAULT_ACTIONS],
        ).delete()


def relabel_manage_owners(apps, schema_editor):
    _label_manage_owners(apps, NEW_LABEL)


def restore_manage_owners_label(apps, schema_editor):
    _label_manage_owners(apps, OLD_LABEL)


def _label_manage_owners(apps, label):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="holdfast", codename="manage_owners"
    ).update(name=label)


class Migration(migrations.Migration):

    dependencies = [
        # Deleting a permission deletes its grants, and the state grants live
        # in Auth's table: its model has to be in the state handed over.
        ("authentication", "0026_alter_characterownership_user_and_more"),
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("holdfast", "0014_webhook_gone_and_pinned_routes"),
    ]

    operations = [
        *(
            migrations.AlterModelOptions(
                name=name, options={"default_permissions": (), **options}
            )
            for name, options in MODEL_OPTIONS.items()
        ),
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
                    ("manage_owners", NEW_LABEL),
                ),
            },
        ),
        migrations.RunPython(drop_default_permissions, migrations.RunPython.noop),
        migrations.RunPython(relabel_manage_owners, restore_manage_owners_label),
    ]
