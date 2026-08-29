"""URLs for the test settings.

Just Alliance Auth's own root URLconf. It walks the ``url_hook`` registrations
and mounts each app itself, so including this app explicitly would mount it
twice and make its namespace ambiguous -- and going through the hook is what a
real install does anyway, which makes the hook itself part of what is tested.
"""

from django.urls import include, path

urlpatterns = [
    path("", include("allianceauth.urls")),
]
