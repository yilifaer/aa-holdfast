"""Settings an install may override in myauth/settings/local.py.

Most tuning lives on the in-app settings pages instead. The scope lists and
notification types below are constants, not settings: changing them would
leave every token already issued without what the code asks of it.
"""

from django.conf import settings

from . import ESI_COMPATIBILITY_DATE


def _setting(name, default):
    return getattr(settings, name, default)


# ESI compatibility date sent with every request. Must be >= 2026-05-19 or the
# sovereignty hub / skyhook routes return 404.
HOLDFAST_ESI_COMPATIBILITY_DATE = _setting(
    "HOLDFAST_ESI_COMPATIBILITY_DATE", ESI_COMPATIBILITY_DATE
)

# Fallback bar for skyhook theft alerts, used only for reagents that have no
# row in the ReagentThreshold table. Per-reagent bars are set by a skyhook
# manager on the Skyhook settings page; a row is created automatically the
# first time each reagent is seen on a skyhook.
HOLDFAST_SKYHOOK_MIN_UNSECURED = _setting("HOLDFAST_SKYHOOK_MIN_UNSECURED", 100)

# Extra alliance IDs to keep sovereignty/ADM records for, on top of the tracked
# alliance chosen on the SOV settings page. Corporations in these alliances may
# be registered too. Read in core.scope.
HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS = _setting("HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS", [])

# How long the public raidable-skyhook list that refresh_raidable stores in
# Redis lives. The Raid Targets page only reads it, so this has to outlast
# the task's ten-minute schedule with room for a failed run or two; below
# that the page keeps saying the list is being fetched.
HOLDFAST_RAIDABLE_CACHE_SECONDS = _setting("HOLDFAST_RAIDABLE_CACHE_SECONDS", 1800)

# Detail calls per owner per run. ESI charges two rate-limit tokens for a
# successful response, so 60 calls is about 120 tokens of the 300-per-15-
# minutes corporation bucket -- leaving most of it for the other Auth modules
# on this site, which share the bucket because they share the ESI application. The old default of 100 was chosen when a call was believed to
# cost one token, and spent two thirds of the bucket rather than one third.
HOLDFAST_DETAIL_CALLS_PER_RUN = _setting("HOLDFAST_DETAIL_CALLS_PER_RUN", 60)

# Spread owners out so they do not all reach for the same bucket at once.
# Kept well under the gap between runs: at five minutes, two runs of a
# fifteen-minute schedule could land ten minutes apart and overlap inside
# ESI's sliding window.
HOLDFAST_OWNER_SYNC_JITTER_SECONDS = _setting("HOLDFAST_OWNER_SYNC_JITTER_SECONDS", 120)

# Planet names to resolve per run. The universe route is public with no
# declared rate limit, but each planet is a separate HTTP call, so it gets its
# own budget instead of blocking a listing sync.
HOLDFAST_PLANET_RESOLVE_PER_RUN = _setting("HOLDFAST_PLANET_RESOLVE_PER_RUN", 60)

# Alert log rows older than this many days are pruned once they can no
# longer stop anything: the structure, operation or event they were about is
# gone or has moved on. A row whose condition still holds is kept however
# old, so pruning never repeats an alert. Covers the alert log only; den
# notifications have their own retention below.
HOLDFAST_STALE_PRUNE_DAYS = _setting("HOLDFAST_STALE_PRUNE_DAYS", 14)

# Scopes a corporation owner token must carry.
HOLDFAST_ESI_SCOPES = ["esi-structures.read_corporation.v1"]

# Scopes a den operator's character token must carry. Requested together at
# claim time: den routes are character scoped, and adding a scope later means
# making every operator re-authorise.
HOLDFAST_DEN_ESI_SCOPES = [
    "esi-structures.read_character.v1",      # the dens themselves
    "esi-activities.read_character.v1",      # tactical operations
    "esi-characters.read_notifications.v1",  # "being shot right now"
]

# Detail calls one den character may spend per run. The char-structure bucket
# is only 30 per 15 minutes, and a character can hold at most 5 dens, so this
# is generous.
HOLDFAST_DEN_DETAIL_CALLS_PER_RUN = _setting("HOLDFAST_DEN_DETAIL_CALLS_PER_RUN", 10)

# Notification types worth pulling. Everything else on a den is visible from
# its own route, which is cheaper and does not need the notification scope.
HOLDFAST_DEN_NOTIFICATION_TYPES = [
    "MercenaryDenAttacked",
    "MercenaryDenReinforced",
    "MercenaryDenNewMTO",
]

# How long a den character whose sync found nothing is left alone before it is
# listed again. See DenCharacter.idle_until.
HOLDFAST_DEN_IDLE_HOURS = _setting("HOLDFAST_DEN_IDLE_HOURS", 6)

# An operator's very first sync must not replay days of history into Discord,
# but it must not swallow an attack happening right now either -- which is
# exactly when a new operator is most likely to be registering. Anything newer
# than this on a first sync is treated as live.
HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES = _setting(
    "HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES", 90
)

# A den attack older than this is history: a sync records it without
# announcing it, and one Discord never took stops being retried.
HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS = _setting(
    "HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS", 24
)

# Days a den notification is kept. Its text names the attacker's character,
# corporation and alliance -- people with no account here -- so it is not
# kept for good; older ones are pruned daily and not read back in from ESI.
HOLDFAST_DEN_EVENT_RETENTION_DAYS = _setting("HOLDFAST_DEN_EVENT_RETENTION_DAYS", 30)
