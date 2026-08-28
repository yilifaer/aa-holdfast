"""Tunables. Override any of these in myauth/settings/local.py."""

from django.conf import settings

from . import ESI_COMPATIBILITY_DATE


def _setting(name, default):
    return getattr(settings, name, default)


# ESI compatibility date sent with every request. Must be >= 2026-05-19 or the
# sovereignty hub / skyhook routes return 404.
HOLDFAST_ESI_COMPATIBILITY_DATE = _setting(
    "HOLDFAST_ESI_COMPATIBILITY_DATE", ESI_COMPATIBILITY_DATE
)

# Hours-of-fuel-left thresholds that trigger a sov hub fuel alert. Each hub
# alerts at most once per threshold band per refuelling.
HOLDFAST_FUEL_ALERT_THRESHOLDS = _setting("HOLDFAST_FUEL_ALERT_THRESHOLDS", [48, 24, 6])

# How long before a skyhook's theft window opens we warn, in minutes.
HOLDFAST_SKYHOOK_THEFT_LEAD_MINUTES = _setting("HOLDFAST_SKYHOOK_THEFT_LEAD_MINUTES", 45)

# Fallback bar for skyhook theft alerts, used only for reagents that have no
# row in the ReagentThreshold table. Per-reagent bars are set by admins in
# Django admin (Holdfast -> Reagent alert thresholds); a row is created
# automatically the first time each reagent is seen on a skyhook.
HOLDFAST_SKYHOOK_MIN_UNSECURED = _setting("HOLDFAST_SKYHOOK_MIN_UNSECURED", 100)

# Warn when a tracked system's ADM drops below this. None disables the check.
HOLDFAST_ADM_ALERT_THRESHOLD = _setting("HOLDFAST_ADM_ALERT_THRESHOLD", 3.0)

# Extra alliance IDs to keep sovereignty/ADM records for, on top of the
# alliances the registered owner corporations belong to.
HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS = _setting("HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS", [])

# Seconds to cache the public raidable-skyhook list in Redis. The ESI route
# itself is cached for 300s, so going below that just burns rate limit.
HOLDFAST_RAIDABLE_CACHE_SECONDS = _setting("HOLDFAST_RAIDABLE_CACHE_SECONDS", 300)

# Detail calls one owner may spend per sync run. Each hub and each skyhook
# costs one. The corp-structure rate limit is 300 per 15 minutes per token and
# is shared with any other app using the same character, so leave headroom: at
# 100 per run on a 15-minute schedule a 460-structure corporation is fully
# refreshed in about 75 minutes, close to CCP's own one hour cache, using a
# third of the bucket.
HOLDFAST_DETAIL_CALLS_PER_RUN = _setting("HOLDFAST_DETAIL_CALLS_PER_RUN", 100)

# Seconds between owner ESI syncs. The corp structure routes are cached for one
# hour server-side, so anything shorter returns identical data.
HOLDFAST_OWNER_SYNC_SECONDS = _setting("HOLDFAST_OWNER_SYNC_SECONDS", 3600)

# Stagger owner syncs by this many seconds so a big alliance doesn't fire every
# corp's requests in the same instant.
HOLDFAST_OWNER_SYNC_JITTER_SECONDS = _setting("HOLDFAST_OWNER_SYNC_JITTER_SECONDS", 300)

# Planet names to resolve per run. The universe route is public with no
# declared rate limit, but each planet is a separate HTTP call, so it gets its
# own budget instead of blocking a listing sync.
HOLDFAST_PLANET_RESOLVE_PER_RUN = _setting("HOLDFAST_PLANET_RESOLVE_PER_RUN", 60)

# Rows older than this many days with no sighting get pruned.
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

# Ignore notifications older than this on first sync, so registering a
# character does not replay months of history into Discord.
HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS = _setting(
    "HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS", 24
)
