"""URLs for the test settings.

Alliance Auth's menu and notification machinery resolve a few named URLs at
import time, so the app's own routes are mounted under the namespace it uses in
a real install.
"""

from django.urls import include, path

urlpatterns = [
    path("holdfast/", include("holdfast.urls", namespace="holdfast")),
]
