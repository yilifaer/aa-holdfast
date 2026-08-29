"""URLs for the test settings.

Rendering any page means rendering Alliance Auth's base template, and that
template reverses named URLs from Auth itself: the notification bell, the
group-management menu, the language picker, the sidebar toggle. Mounting Auth's
own root URLconf is simpler and less brittle than listing them, and it is what
a real install has anyway.
"""

from django.urls import include, path

urlpatterns = [
    path("holdfast/", include("holdfast.urls", namespace="holdfast")),
    path("", include("allianceauth.urls")),
]
