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

With allianceauth-corptools installed, an operator's dens and tactical
operations are read from its tables when its copy is recent and not older than
what Holdfast already holds (``thirdparty.corptools_source``), which leaves the
notifications as this module's only ESI call. Without corptools, or when its
copy will not do, nothing changes.
"""

from datetime import timedelta

import yaml
from allianceauth.services.hooks import get_extension_logger
from django.utils import timezone
from esi.exceptions import HTTPNotModified
from eveuniverse.models import EvePlanet

from ..app_settings import (
    HOLDFAST_DEN_DETAIL_CALLS_PER_RUN,
    HOLDFAST_DEN_EVENT_RETENTION_DAYS,
    HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES,
    HOLDFAST_DEN_IDLE_HOURS,
    HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS,
    HOLDFAST_DEN_NOTIFICATION_TYPES,
)
from ..models import (
    TEMPERATE_PLANET_TYPE_ID,
    DenCharacter,
    DenClaim,
    DenEvent,
    DenSlot,
    HoldfastConfig,
    MercenaryDen,
    MercenaryTacticalOperation,
    Skyhook,
)
from ..providers import esi
from ..thirdparty import corptools_source
from .esi_sync import ESIBucketLimitException

logger = get_extension_logger(__name__)


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
    # all. Slots whose planet has not been named yet are left strictly alone:
    # one that is in fact temperate would come back as a new slot, and its
    # claims would no longer point at it.
    stale = DenSlot.objects.exclude(skyhook__in=temperate).exclude(
        skyhook__eve_planet__isnull=True
    )
    removed = stale.count()
    stale.delete()

    # A claim outlives its slot as history (DenClaim.slot is SET_NULL). One
    # still pending can never be decided -- the den admin page only shows
    # claims on slots that exist -- so it is closed rather than left waiting
    # on the member's page for good.
    closed = DenClaim.objects.filter(
        slot__isnull=True, status=DenClaim.Status.PENDING
    ).update(
        status=DenClaim.Status.REJECTED,
        decided_at=timezone.now(),
        decision_note="The slot no longer exists: its skyhook is gone.",
    )

    if created or removed or closed:
        logger.info("Den slots: +%s, -%s, %s orphaned claim(s) closed", created, removed, closed)
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
    _store_den_detail(den, detail.model_dump(), timezone.now())


def _store_den_detail(den, data, observed_at):
    """Write one den's details, from ESI's shape of them.

    Shared by the fetch and the corptools copy, so what is stored is decided
    in one place. ``observed_at`` is when the reading was taken.
    """
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
        detail_updated_at=observed_at,
    )
    den.refresh_from_db()
    link_den_to_slot(den)


def store_dens_from_copy(den_character, copies, planet_memo) -> int:
    """Make this character's dens match a corptools copy.

    The copy is the whole truth about which dens exist, as the listing is on
    the ESI path: new ones are created, the rest updated, and any the copy
    does not have are removed. Returns how many dens the copy holds.
    """
    for den_id, data in copies.rows.items():
        planet_id = data.get("planet_id")
        planet = _resolve_planet(planet_id, planet_memo)
        den, _ = MercenaryDen.objects.update_or_create(
            den_id=den_id,
            defaults={
                "den_character": den_character,
                "eve_character": den_character.character,
                "planet_id": planet_id,
                "eve_planet": planet,
                "eve_solar_system": planet.eve_solar_system if planet else None,
                "last_seen_at": copies.stamp,
            },
        )
        _store_den_detail(den, data, copies.stamp)

    removed, _ = (
        MercenaryDen.objects.filter(den_character=den_character)
        .exclude(den_id__in=list(copies.rows))
        .delete()
    )
    if removed:
        logger.info("%s: %s den(s) gone", den_character, removed)
    return len(copies.rows)


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

    # The listing is the whole truth about which operations exist; the detail
    # calls only add expiry and type to ones we already know are there. Taking
    # the id set from the listing up front means a bucket limit part way
    # through costs us some detail, not the rows themselves. Deleting on what
    # we managed to fetch made a rate limit look like an operation ending.
    entries = [e.model_dump() for e in listing.operations]
    seen = {e["id"] for e in entries if e.get("id")}

    for entry in entries:
        operation_id = entry.get("id")
        if not operation_id:
            continue
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
        _store_operation(
            den_character,
            operation_id,
            data,
            data.get("mercenary_den_id") or entry.get("mercenary_den_id"),
            timezone.now(),
        )

    MercenaryTacticalOperation.objects.filter(den_character=den_character).exclude(
        operation_id__in=seen
    ).delete()
    return len(seen)


def _store_operation(den_character, operation_id, data, den_id, updated_at):
    """Write one operation, from ESI's shape of it."""
    MercenaryTacticalOperation.objects.update_or_create(
        operation_id=operation_id,
        defaults={
            "den_character": den_character,
            "den": MercenaryDen.objects.filter(den_id=den_id).first(),
            "mercenary_den_id": den_id,
            # Stored raw: dungeon ids are not inventory type ids, and
            # resolving one against the type tables produces a confident
            # wrong answer rather than an empty one.
            "dungeon_type_id": data.get("dungeon_type_id"),
            "state": data.get("state") or "Unspecified",
            "expires": data.get("expires"),
            "updated_at": updated_at,
        },
    )


