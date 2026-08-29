"""The app's periodic tasks, as one importable dict.

Eleven schedule entries pasted into ``local.py`` by hand is eleven chances to
paste ten. Leaving one out does not fail loudly either -- it quietly removes a
feature, which is a bad way to find out about it a week later. So the schedule
lives here and an install merges it::

    from holdfast.schedule import CELERYBEAT_SCHEDULE as HOLDFAST_SCHEDULE
    CELERYBEAT_SCHEDULE.update(HOLDFAST_SCHEDULE)

Anyone who wants to retune a single entry can still do it afterwards::

    CELERYBEAT_SCHEDULE["holdfast_update_all_owners"]["schedule"] = crontab(...)

This module is imported from settings, so it must not touch Django models or
anything that needs the app registry -- only ``celery.schedules``.

The minute offsets are deliberate rather than decorative: the tasks that read
ESI are spread across the hour so they do not all reach for the same rate-limit
bucket at once, and ``track_workforce`` runs five minutes after the owner sync
so it reads workforce numbers that were just refreshed rather than the previous
hour's.
"""

from celery.schedules import crontab

CELERYBEAT_SCHEDULE = {
    # Structures. Each run pulls two listings plus a rotating slice of
    # structure details -- see the rate limiting section of the README.
    "holdfast_update_all_owners": {
        "task": "holdfast.tasks.update_all_owners",
        "schedule": crontab(minute="7,22,37,52"),
    },
    # Reads workforce, so it follows the owner sync rather than leading it.
    "holdfast_track_workforce": {
        "task": "holdfast.tasks.track_workforce",
        "schedule": crontab(minute="12,27,42,57"),
    },
    # Skyhook listings return a planet id and nothing else. Naming them costs
    # one universe call each, so it runs on its own rather than turning a
    # two-call listing sync into a several-minute crawl.
    "holdfast_resolve_planets": {
        "task": "holdfast.tasks.resolve_planets",
        "schedule": crontab(minute="9,24,39,54"),
    },
    # Public sovereignty map.
    "holdfast_update_sov_map": {
        "task": "holdfast.tasks.update_sov_map",
        "schedule": crontab(minute="*/15"),
    },
    # Public, one call per region, cached an hour their side.
    "holdfast_update_system_costs": {
        "task": "holdfast.tasks.update_system_costs",
        "schedule": crontab(minute=35),
    },
    # Entosis campaigns: public, cached five seconds. The only public evidence
    # that a sovereignty hub has been reinforced -- the hub detail route has no
    # state field -- so without this the timer board never turns red.
    "holdfast_refresh_raidable": {
        "task": "holdfast.tasks.refresh_raidable",
        "schedule": crontab(minute="*/10"),
    },
    "holdfast_update_campaigns": {
        "task": "holdfast.tasks.update_campaigns",
        "schedule": crontab(minute="*/5"),
    },
    # Mercenary dens. Character-scoped routes, one operator at a time.
    "holdfast_update_all_den_characters": {
        "task": "holdfast.tasks.update_all_den_characters",
        "schedule": crontab(minute="3,23,43"),
    },
    "holdfast_sync_den_slots": {
        "task": "holdfast.tasks.sync_den_slots",
        "schedule": crontab(minute=15),
    },
    # Delivery and housekeeping.
    "holdfast_run_alerts": {
        "task": "holdfast.tasks.run_alerts",
        "schedule": crontab(minute="*/10"),
    },
    "holdfast_prune_alert_log": {
        "task": "holdfast.tasks.prune_alert_log",
        "schedule": crontab(minute=40, hour=4),
    },
}
