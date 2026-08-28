"""Single shared ESI client for the app.

django-esi builds its client from ``meta/openapi.json`` and sends the
``X-Compatibility-Date`` header for us. We filter the spec down to the three
tags we actually touch -- loading all 218 routes costs a lot of memory on a
Raspberry Pi, and django-esi refuses an unfiltered spec when DEBUG is off.
"""

from esi.openapi_clients import ESIClientProvider

from . import __version__
from .app_settings import HOLDFAST_ESI_COMPATIBILITY_DATE

esi = ESIClientProvider(
    compatibility_date=HOLDFAST_ESI_COMPATIBILITY_DATE,
    ua_appname="aa-holdfast",
    ua_version=__version__,
    ua_url="https://github.com/yilifaer/aa-holdfast",
    tags=["Structures", "Sovereignty", "Activities", "Character", "Industry"],
)
