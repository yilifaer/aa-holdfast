"""Minimal Django settings for running the test suite.

A full Alliance Auth project pulls in dozens of apps and takes minutes to
migrate, which is a lot to pay in CI for a suite that touches this app's own
models and a handful of Alliance Auth ones. This is the smallest configuration
that lets ``holdfast`` import and its models build: Auth's authentication and
eveonline apps, django-esi, eveuniverse, and their dependencies.

It is not a substitute for testing against a real install -- it will not catch
a template that breaks under Auth's own base layout -- but it makes the logic
tests runnable anywhere in seconds.
"""

SECRET_KEY = "only-for-tests-never-deployed"
DEBUG = False
USE_TZ = True
TIME_ZONE = "UTC"
ROOT_URLCONF = "tests.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django_celery_beat",
    "esi",
    "eveuniverse",
    "allianceauth",
    "allianceauth.authentication",
    "allianceauth.eveonline",
    "allianceauth.framework",
    "allianceauth.menu",
    "allianceauth.notifications",
    "allianceauth.groupmanagement",
    "holdfast",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# django-esi refuses to build a client without these, and the tests never
# reach ESI.
ESI_SSO_CLIENT_ID = "test"
ESI_SSO_CLIENT_SECRET = "test"
ESI_SSO_CALLBACK_URL = "https://example.invalid/sso/callback"
ESI_USER_CONTACT_EMAIL = "tests@example.invalid"

CELERY_ALWAYS_EAGER = True

# Alliance Auth's authentication app reaches for a Redis connection while it is
# still loading -- its task-statistics counters are built at import time -- so a
# local-memory cache is not enough to get the app registry up. CI runs a Redis
# service for this; locally, any Redis on the default port will do.
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/9",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}


# Alliance Auth ships deployment checks that refuse to let a project start
# until these are set. They describe how a real install should be configured;
# for a test run any valid value will do, but leaving them unset stops the
# suite before it begins.
SITE_URL = "https://example.invalid"
CSRF_TRUSTED_ORIGINS = [SITE_URL]
LOGIN_TOKEN_SCOPES = ["publicData"]

# B003 and B004 inspect the running Celery application's configuration, not
# Django settings, and this harness never starts a worker. A002 only warns that
# the local Redis is old. None of the three say anything about this app.
SILENCED_SYSTEM_CHECKS = [
    "allianceauth.checks.A002",
    "allianceauth.checks.B003",
    "allianceauth.checks.B004",
]
