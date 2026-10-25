"""Celery tasks.

The corporation structure routes are cached by CCP for an hour, so the two
listings an owner sync pulls return the same data for most runs. The sync
still runs every fifteen minutes because of the detail calls: each run spends
a fixed budget on the stalest structures, and four runs an hour is what
rotates a large corporation through a full detail refresh inside the hour
CCP caches them for. The listings cost four tokens a run of that; see the
rate limiting section of the README.

Every task is queued once (Alliance Auth's QueueOnce): the lock is taken when
the task is queued and dropped when the run ends, so a slow run never has a
second copy of itself running beside it. Each lock expires before the task's
next scheduled run, so one left behind by a killed worker costs one run, and
each time limit leaves the lock enough room to cover the run.
"""

import random
from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from allianceauth.services.tasks import QueueOnce
from celery import shared_task
from django.db.models import Q
from django.utils import timezone
from esi.models import Token

from .app_settings import (
    HOLDFAST_OWNER_SYNC_JITTER_SECONDS,
    HOLDFAST_PLANET_RESOLVE_PER_RUN,
    HOLDFAST_STALE_PRUNE_DAYS,
)
from .core import alerts, den_sync, esi_sync
from .core.scope import inactive_reason
from .models import DenCharacter, Owner

logger = get_extension_logger(__name__)

MINUTE = 60


def _once(timeout_minutes, keys=None):
    """QueueOnce options: drop a second dispatch quietly, lock for this long.

    graceful is spelled out although Auth's QueueOnce sets it: Auth sets it
    by mutating celery_once's shared class dict, and a task's own ``once``
    replaces that dict rather than adding to it.
    """
    options = {"graceful": True, "timeout": timeout_minutes * MINUTE}
    if keys:
        options["keys"] = keys
    return options


def enqueue(task, *args) -> bool:
    """Queue a task from a web request, without letting the broker fail it.

    The caller has already saved what the user asked for, and a 500 would
    tell them it was lost. A task that cannot be queued now runs with its
    next scheduled sync, so this logs, says False, and lets the view tell
    the user so. A dispatch dropped because the task is already queued
    counts as queued: it is.
    """
    try:
        task.delay(*args)
    except Exception:  # noqa: BLE001 - kombu, redis and socket errors alike
        logger.exception(
            "Could not queue %s%s; it runs with the next scheduled sync", task.name, args
        )
        # QueueOnce takes its lock before handing the task to the broker.
        # Left behind, it would turn away the next scheduled run as well.
        try:
            task.once_backend.clear_lock(task.get_key(args, {}))
        except Exception:  # noqa: BLE001 - the cache may be what is down
            logger.warning("Could not release the queue lock of %s", task.name)
        return False
    return True


@shared_task(base=QueueOnce, once=_once(5), time_limit=2 * MINUTE)
def update_all_owners():
    """Kick off a sync for every active owner, spread over a few minutes."""
    owners = list(Owner.objects.active())
    if not owners:
        logger.info("No active owners to sync")
        return 0
    jitter = max(HOLDFAST_OWNER_SYNC_JITTER_SECONDS, 0)
    for owner in owners:
        countdown = random.randint(0, jitter) if jitter else 0
        update_owner.apply_async(args=[owner.pk], countdown=countdown)
    logger.info("Queued sync for %s owner(s)", len(owners))
    return len(owners)


