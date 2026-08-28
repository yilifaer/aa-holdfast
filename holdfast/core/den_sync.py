"""Mercenary den synchronisation.

Every den route in ESI is *character* scoped. There is no corporation or
public equivalent, which has two consequences that shape this whole module:

* We can only ever see our own operators' dens, and only if each of them
  registers a token. A hostile den on our ground is invisible to ESI and gets
  recorded by hand instead.
* The one thing the dedicated routes cannot show is a den being shot right now
  but not yet reinforced. That, and only that, is why notifications are pulled.
  Reinforcement is visible as ``state == Paused``; tactical operations have
  their own route.

Budgets here are small on purpose: the ``char-structure`` bucket allows only
30 requests per 15 minutes, and ``char-notification`` only 15.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from esi.exceptions import HTTPNotModified
from eveuniverse.models import EvePlanet

from ..app_settings import (
    HOLDFAST_DEN_DETAIL_CALLS_PER_RUN,
    HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS,
    HOLDFAST_DEN_NOTIFICATION_TYPES,
)
from ..models import (
    TEMPERATE_PLANET_TYPE_ID,
    DenCharacter,
    DenEvent,
    DenSlot,
    MercenaryDen,
    MercenaryTacticalOperation,
    Skyhook,
    HoldfastConfig,
)
from ..providers import esi
from .esi_sync import ESIBucketLimitException

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------


def sync_den_slots() -> dict:
    """Keep one slot per temperate-planet skyhook we can see.

    Dens anchor within 10km of a skyhook and only on temperate planets, so the
    set of slots is exactly the set of temperate skyhooks we own. Nothing here
    talks to ESI.
    """
    temperate = Skyhook.objects.filter(
        eve_planet__eve_type_id=TEMPERATE_PLANET_TYPE_ID
    ).select_related("eve_planet")

    created = 0
    for skyhook in temperate:
        _, was_created = DenSlot.objects.get_or_create(skyhook=skyhook)
        created += int(was_created)

    # A skyhook we lost takes its slot with it through the FK cascade. What is
    # dropped here is a slot whose planet turned out not to be temperate after
    # all. Slots whose planet has not been named yet are left strictly alone --
    # deleting one would take its claims with it.
    stale = DenSlot.objects.exclude(skyhook__in=temperate).exclude(
        skyhook__eve_planet__isnull=True
    )
    removed = stale.count()
    stale.delete()

    if created or removed:
        logger.info("Den slots: +%s, -%s", created, removed)
    return {"created": created, "removed": removed, "total": DenSlot.objects.count()}


def link_den_to_slot(den: MercenaryDen) -> None:
    """Attach a den to one of our slots when it sits on our own skyhook."""
    if not den.skyhook_id:
        return
    slot = DenSlot.objects.filter(skyhook__skyhook_id=den.skyhook_id).first()
    if slot and den.slot_id != slot.pk:
        den.slot = slot
        den.save(update_fields=["slot"])


# --------------------------------------------------------------------------
# Dens
# --------------------------------------------------------------------------


def sync_den_listing(den_character: DenCharacter, token, planet_memo) -> int:
    character_id = den_character.character.character_id
    try:
        listing = esi.client.Structures.GetCharactersStructuresMercenaryDensListing(
            character_id=character_id, token=token
        ).result(force_refresh=True)
    except HTTPNotModified:
        logger.info("%s: den listing unchanged", den_character)
        return MercenaryDen.objects.filter(den_character=den_character).count()

    now = timezone.now()
    seen = set()
    for entry in (e.model_dump() for e in listing.mercenary_dens):
        den_id = entry.get("id")
        if den_id is None:
            continue
        seen.add(den_id)
        planet_id = entry.get("planet_id")
        planet = _resolve_planet(planet_id, planet_memo)
        MercenaryDen.objects.update_or_create(
            den_id=den_id,
            defaults={
                "den_character": den_character,
                "eve_character": den_character.character,
                "planet_id": planet_id,
                "eve_planet": planet,
                "eve_solar_system": planet.eve_solar_system if planet else None,
                "last_seen_at": now,
            },
        )

    removed, _ = (
        MercenaryDen.objects.filter(den_character=den_character)
        .exclude(den_id__in=seen)
        .delete()
    )
    if removed:
        logger.info("%s: %s den(s) gone", den_character, removed)
    return len(seen)


def refresh_den_details(den_character: DenCharacter, token, budget) -> int:
    refreshed = 0
    stalest = MercenaryDen.objects.filter(den_character=den_character).order_by(
        "detail_updated_at"
    )
    for den in stalest:
        if budget <= 0:
            break
        budget -= 1
        try:
            _fetch_den_detail(den_character, token, den)
        except ESIBucketLimitException:
            logger.warning("%s: den bucket exhausted", den_character)
            break
        refreshed += 1
    return refreshed


def _fetch_den_detail(den_character, token, den):
    try:
        detail = esi.client.Structures.GetCharactersStructuresMercenaryDensDetail(
            mercenary_den_id=den.den_id,
            character_id=den_character.character.character_id,
            token=token,
        ).result()
    except HTTPNotModified:
        MercenaryDen.objects.filter(den_id=den.den_id).update(
            detail_updated_at=timezone.now()
        )
        return
    data = detail.model_dump()

    evolution = data.get("evolution") or {}
    development = evolution.get("development") or {}
    anarchy = evolution.get("anarchy") or {}
    skyhook = data.get("skyhook") or {}
    reinforce = data.get("reinforcement_timer") or {}
    infomorphs = data.get("infomorphs") or {}

    MercenaryDen.objects.filter(den_id=den.den_id).update(
        type_id=data.get("type_id"),
        state=data.get("state") or "Unspecified",
        development_level=development.get("level") or "Unspecified",
        development_amount=development.get("amount") or 0,
        anarchy_level=anarchy.get("level") or "Unspecified",
        anarchy_amount=anarchy.get("amount") or 0,
        infomorphs=infomorphs.get("amount") or 0,
        skyhook_id=skyhook.get("id"),
        skyhook_corporation_id=skyhook.get("corporation_id"),
        reinforce_end=reinforce.get("end"),
        detail_updated_at=timezone.now(),
    )
    den.refresh_from_db()
    link_den_to_slot(den)


# --------------------------------------------------------------------------
# Tactical operations
# --------------------------------------------------------------------------


def sync_operations(den_character: DenCharacter, token) -> int:
    """Pull MTOs. One listing call plus one detail call per operation.

    A character can hold at most five dens, so this stays tiny.
    """
    character_id = den_character.character.character_id
    try:
        listing = esi.client.Activities.GetCharactersMercenaryTacticalOperationsListing(
            character_id=character_id, token=token
        ).result(force_refresh=True)
    except HTTPNotModified:
        return MercenaryTacticalOperation.objects.filter(
            den_character=den_character
        ).count()

    seen = set()
    for entry in (e.model_dump() for e in listing.operations):
        operation_id = entry.get("id")
        if not operation_id:
            continue
        seen.add(operation_id)
        try:
            detail = esi.client.Activities.GetCharactersMercenaryTacticalOperationsDetail(
                operation_id=operation_id, character_id=character_id, token=token
            ).result()
        except HTTPNotModified:
            continue
        except ESIBucketLimitException:
            logger.warning("%s: activity bucket exhausted", den_character)
            break
        data = detail.model_dump()
        den_id = data.get("mercenary_den_id") or entry.get("mercenary_den_id")
        dungeon_type_id = data.get("dungeon_type_id")
        MercenaryTacticalOperation.objects.update_or_create(
            operation_id=operation_id,
            defaults={
                "den_character": den_character,
                "den": MercenaryDen.objects.filter(den_id=den_id).first(),
                "mercenary_den_id": den_id,
                # Stored raw: dungeon ids are not inventory type ids, and
                # resolving one against the type tables produces a confident
                # wrong answer rather than an empty one.
                "dungeon_type_id": dungeon_type_id,
                "state": data.get("state") or "Unspecified",
                "expires": data.get("expires"),
                "updated_at": timezone.now(),
            },
        )

    removed, _ = (
        MercenaryTacticalOperation.objects.filter(den_character=den_character)
        .exclude(operation_id__in=seen)
        .delete()
    )
    return len(seen)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def sync_notifications(den_character: DenCharacter, token) -> int:
    """Pull the three den notification types.

    Notifications are stored server-side, so nothing is lost while a character
    is logged out. Old ones do roll off the list eventually, which is why this
    runs on a schedule rather than on demand.
    """
    try:
        result = esi.client.Character.GetCharactersCharacterIdNotifications(
            character_id=den_character.character.character_id, token=token
        ).result()
    except HTTPNotModified:
        return 0
    except ESIBucketLimitException:
        logger.warning("%s: notification bucket exhausted", den_character)
        return 0

    cutoff = timezone.now() - timedelta(hours=HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS)
    first_sync = not DenEvent.objects.filter(den_character=den_character).exists()

    created = 0
    for item in result:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        kind = data.get("type")
        if kind not in HOLDFAST_DEN_NOTIFICATION_TYPES:
            continue
        timestamp = data.get("timestamp")
        if not timestamp:
            continue

        text = data.get("text") or ""
        den_id = _den_id_from_text(text)
        _, was_created = DenEvent.objects.get_or_create(
            notification_id=data.get("notification_id"),
            defaults={
                "den_character": den_character,
                "den": MercenaryDen.objects.filter(den_id=den_id).first()
                if den_id
                else None,
                "kind": kind,
                "timestamp": timestamp,
                "text": text[:4000],
                # On a character's very first sync, backfill history silently
                # rather than replaying days of it into Discord.
                "is_alerted": first_sync or timestamp < cutoff,
            },
        )
        created += int(was_created)

    return created


def _den_id_from_text(text: str):
    """Dig the den's item ID out of the notification's YAML-ish body."""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        for key in ("mercenaryDenID:", "structureID:", "itemID:"):
            if line.startswith(key):
                value = line[len(key):].strip()
                if value.isdigit():
                    return int(value)
    return None


# --------------------------------------------------------------------------
# Workforce siphon detection
# --------------------------------------------------------------------------


def track_workforce_high_water() -> dict:
    """Remember each skyhook's best-ever workforce and note when it drops.

    A den reaching anarchy level 2 siphons its skyhook's workforce output. We
    cannot see anyone else's den, so a sustained shortfall against a skyhook's
    own historic peak is the only automatic tell that one is there.
    """
    config = HoldfastConfig.get_solo()
    now = timezone.now()
    raised = dropped = recovered = 0

    for skyhook in Skyhook.objects.filter(effective_workforce__isnull=False):
        # If a den is already taking a cut, the reading in front of us is not
        # this skyhook's real output. Using it as the peak would bake the theft
        # into the baseline and guarantee we never notice -- exactly what
        # happened to three skyhooks that were already siphoned the first time
        # this app ever saw them. Use the implied original instead.
        current = skyhook.workforce_base or skyhook.effective_workforce
        peak = skyhook.workforce_high_water
        changed = []

        if peak is None or current > peak:
            skyhook.workforce_high_water = current
            changed.append("workforce_high_water")
            if peak is not None:
                raised += 1
            # A new peak means whatever was dragging it down is gone.
            if skyhook.workforce_dropped_at:
                skyhook.workforce_dropped_at = None
                changed.append("workforce_dropped_at")
                recovered += 1
        else:
            shortfall_pct = (peak - current) / peak * 100 if peak else 0
            if shortfall_pct >= config.workforce_drop_percent:
                if not skyhook.workforce_dropped_at:
                    skyhook.workforce_dropped_at = now
                    changed.append("workforce_dropped_at")
                    dropped += 1
            elif skyhook.workforce_dropped_at:
                skyhook.workforce_dropped_at = None
                changed.append("workforce_dropped_at")
                recovered += 1

        if changed:
            skyhook.save(update_fields=changed)

    return {"peaks_raised": raised, "newly_dropped": dropped, "recovered": recovered}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def update_den_character(den_character: DenCharacter) -> dict:
    """Refresh one operator's dens, operations and notifications."""
    token = den_character.fetch_token()
    planet_memo: dict = {}

    dens = sync_den_listing(den_character, token, planet_memo)
    details = refresh_den_details(
        den_character, token, HOLDFAST_DEN_DETAIL_CALLS_PER_RUN
    )
    operations = sync_operations(den_character, token)
    events = sync_notifications(den_character, token)

    return {
        "dens": dens,
        "den_details": details,
        "operations": operations,
        "new_events": events,
    }


# --------------------------------------------------------------------------
# Shared resolvers -- kept local so den sync does not import the corp module's
# private helpers.
# --------------------------------------------------------------------------



def _resolve_planet(planet_id, memo):
    if planet_id in memo:
        return memo[planet_id]
    try:
        planet, _ = EvePlanet.objects.get_or_create_esi(id=planet_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve planet %s", planet_id, exc_info=True)
        planet = None
    memo[planet_id] = planet
    return planet