def store_operations_from_copy(den_character, copies) -> int:
    """Make this character's operations match a corptools copy.

    The operation's id is the primary key, and the alert log and the
    operator's dismissal both hang off it. corptools spells the id as a UUID
    and may not match the spelling already stored, so an operation we already
    hold under another spelling of the same UUID keeps the key it has;
    otherwise it would be a new row, announced again, with the dismissal
    gone.
    """
    held = {
        corptools_source.normalise_operation_id(key): key
        for key in MercenaryTacticalOperation.objects.filter(
            den_character=den_character
        ).values_list("operation_id", flat=True)
    }
    keys = set()
    for raw_id, data in copies.rows.items():
        key = held.get(corptools_source.normalise_operation_id(raw_id), raw_id)
        keys.add(key)
        _store_operation(
            den_character, key, data, data.get("mercenary_den_id"), copies.stamp
        )
    MercenaryTacticalOperation.objects.filter(den_character=den_character).exclude(
        operation_id__in=keys
    ).delete()
    return len(keys)


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
        # force_refresh, for the reason the sov hub listing gives at length:
        # django-esi banks the ETag as soon as a response arrives, so anything
        # that reads this list and does not record what it saw leaves a 304
        # behind it -- and the events in that response are then invisible
        # until CCP sends a newer one. A listing that decides whether an event
        # exists cannot be allowed to go stale that way.
        result = esi.client.Character.GetCharactersCharacterIdNotifications(
            character_id=den_character.character.character_id, token=token
        ).result(force_refresh=True)
    except HTTPNotModified:  # pragma: no cover - unreachable with force_refresh
        return 0
    except ESIBucketLimitException:
        logger.warning("%s: notification bucket exhausted", den_character)
        return 0

    now = timezone.now()
    cutoff = notification_cutoff(den_character, now)
    retained_from = now - timedelta(days=HOLDFAST_DEN_EVENT_RETENTION_DAYS)

    created = 0
    for item in result:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        kind = data.get("type")
        if kind not in HOLDFAST_DEN_NOTIFICATION_TYPES:
            continue
        timestamp = data.get("timestamp")
        # Past retention it would only be pruned again tonight; ESI keeps a
        # notification as long as it likes, and reading it back in every sync
        # would undo the prune.
        if not timestamp or timestamp < retained_from:
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
                # Older than the window is backfill: recorded for the
                # timeline, never announced.
                "is_alerted": timestamp < cutoff,
            },
        )
        created += int(was_created)

    if den_character.notifications_synced_at is None:
        # An update: the sync holds this instance for a while, and a save
        # would put back whatever it read at the start.
        DenCharacter.objects.filter(
            pk=den_character.pk, notifications_synced_at__isnull=True
        ).update(notifications_synced_at=now)
        den_character.notifications_synced_at = now
    return created


def notification_cutoff(den_character: DenCharacter, now):
    """Notifications older than this are backfill: kept, never announced."""
    cutoff = now - timedelta(hours=HOLDFAST_DEN_NOTIFICATION_MAX_AGE_HOURS)
    if den_character.notifications_synced_at is None:
        # First sync for this operator. Suppressing *everything* here was too
        # blunt: the first thing that happens to a new operator's den is
        # precisely what they registered to hear about, and it was the one
        # event guaranteed to be swallowed. Narrow the window instead of
        # closing it.
        cutoff = max(
            cutoff, now - timedelta(minutes=HOLDFAST_DEN_FIRST_SYNC_GRACE_MINUTES)
        )
    return cutoff


def prune_den_events() -> int:
    """Drop den notifications past HOLDFAST_DEN_EVENT_RETENTION_DAYS.

    Their text carries the attacker's character, corporation and alliance --
    people with no account here -- and nothing shows one that old: the den
    timers page lists a week of events, and an attack alert gives up within a
    day.
    """
    cutoff = timezone.now() - timedelta(days=HOLDFAST_DEN_EVENT_RETENTION_DAYS)
    deleted, _ = DenEvent.objects.filter(timestamp__lt=cutoff).delete()
    return deleted


def parse_notification_body(text: str) -> dict:
    """Parse a notification body. It is YAML, anchors and all.

    A real ``MercenaryDenAttacked`` body looks like this::

        aggressorAllianceName: <a href="showinfo:16159//99003995">Invidia</a>
        aggressorCharacterID: 2116759862
        armorPercentage: 100.0
        itemID: &id001 1053328404172
        mercenaryDenShowInfoData: [showinfo, 85230, *id001]
        shieldPercentage: 94.96407739693383

    Note ``itemID``, not ``mercenaryDenID``, and note the YAML anchor in front
    of the number. Scanning lines for a key and calling ``.isdigit()`` on what
    follows -- which is what this used to do -- finds the right line and then
    rejects ``&id001 1053328404172``, so the event lands with no den attached.
    Handing the whole thing to a YAML parser resolves the anchor and gets the
    other fields for free.
    """
    if not text:
        return {}
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        logger.warning("Could not parse a notification body as YAML", exc_info=True)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _den_id_from_text(text: str):
    """The den's item ID, from whichever key this notification type uses."""
    body = parse_notification_body(text)
    for key in ("mercenaryDenID", "itemID", "structureID"):
        value = body.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
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


