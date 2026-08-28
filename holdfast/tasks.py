"""Celery tasks.

The corporation structure routes are cached by CCP for a full hour and are
event-based, so syncing an owner more often than hourly returns byte-identical
data and only burns the 300-per-15-minutes rate limit. The public routes are on
a five minute cache and are cheap, so they run more often.
"""

import logging
import random
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .app_settings import (
    HOLDFAST_OWNER_SYNC_JITTER_SECONDS,
    HOLDFAST_PLANET_RESOLVE_PER_RUN,
    HOLDFAST_STALE_PRUNE_DAYS,
)
from .core import alerts, den_sync, esi_sync
from .models import AlertLog, DenCharacter, Owner

logger = logging.getLogger(__name__)


@shared_task
def update_all_owners():
    """Kick off a sync for every enabled owner, spread over a few minutes."""
    owners = list(Owner.objects.filter(is_enabled=True))
    if not owners:
        logger.info("No enabled owners to sync")
        return 0
    jitter = max(HOLDFAST_OWNER_SYNC_JITTER_SECONDS, 0)
    for owner in owners:
        countdown = random.randint(0, jitter) if jitter else 0
        update_owner.apply_async(args=[owner.pk], countdown=countdown)
    logger.info("Queued sync for %s owner(s)", len(owners))
    return len(owners)


@shared_task
def update_owner(owner_pk):
    """Refresh one corporation's hubs and skyhooks."""
    try:
        owner = Owner.objects.select_related("corporation").get(pk=owner_pk)
    except Owner.DoesNotExist:
        logger.warning("Owner %s no longer exists", owner_pk)
        return None

    try:
        stats = esi_sync.update_owner(owner)
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


@shared_task
def update_sov_map():
    """Refresh the public ADM board for tracked alliances."""
    count = esi_sync.update_sov_systems()
    logger.info("Sovereignty map: %s tracked system(s)", count)
    return count


@shared_task
def refresh_raidable():
    """Warm the raidable-skyhook cache so the dashboard never waits on ESI."""
    rows = esi_sync.get_raidable_skyhooks(force_refresh=True)
    logger.info("Raidable skyhooks: %s", len(rows))
    return len(rows)


@shared_task
def run_alerts():
    """Evaluate every alert rule and post what fires."""
    results = alerts.run_all_checks()
    total = sum(results.values())
    if total:
        logger.info("Sent %s alert(s): %s", total, results)
    return results


@shared_task
def prune_alert_log():
    """Drop alert-log rows old enough that they can never suppress anything."""
    cutoff = timezone.now() - timedelta(days=HOLDFAST_STALE_PRUNE_DAYS)
    deleted, _ = AlertLog.objects.filter(sent_at__lt=cutoff).delete()
    if deleted:
        logger.info("Pruned %s alert log row(s)", deleted)
    return deleted


@shared_task
def update_all_den_characters():
    """Refresh every registered den operator, spread over a few minutes."""
    characters = list(DenCharacter.objects.filter(is_enabled=True))
    if not characters:
        logger.info("No den characters registered")
        return 0
    jitter = max(HOLDFAST_OWNER_SYNC_JITTER_SECONDS, 0)
    for den_character in characters:
        countdown = random.randint(0, jitter) if jitter else 0
        update_den_character.apply_async(args=[den_character.pk], countdown=countdown)
    logger.info("Queued sync for %s den character(s)", len(characters))
    return len(characters)


@shared_task
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
    except esi_sync.ESIBucketLimitException as exc:
        logger.warning("%s: rate limited (%s)", den_character, exc)
        den_character.mark_sync(False, f"Rate limited, will retry next run: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Den sync failed for %s", den_character)
        den_character.mark_sync(False, f"{type(exc).__name__}: {exc}")
        raise

    den_character.mark_sync(True)
    logger.info("%s: %s", den_character, stats)
    return stats


@shared_task
def sync_den_slots():
    """Keep the slot list in step with our temperate-planet skyhooks."""
    stats = den_sync.sync_den_slots()
    logger.info("Den slots: %s", stats)
    return stats


@shared_task
def track_workforce():
    """Update workforce high-water marks used for siphon detection."""
    stats = den_sync.track_workforce_high_water()
    if stats["newly_dropped"] or stats["recovered"]:
        logger.info("Workforce tracking: %s", stats)
    return stats


@shared_task
def resolve_planets():
    """Put names to skyhook planets a batch at a time.

    Kept out of the listing sync: naming 400 planets means 400 separate calls
    to the universe route, which would turn a one-call listing into a crawl.
    """
    stats = esi_sync.resolve_pending_planets(HOLDFAST_PLANET_RESOLVE_PER_RUN)
    if stats["resolved"] or stats["remaining"]:
        logger.info("Planet resolution: %s", stats)
    return stats


@shared_task
def update_system_costs():
    """Refresh industry cost indices for systems our alliances hold."""
    count = esi_sync.update_system_costs()
    logger.info("System cost indices: %s system(s)", count)
    return count


@shared_task
def update_campaigns():
    """Refresh Entosis campaigns -- our only public "is it reinforced" signal."""
    count = esi_sync.update_campaigns()
    if count:
        logger.info("Sovereignty campaigns against our space: %s", count)
    return count