@shared_task(
    base=QueueOnce, once=_once(14, keys=["owner_pk"]), time_limit=10 * MINUTE
)
def update_owner(owner_pk):
    """Refresh one corporation's hubs and skyhooks."""
    try:
        owner = Owner.objects.select_related("corporation__alliance").get(pk=owner_pk)
    except Owner.DoesNotExist:
        logger.warning("Owner %s no longer exists", owner_pk)
        return None

    # Checked here as well as in the fan-out: the task is also queued straight
    # from a registration, and an owner can leave the tracked alliance between
    # being queued and being run. Recorded on the owner, not raised, for the
    # same reason as a missing token below.
    reason = inactive_reason(owner)
    if reason is not None:
        logger.info("%s: not synced -- %s", owner, reason)
        owner.mark_sync(False, reason)
        return None

    try:
        stats = esi_sync.update_owner(owner)
    except Token.DoesNotExist as exc:
        # A revoked or never-registered token is a configuration problem, not
        # a fault. Raising made every 15-minute run log a task failure for as
        # long as it stayed unfixed, which buries the failures that matter.
        # It is recorded on the owner and shown on the Owners page instead.
        logger.warning("%s: %s", owner, exc)
        owner.mark_sync(False, str(exc))
        return None
    except esi_sync.ESIBucketLimitException as exc:
        # Another app sharing this token's bucket got there first. Nothing is
        # wrong with our data; the next run picks up where this one stopped.
        logger.warning("%s: rate limited before any detail call (%s)", owner, exc)
        owner.mark_sync(False, f"Rate limited, will retry next run: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - record it, don't crash the beat
        logger.exception("Sync failed for %s", owner)
        owner.mark_sync(False, f"{type(exc).__name__}: {exc}")
        raise

    note = ""
    if stats["rate_limited"]:
        note = "Rate limited part way through; remainder continues next run."
    elif not stats["complete"]:
        note = "Refreshing in rotation; remainder continues next run."
    owner.mark_sync(True, note)

    logger.info(
        "%s: %s hub(s)/%s skyhook(s) known, refreshed %s/%s this run%s",
        owner,
        stats["sov_hubs"],
        stats["skyhooks"],
        stats["hub_details"] + stats["skyhook_details"],
        stats["sov_hubs"] + stats["skyhooks"],
        " (rate limited)" if stats["rate_limited"] else "",
    )
    return stats


@shared_task(base=QueueOnce, once=_once(14), time_limit=10 * MINUTE)
def update_sov_map():
    """Refresh the public ADM board for tracked alliances."""
    count = esi_sync.update_sov_systems()
    logger.info("Sovereignty map: %s tracked system(s)", count)
    return count


@shared_task(base=QueueOnce, once=_once(9), time_limit=5 * MINUTE)
def refresh_raidable():
    """Store the public raidable list; the Raid Targets page only reads it."""
    rows = esi_sync.refresh_raidable_skyhooks()
    logger.info("Raidable skyhooks: %s", len(rows))
    return len(rows)


@shared_task(base=QueueOnce, once=_once(9), time_limit=8 * MINUTE)
def run_alerts():
    """Evaluate every alert rule and post what fires."""
    results = alerts.run_all_checks()
    total = sum(results.values())
    if total:
        logger.info("Sent %s alert(s): %s", total, results)
    return results


@shared_task(base=QueueOnce, once=_once(60), time_limit=30 * MINUTE)
def prune_alert_log():
    """Daily housekeeping: the alert log, and den notifications past retention.

    An alert log row is what stops an alert repeating, so age alone is no
    reason to drop one: pruning a row whose condition still held re-armed
    the alert, and a low ADM was announced again every fourteen days. A row
    older than HOLDFAST_STALE_PRUNE_DAYS goes only once it can no longer stop
    anything -- see ``alerts.prune_alert_log`` for the rule per kind.

    Den notifications hold the attacker's character, corporation and
    alliance ids, people with no account here, and go after
    HOLDFAST_DEN_EVENT_RETENTION_DAYS whatever their state.

    The name stays: an install's beat schedule refers to it.
    """
    cutoff = timezone.now() - timedelta(days=HOLDFAST_STALE_PRUNE_DAYS)
    stats = {
        "alert_log": alerts.prune_alert_log(cutoff),
        "den_events": den_sync.prune_den_events(),
    }
    if any(stats.values()):
        logger.info("Pruned %s", stats)
    return stats


@shared_task(base=QueueOnce, once=_once(5), time_limit=2 * MINUTE)
def update_all_den_characters():
    """Refresh every registered den operator, spread over a few minutes.

    Characters whose last sync found nothing are left alone until their
    idle_until passes; see DenCharacter.idle_until.
    """
    now = timezone.now()
    due = DenCharacter.objects.filter(is_enabled=True).filter(
        Q(idle_until__isnull=True) | Q(idle_until__lte=now)
    )
    characters = den_characters_in_use(due)
    idle = DenCharacter.objects.filter(is_enabled=True, idle_until__gt=now).count()
    if not characters:
        logger.info("No den characters due (%s idle)", idle)
        return 0
    jitter = max(HOLDFAST_OWNER_SYNC_JITTER_SECONDS, 0)
    for den_character in characters:
        countdown = random.randint(0, jitter) if jitter else 0
        update_den_character.apply_async(args=[den_character.pk], countdown=countdown)
    logger.info("Queued sync for %s den character(s)", len(characters))
    return len(characters)


def den_characters_in_use(den_characters):
    """The den characters whose user may still open the den section.

    Someone who left the alliance, or dropped to a state without den access,
    stops being polled: their token would otherwise keep reading their
    notifications for an alliance they are no longer in. Their rows are kept,
    not deleted -- dens, events and claim history stay for the officers, and
    somebody who comes back is polled again without registering anew.
    """
    from .views.common import den_entrant_ids

    characters = list(den_characters.select_related("character_ownership"))
    allowed = den_entrant_ids({c.character_ownership.user_id for c in characters})
    return [c for c in characters if c.character_ownership.user_id in allowed]


@shared_task(
    base=QueueOnce, once=_once(18, keys=["den_character_pk"]), time_limit=10 * MINUTE
)
def update_den_character(den_character_pk):
    """Refresh one operator's dens, tactical operations and notifications."""
    try:
        den_character = DenCharacter.objects.select_related(
            "character_ownership__character"
        ).get(pk=den_character_pk)
    except DenCharacter.DoesNotExist:
        logger.warning("Den character %s no longer exists", den_character_pk)
        return None

    try:
        stats = den_sync.update_den_character(den_character)
    except Token.DoesNotExist as exc:
        # Same reasoning as update_owner: operators lose tokens routinely --
        # they leave, or re-authorise a character elsewhere.
        logger.warning("%s: %s", den_character, exc)
        den_character.mark_sync(False, str(exc))
        return None
    except esi_sync.ESIBucketLimitException as exc:
        logger.warning("%s: rate limited (%s)", den_character, exc)
        den_character.mark_sync(False, f"Rate limited, will retry next run: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Den sync failed for %s", den_character)
        den_character.mark_sync(False, f"{type(exc).__name__}: {exc}")
        raise

    den_character.mark_sync(True)
    den_sync.note_idle(den_character, stats)
    logger.info("%s: %s", den_character, stats)
    return stats


@shared_task(base=QueueOnce, once=_once(30), time_limit=10 * MINUTE)
def sync_den_slots():
    """Keep the slot list in step with our temperate-planet skyhooks."""
    stats = den_sync.sync_den_slots()
    logger.info("Den slots: %s", stats)
    return stats


@shared_task(base=QueueOnce, once=_once(14), time_limit=5 * MINUTE)
def track_workforce():
    """Update workforce high-water marks used for siphon detection."""
    stats = den_sync.track_workforce_high_water()
    if stats["newly_dropped"] or stats["recovered"]:
        logger.info("Workforce tracking: %s", stats)
    return stats


@shared_task(base=QueueOnce, once=_once(14), time_limit=10 * MINUTE)
def resolve_planets():
    """Put names to skyhook planets a batch at a time.

    Kept out of the listing sync: naming 400 planets means 400 separate calls
    to the universe route, which would turn a one-call listing into a crawl.
    """
    stats = esi_sync.resolve_pending_planets(HOLDFAST_PLANET_RESOLVE_PER_RUN)
    if stats["resolved"] or stats["remaining"]:
        logger.info("Planet resolution: %s", stats)
    return stats


@shared_task(base=QueueOnce, once=_once(30), time_limit=10 * MINUTE)
def update_system_costs():
    """Refresh industry cost indices for systems our alliances hold."""
    count = esi_sync.update_system_costs()
    logger.info("System cost indices: %s system(s)", count)
    return count


@shared_task(base=QueueOnce, once=_once(4), time_limit=3 * MINUTE)
def update_campaigns():
    """Refresh Entosis campaigns -- our only public "is it reinforced" signal."""
    count = esi_sync.update_campaigns()
    if count:
        logger.info("Sovereignty campaigns against our space: %s", count)
    return count