def _newest_den_reading(den_character):
    """The latest time anything was read about this character's dens."""
    times = []
    for den in MercenaryDen.objects.filter(den_character=den_character):
        times += [t for t in (den.detail_updated_at, den.last_seen_at) if t]
    return max(times, default=None)


def _newest_operation_reading(den_character):
    return (
        MercenaryTacticalOperation.objects.filter(den_character=den_character)
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first()
    )


def _take_den_copy(den_character, copies):
    """Whether to use this corptools copy of the dens, and if not, why.

    Returns ``(use, unchanged, reason)``. A copy is refused when it is older
    than anything Holdfast has read about these dens: taking it would flip a
    den back to an earlier state and announce the change twice. It is also
    refused when it is empty while we hold dens, because corptools writes
    these a den at a time and an empty copy is as likely to be a read in the
    middle of that as a den that is really gone -- ESI can tell the two apart.
    ``unchanged`` means it is the very copy already stored; nothing to write.
    """
    if not copies.usable:
        return False, False, copies.reason
    newest = _newest_den_reading(den_character)
    if newest is not None and copies.stamp < newest:
        return False, False, "older than what Holdfast already holds"
    if not copies.rows and newest is not None:
        return False, False, "no dens in the copy; probably mid-write"
    return True, copies.stamp == newest, ""


def _take_operation_copy(den_character, copies):
    if not copies.usable:
        return False, False, copies.reason
    newest = _newest_operation_reading(den_character)
    if newest is not None and copies.stamp < newest:
        return False, False, "older than what Holdfast already holds"
    return True, copies.stamp == newest, ""


def update_den_character(den_character: DenCharacter) -> dict:
    """Refresh one operator's dens, operations and notifications.

    Dens and operations come from allianceauth-corptools when it holds a copy
    recent enough and not older than ours; otherwise from ESI, as ever. The
    order is fixed -- dens, then operations (which link to dens), then
    notifications -- and notifications always come from ESI: corptools does not
    keep them, and the attack alerts are built on them. The token is fetched
    first either way because of that.
    """
    token = den_character.fetch_token()
    planet_memo: dict = {}
    stats: dict = {}

    den_copies = corptools_source.den_copies(den_character)
    use, unchanged, reason = _take_den_copy(den_character, den_copies)
    if use:
        dens = len(den_copies.rows) if unchanged else store_dens_from_copy(
            den_character, den_copies, planet_memo
        )
        details = 0
    else:
        dens = sync_den_listing(den_character, token, planet_memo)
        details = refresh_den_details(
            den_character, token, HOLDFAST_DEN_DETAIL_CALLS_PER_RUN
        )

    operation_copies = corptools_source.operation_copies(den_character)
    use_ops, ops_unchanged, ops_reason = _take_operation_copy(
        den_character, operation_copies
    )
    if use_ops:
        operations = (
            len(operation_copies.rows)
            if ops_unchanged
            else store_operations_from_copy(den_character, operation_copies)
        )
    else:
        operations = sync_operations(den_character, token)

    events = sync_notifications(den_character, token)

    stats.update(
        {
            "dens": dens,
            "den_details": details,
            "operations": operations,
            "new_events": events,
        }
    )
    # Only when corptools is installed, so an install without it logs and
    # reports exactly what it always did.
    if den_copies.installed or operation_copies.installed:
        stats["dens_from"] = "corptools" if use else "esi"
        stats["operations_from"] = "corptools" if use_ops else "esi"
        stats["corptools"] = "; ".join(
            f"{what}: {why}"
            for what, why in (("dens", reason), ("operations", ops_reason))
            if why
        ) or "ok"
    return stats


def note_idle(den_character: DenCharacter, stats: dict) -> None:
    """Back off a character whose sync found nothing; wake one that has
    something again.

    "Nothing" is no dens, no operations and no new notifications. A den that
    comes down leaves the character idle too, which is right: there is
    nothing left to watch. Anchoring a new den is noticed within
    HOLDFAST_DEN_IDLE_HOURS at the latest, or at once if the operator
    registers or claims through the app, both of which sync directly.
    """
    found = (stats.get("dens") or 0) + (stats.get("operations") or 0) + (
        stats.get("new_events") or 0
    )
    idle_until = (
        None if found else timezone.now() + timedelta(hours=HOLDFAST_DEN_IDLE_HOURS)
    )
    if den_character.idle_until != idle_until:
        den_character.idle_until = idle_until
        den_character.save(update_fields=["idle_until"])


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
