"""Tick this app's charlink entry by default on installs that already have it.

charlink reads an entry's ``default_initial_selection`` exactly once -- when
it first sees the entry, it stores the value in its own AppSettings row and
reads the row from then on. So flipping the flag in charlink_hook.py changes
nothing for an install that imported the entry while it was off, which is
every install that has run a release before 0.1.10. This flips the stored
value once; an admin who wants it off can turn it off again in charlink's
admin, and this will not touch it a second time.

charlink is optional, and when present its tables may be created after ours
on a fresh install -- where the row does not exist yet anyway, and charlink
will create it from the flag. Hence the live registry and the table check,
rather than a migration dependency on an app that may not be there.
"""

from django.apps import apps as live_apps
from django.db import migrations

APP_NAME = "holdfast_tools"  # charlink's key: "<app label>_<unique_id>"


def tick_by_default(apps, schema_editor):
    if not live_apps.is_installed("charlink"):
        return
    try:
        AppSettings = live_apps.get_model("charlink", "AppSettings")
    except LookupError:
        return
    if AppSettings._meta.db_table not in schema_editor.connection.introspection.table_names():
        return
    AppSettings.objects.filter(app_name=APP_NAME).update(default_selection=True)


class Migration(migrations.Migration):

    dependencies = [
        ("holdfast", "0012_den_character_idle"),
    ]

    operations = [
        migrations.RunPython(tick_by_default, migrations.RunPython.noop),
    ]
