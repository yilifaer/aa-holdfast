"""How many things want a given user's attention, per section.

Used for the badge on each sidebar entry. The sidebar renders on *every* page
of the site, so these have to stay cheap: aggregate queries only, no building
of display objects, and a short per-user cache so a burst of page loads costs
one round of queries rather than one per request.
"""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from ..models import (
    DenClaim,
    DenEvent,
    DenSlot,
    MercenaryDen,
    PowerState,
    ReagentThreshold,
    Skyhook,
    SovCampaign,
    SovHub,
    HoldfastConfig,
    SovSystem,
)

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60


def _cached(section, user, builder):
    key = f"holdfast_attention:{section}:{user.pk}"
    value = cache.get(key)
    if value is None:
        try:
            value = builder()
        except Exception:  # noqa: BLE001 - a badge must never break the sidebar
            logger.exception("Attention count failed for %s", section)
            value = 0
        cache.set(key, value, CACHE_SECONDS)
    return value


def _alliance_ids(user):
    from ..views.common import visible_alliance_ids

    return visible_alliance_ids(user)


def _owner_ids(user):
    from ..views.common import visible_owners

    return list(visible_owners(user).values_list("pk", flat=True))


def sov_count(user) -> int:
    from ..views.common import sov_can_view_all

    if not sov_can_view_all(user):
        return 0

    def build():
        config = HoldfastConfig.get_solo()
        owners = _owner_ids(user)
        now = timezone.now()
        total = 0

        # Hubs inside the widest fuel band. fuel_expires_at is denormalised
        # precisely so this stays one indexed comparison.
        cutoff = now + timedelta(days=config.fuel_warning_days)
        total += SovHub.objects.filter(
            owner_id__in=owners,
            fuel_expires_at__isnull=False,
            fuel_expires_at__lte=cutoff,
        ).count()

        if config.notify_upgrade_offline:
            total += (
                SovHub.objects.filter(
                    owner_id__in=owners, upgrades__power_state=PowerState.LOW
                )
                .distinct()
                .count()
            )

        alliance_ids = _alliance_ids(user)
        if config.adm_alert_threshold is not None:
            total += SovSystem.objects.filter(
                alliance_id__in=alliance_ids,
                activity_defense_multiplier__lt=config.adm_alert_threshold,
            ).count()

        total += SovCampaign.objects.filter(
            event_type=SovCampaign.EventType.IHUB,
            solar_system_id__in=SovSystem.objects.filter(
                alliance_id__in=alliance_ids
            ).values("solar_system_id"),
        ).count()
        return total

    return _cached("sov", user, build)


def skyhook_count(user) -> int:
    from ..views.common import skyhook_can_view_all

    if not skyhook_can_view_all(user):
        return 0

    def build():
        owners = _owner_ids(user)
        now = timezone.now()
        horizon = now + timedelta(hours=24)
        total = Skyhook.objects.filter(owner_id__in=owners).exclude(
            state__in=["Unspecified", "ShieldVulnerable"]
        ).count()

        rules = {r.type_id: r for r in ReagentThreshold.objects.all()}
        candidates = Skyhook.objects.filter(
            owner_id__in=owners,
            theft_start__isnull=False,
            theft_start__lte=horizon,
            theft_end__gte=now,
        ).prefetch_related("reagents")
        for skyhook in candidates:
            for reagent in skyhook.reagents.all():
                rule = rules.get(reagent.type_id)
                if rule and rule.is_enabled and reagent.unsecured_stock >= rule.min_unsecured:
                    total += 1
                    break
        return total

    return _cached("skyhook", user, build)


def den_count(user) -> int:
    from ..views.common import den_can_enter, den_can_view_all

    if not den_can_enter(user):
        return 0

    def build():
        now = timezone.now()
        mine = MercenaryDen.objects.filter(
            den_character__character_ownership__user=user
        )
        total = mine.exclude(state=MercenaryDen.State.RUNNING).count()
        total += DenEvent.objects.filter(
            den_character__character_ownership__user=user,
            kind=DenEvent.Kind.ATTACKED,
            timestamp__gte=now - timedelta(hours=24),
        ).count()

        if den_can_view_all(user):
            owners = _owner_ids(user)
            slots = DenSlot.objects.filter(skyhook__owner_id__in=owners)
            total += DenClaim.objects.filter(
                slot__in=slots, status=DenClaim.Status.PENDING
            ).count()
            total += Skyhook.objects.filter(
                den_slot__in=slots, workforce_siphon_percent__isnull=False
            ).count()
        return total

    return _cached("den", user, build)


def invalidate(user=None):
    """Drop cached counts so a badge updates without waiting a minute."""
    if user is None:
        return
    for section in ("sov", "skyhook", "den"):
        cache.delete(f"holdfast_attention:{section}:{user.pk}")
