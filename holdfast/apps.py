from django.apps import AppConfig

from . import __version__


class HoldfastConfig(AppConfig):
    name = "holdfast"
    label = "holdfast"
    verbose_name = f"SOV Monitor v{__version__}"
    default_auto_field = "django.db.models.AutoField"
