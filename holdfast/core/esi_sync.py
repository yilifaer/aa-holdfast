"""Pulling data out of ESI and into our tables.

Everything here is synchronous and safe to call from a Celery task. The ESI
routes used are the Equinox ones that only exist from compatibility date
2026-05-19 onward; see ``holdfast.providers`` for how that date gets sent.

**Rate limiting shapes the design.** The ``corp-structure`` bucket allows 300
tokens per 15 minutes -- a successful call costs two of them, a 304 costs one --
and a detail call is needed per structure. CCP keys the bucket on application
and character together, and an Auth install has one ESI application shared by
every module on it, so the budget here is really a share of what this site's
other apps leave behind.
A large alliance corporation can easily hold 150 hubs and skyhooks, so a naive
"refresh everything, every run" sync blows the budget and leaves half the rows
stale with no way to tell which half.

Instead each run does two things:

1. Pull the two listings (2 calls), on the hourly run. These are complete, so
   they are what decides which structures exist -- creating stubs for new ones
   and deleting ones that are gone.
2. Spend a fixed detail-call budget on the structures whose details are
   stalest, oldest first -- skipping any refreshed in the last
   ``DETAIL_FRESH``, since CCP caches these routes for an hour and asking
   again inside it returns the same answer.

The run is hourly. A corporation too large for one budget is topped up at
:22, :37 and :52 by ``tasks.top_up_all_owners``, which does only step 2 for
whoever still has structures due, so a big corporation rotates as fast as it
did when the whole sync ran every fifteen minutes and a small one costs a
quarter of the calls. A single run still stays under half the bucket,
leaving the rest for the other Auth modules using the same character.

With allianceauth-corptools installed, sovereignty hub details are taken from
its tables first when its copy is recent enough -- see
``adopt_hubs_from_corptools``. Skyhooks are not stored by corptools and always
come from ESI.
"""

from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from esi.exceptions import HTTPNotModified
from eveuniverse.models import EvePlanet, EveSolarSystem, EveType

from ..app_settings import (
    HOLDFAST_DETAIL_CALLS_PER_RUN,
    HOLDFAST_RAIDABLE_CACHE_SECONDS,
    HOLDFAST_SKYHOOK_MIN_UNSECURED,
)
from ..models import (
    Owner,
    PowerState,
    ReagentThreshold,
    Skyhook,
    SkyhookReagent,
    SovCampaign,
    SovHub,
    SovHubReagent,
    SovHubUpgrade,
    SovSystem,
    SystemCostIndex,
)
from ..providers import esi
from ..thirdparty import corptools_source
from .scope import tracked_alliance_ids
from .siphon import detect_siphon, siphon_since

logger = get_extension_logger(__name__)

RAIDABLE_CACHE_KEY = "holdfast_raidable_skyhooks"

# A structure refreshed this recently is not fetched again. CCP caches the
# structure routes for an hour, so asking inside that hour returns what we
# already hold and still costs a token. Ten minutes short of the hour rather
# than the full sixty: the fan-out adds up to two minutes of jitter and a run
# takes a while, and a run that landed at 59 minutes would find nothing due,
# then wait for the next one.
DETAIL_FRESH = timedelta(minutes=50)

try:  # django-esi raises this once a bucket is exhausted
    from esi.exceptions import ESIBucketLimitException
except ImportError:  # pragma: no cover - older django-esi
    class ESIBucketLimitException(Exception):
        pass


# What ESI charges per request, not what we assumed. A successful response
# costs two tokens and a 304 costs one -- so a run of 100 detail calls spends
# about 200 of the 300-token bucket, not 100 of it. The old accounting was out
# by a factor of two in the direction that gets you rate limited.
TOKENS_PER_CALL = 2


class DetailBudget:
    """Counts down the rate-limit tokens one sync run is allowed to spend."""

    def __init__(self, limit: int):
        self.remaining = max(int(limit), 0)
        self.spent = 0
        self.exhausted_by_esi = False

    def take(self, cost: int = TOKENS_PER_CALL) -> bool:
        if self.remaining < cost:
            return False
        self.remaining -= cost
        self.spent += cost
        return True

    @property
    def ran_out(self) -> bool:
        return self.remaining <= 0 or self.exhausted_by_esi


# --------------------------------------------------------------------------
# eveuniverse name resolution
# --------------------------------------------------------------------------


def _resolve_type(type_id, memo):
    """Fetch an EveType on demand, remembering misses so we retry only once."""
    if type_id in memo:
        return memo[type_id]
    try:
        eve_type, _ = EveType.objects.get_or_create_esi(id=type_id)
    except Exception:  # noqa: BLE001 - a missing name must not kill the sync
        logger.warning("Could not resolve type %s", type_id, exc_info=True)
        eve_type = None
    memo[type_id] = eve_type
    return eve_type


def _resolve_system(solar_system_id, memo):
    if solar_system_id in memo:
        return memo[solar_system_id]
    try:
        system, _ = EveSolarSystem.objects.get_or_create_esi(id=solar_system_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve system %s", solar_system_id, exc_info=True)
        system = None
    memo[solar_system_id] = system
    return system


def _resolve_planet(planet_id, memo):
    """Planets are not bulk-imported by eveuniverse, so pull them one by one.

    The result is stored permanently, so this costs one extra ESI call per
    skyhook, ever. It uses the universe bucket, not the corp-structure one.
    """
    if planet_id in memo:
        return memo[planet_id]
    try:
        planet, _ = EvePlanet.objects.get_or_create_esi(id=planet_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve planet %s", planet_id, exc_info=True)
        planet = None
    memo[planet_id] = planet
    return planet


# --------------------------------------------------------------------------
# Sovereignty hubs
# --------------------------------------------------------------------------


def sync_sov_hub_listing(owner: Owner, token, system_memo) -> int:
    """Reconcile which hubs exist. One ESI call, always worth making."""
    # force_refresh, deliberately. django-esi stores the ETag the moment a
    # response arrives, before we have processed a single row -- so if a run
    # dies part way through reconciling (a blown rate-limit bucket will do it),
    # every later run gets a 304 and the partial state becomes permanent. That
    # exact bug once left 97 of 413 skyhooks in the database indefinitely. A
    # listing is two calls out of 300 per 15 minutes; correctness is cheaper.
    try:
        listing = esi.client.Structures.GetCorporationsStructuresSovereigntyHubsListing(
            corporation_id=owner.corporation.corporation_id, token=token
        ).result(force_refresh=True)
    except HTTPNotModified:  # pragma: no cover - unreachable with force_refresh
        logger.info("%s: sov hub listing unchanged", owner)
        return SovHub.objects.filter(owner=owner).count()

    now = timezone.now()
    seen_ids = set()
    for entry in (e.model_dump() for e in listing.sovereignty_hubs):
        hub_id = entry.get("id")
        if hub_id is None:
            continue
        seen_ids.add(hub_id)
        SovHub.objects.update_or_create(
            hub_id=hub_id,
            defaults={
                "owner": owner,
                "solar_system_id": entry.get("solar_system_id"),
                "eve_solar_system": _resolve_system(
                    entry.get("solar_system_id"), system_memo
                ),
                "last_seen_at": now,
            },
        )

    # The listing is complete, so anything missing from it is really gone.
    removed, _ = SovHub.objects.filter(owner=owner).exclude(hub_id__in=seen_ids).delete()
    if removed:
        logger.info("%s: removed %s sov hub(s) no longer owned", owner, removed)
    return len(seen_ids)


def _due(model, owner, now):
    """The owner's rows whose details are older than ``DETAIL_FRESH``."""
    return model.objects.filter(owner=owner).filter(
        Q(detail_updated_at__isnull=True) | Q(detail_updated_at__lt=now - DETAIL_FRESH)
    )


def details_due_count(owner: Owner, now=None) -> int:
    """How many of the owner's structures a detail refresh would still fetch.

    Zero means a run has nothing to ask ESI for -- and, being a few indexed
    counts, is how the top-up task decides whether to bother queueing one.
    """
    now = now or timezone.now()
    return _due(SovHub, owner, now).count() + _due(Skyhook, owner, now).count()


def refresh_details(owner: Owner, token, budget, type_memo):
    """Refresh whatever has gone longest without it, hub or skyhook alike.

    This used to split the budget between the two lists in proportion to their
    sizes, which starved the smaller one: an alliance with one hub and 199
    skyhooks gave the hub ``round(100 * 1/200)``, and Python rounds a half to
    even, so the share was zero. Every run. That hub's fuel was never fetched
    and never alerted on, and nothing anywhere said so.

    One queue ordered by staleness has no shares to get wrong. Whatever waited
    longest goes first, so a single hub among hundreds of skyhooks comes up
    every few runs on its own -- and an alliance with no skyhooks at all still
    spends its whole budget on hubs.

    Only structures not refreshed within ``DETAIL_FRESH`` are queued; see
    ``_due``.

    Returns ``(hubs refreshed, skyhooks refreshed)``.
    """
    now = timezone.now()
    queue = sorted(
        [
            (row.detail_updated_at, index, kind, row)
            for index, (kind, row) in enumerate(
                [("hub", h) for h in _due(SovHub, owner, now)]
                + [("skyhook", s) for s in _due(Skyhook, owner, now)]
            )
        ],
        # Never fetched sorts before everything; the index keeps it stable.
        key=lambda item: (item[0] is not None, item[0] or timezone.now(), item[1]),
    )

    hubs_done = hooks_done = 0
    for _stamp, _index, kind, row in queue:
        if not budget.take():
            break
        try:
            if kind == "hub":
                _fetch_sov_hub_detail(owner, token, row, type_memo)
                hubs_done += 1
            else:
                _fetch_skyhook_detail(owner, token, row, type_memo)
                hooks_done += 1
        except ESIBucketLimitException:
            budget.exhausted_by_esi = True
            logger.warning("%s: ESI bucket exhausted, stopping this run", owner)
            break
    return hubs_done, hooks_done


def _fetch_sov_hub_detail(owner, token, hub, type_memo):
    try:
        detail = esi.client.Structures.GetCorporationsStructuresSovereigntyHubsDetail(
            sovereignty_hub_id=hub.hub_id,
            corporation_id=owner.corporation.corporation_id,
            token=token,
        ).result(
            # Holdfast's ETag is its own, so a 304 only means "same as
            # Holdfast's last read". When what is stored is a corptools copy
            # that is not "same as what we hold", and treating it so would
            # keep a stale copy and date it as an ESI reading. Force a body
            # for those; a hub whose data came from ESI keeps the cheap 304.
            force_refresh=hub.corptools_updated_at is not None
        )
    except HTTPNotModified:
        # Unchanged since our last ETag; the call was still spent, so record
        # the visit or this hub would be picked first again forever.
        now = timezone.now()
        if hub.corptools_updated_at is not None:
            # Unreachable while force_refresh is set for these hubs. If it ever
            # happens, the stored data is a corptools copy, so a 304 must not
            # date it as an ESI reading.
            logger.warning("Hub %s: 304 on a corptools copy; not recorded as an ESI read", hub.hub_id)
            SovHub.objects.filter(hub_id=hub.hub_id).update(detail_updated_at=now)
            return
        SovHub.objects.filter(hub_id=hub.hub_id).update(detail_updated_at=now, esi_read_at=now)
        return
    _store_hub_detail(hub, detail.model_dump(), type_memo, observed_at=timezone.now())


def _store_hub_detail(hub, data, type_memo, observed_at, corptools_updated_at=None):
    """Write one hub's details, from ESI's shape of them.

    ``corptools_updated_at`` is set when ``data`` was read out of
    allianceauth-corptools rather than fetched, and is corptools' own time for
    it; an ESI reading passes None and so clears it, which is how a later
    corptools copy is compared against the newest reading of either kind.

    Split out of the fetch so that what is stored is decided in one place.
    ``observed_at`` is when the reading was taken: it dates the Low edge on an
    upgrade (``low_since``), which is what the den dashboard shows as "offline
    for 3 hours".
    """
    resources = data.get("resources") or {}
    power = resources.get("power") or {}
    workforce = resources.get("workforce") or {}
    window = data.get("vulnerability_window") or {}
    bay = data.get("reagent_bay") or {}

    with transaction.atomic():
        SovHub.objects.filter(hub_id=hub.hub_id).update(
            power_available=power.get("available") or 0,
            power_allocated=power.get("allocated") or 0,
            workforce_available=workforce.get("available") or 0,
            workforce_allocated=workforce.get("allocated") or 0,
            vulnerability_start=window.get("start"),
            vulnerability_end=window.get("end"),
            reagent_bay_updated_at=bay.get("last_updated"),
            fuel_access_list_id=data.get("fuel_access_list_id"),
            workforce_transport=_dump_transport(data.get("workforce_transport")),
            detail_updated_at=timezone.now(),
            corptools_updated_at=corptools_updated_at,
            # An ESI reading (no corptools time) restarts the clock that
            # bounds how long corptools' copy may stand in for it.
            **({"esi_read_at": timezone.now()} if corptools_updated_at is None else {}),
        )
        hub.refresh_from_db()

        reagent_ids = []
        for reagent in bay.get("reagents") or []:
            type_id = reagent.get("type_id")
            if type_id is None:
                continue
            reagent_ids.append(type_id)
            SovHubReagent.objects.update_or_create(
                hub=hub,
                type_id=type_id,
                defaults={
                    "eve_type": _resolve_type(type_id, type_memo),
                    "amount": reagent.get("amount") or 0,
                    "burning_per_hour": reagent.get("burning_per_hour") or 0,
                },
            )
        hub.reagents.exclude(type_id__in=reagent_ids).delete()

        # Read before writing: whether an upgrade was already Low decides if
        # this reading starts the outage or continues one.
        previous = {u.type_id: u for u in hub.upgrades.all()}
        upgrade_ids = []
        for upgrade in data.get("upgrades") or []:
            type_id = upgrade.get("type_id")
            if type_id is None:
                continue
            upgrade_ids.append(type_id)
            state = upgrade.get("power_state") or "Unspecified"
            if state != PowerState.LOW:
                low_since = None
            elif type_id in previous and previous[type_id].power_state == PowerState.LOW:
                # Still down. A NULL here is an outage from before the column
                # existed and stays unknown; do not date it to now.
                low_since = previous[type_id].low_since
            else:
                low_since = observed_at
            SovHubUpgrade.objects.update_or_create(
                hub=hub,
                type_id=type_id,
                defaults={
                    "eve_type": _resolve_type(type_id, type_memo),
                    "power_state": state,
                    "low_since": low_since,
                },
            )
        hub.upgrades.exclude(type_id__in=upgrade_ids).delete()

    hub.recalculate_fuel_expiry()


def _dump_transport(transport):
    """Flatten the workforce_transport union into something JSON-storable."""
    if not transport:
        return {}
    result = {}
    for key in ("configuration", "state"):
        value = transport.get(key)
        if not value:
            continue
        result[key] = {k: v for k, v in value.items() if v is not None}
    return result


# --------------------------------------------------------------------------
# Skyhooks
# --------------------------------------------------------------------------


def sync_skyhook_listing(owner: Owner, token) -> int:
    # force_refresh for the same reason as the sov hub listing above.
    try:
        listing = esi.client.Structures.GetCorporationsStructuresSkyhooksListing(
            corporation_id=owner.corporation.corporation_id, token=token
        ).result(force_refresh=True)
    except HTTPNotModified:  # pragma: no cover - unreachable with force_refresh
        logger.info("%s: skyhook listing unchanged", owner)
        return Skyhook.objects.filter(owner=owner).count()

    now = timezone.now()
    seen_ids = set()
    for entry in (e.model_dump() for e in listing.skyhooks):
        skyhook_id = entry.get("id")
        if skyhook_id is None:
            continue
        seen_ids.add(skyhook_id)
        # Only the raw planet ID here. Naming a planet costs one ESI call to
        # the universe route, and doing 400 of them inline would turn a
        # one-call listing into a two-minute crawl. resolve_pending_planets()
        # fills the names in afterwards on its own budget. eve_planet is
        # deliberately absent from defaults, so a resolved name is never wiped.
        Skyhook.objects.update_or_create(
            skyhook_id=skyhook_id,
            defaults={
                "owner": owner,
                "planet_id": entry.get("planet_id"),
                "last_seen_at": now,
            },
        )

    removed, _ = (
        Skyhook.objects.filter(owner=owner).exclude(skyhook_id__in=seen_ids).delete()
    )
    if removed:
        logger.info("%s: removed %s skyhook(s) no longer owned", owner, removed)
    return len(seen_ids)



def _fetch_skyhook_detail(owner, token, skyhook, type_memo):
    try:
        detail = esi.client.Structures.GetCorporationsStructuresSkyhooksDetail(
            skyhook_id=skyhook.skyhook_id,
            corporation_id=owner.corporation.corporation_id,
            token=token,
        ).result()
    except HTTPNotModified:
        Skyhook.objects.filter(skyhook_id=skyhook.skyhook_id).update(
            detail_updated_at=timezone.now()
        )
        return
    data = detail.model_dump()

    theft = data.get("theft_vulnerability") or {}
    reinforce = data.get("reinforcement_timer") or {}

    # An un-round workforce figure means a den is taking a cut. Worked out here
    # rather than at report time so the value is stored alongside the reading
    # it was derived from.
    workforce = data.get("effective_workforce")
    siphon_percent, siphon_base = detect_siphon(workforce)
    # ``skyhook`` is the row the queue picked, so it still holds the previous
    # reading; the owner's lock keeps anyone else from writing in between.
    siphon_started = siphon_since(
        skyhook.workforce_siphon_percent,
        skyhook.workforce_siphon_since,
        siphon_percent,
        timezone.now(),
    )

    with transaction.atomic():
        Skyhook.objects.filter(skyhook_id=skyhook.skyhook_id).update(
            is_active=bool(data.get("is_active")),
            effective_workforce=workforce,
            workforce_siphon_percent=siphon_percent,
            workforce_base=siphon_base,
            workforce_siphon_since=siphon_started,
            state=data.get("state") or "Unspecified",
            theft_start=theft.get("start"),
            theft_end=theft.get("end"),
            reinforce_end=reinforce.get("end"),
            detail_updated_at=timezone.now(),
        )
        skyhook.refresh_from_db()

        reagent_ids = []
        for reagent in data.get("reagents") or []:
            type_id = reagent.get("type_id")
            if type_id is None:
                continue
            reagent_ids.append(type_id)
            eve_type = _resolve_type(type_id, type_memo)
            # Give the admin a row to tune the moment a new reagent shows up,
            # rather than making them guess type IDs.
            ReagentThreshold.objects.get_or_create(
                type_id=type_id,
                defaults={
                    "eve_type": eve_type,
                    "min_unsecured": HOLDFAST_SKYHOOK_MIN_UNSECURED,
                },
            )
            SkyhookReagent.objects.update_or_create(
                skyhook=skyhook,
                type_id=type_id,
                defaults={
                    "eve_type": eve_type,
                    "secured_stock": reagent.get("secured_stock") or 0,
                    "unsecured_stock": reagent.get("unsecured_stock") or 0,
                    "last_cycle": reagent.get("last_cycle"),
                },
            )
        skyhook.reagents.exclude(type_id__in=reagent_ids).delete()


def resolve_pending_planets(limit: int = 60) -> dict:
    """Put names to skyhook planets, a batch at a time.

    The listing gives us planet IDs; turning those into names (and systems)
    means one call each to the universe route. That route is public and has no
    declared rate limit, but 400 sequential calls still take minutes, so this
    runs on its own schedule instead of blocking a sync. Each planet is fetched
    once ever -- eveuniverse keeps it forever afterwards.
    """
    pending = (
        Skyhook.objects.filter(eve_planet__isnull=True)
        .exclude(planet_id__isnull=True)
        .order_by("skyhook_id")[:limit]
    )
    memo: dict = {}
    resolved = failed = 0

    for skyhook in pending:
        planet = _resolve_planet(skyhook.planet_id, memo)
        if not planet:
            failed += 1
            continue
        Skyhook.objects.filter(skyhook_id=skyhook.skyhook_id).update(
            eve_planet=planet, eve_solar_system=planet.eve_solar_system
        )
        resolved += 1

    # The public raid target list covers all of New Eden, so those planets need
    # names too -- otherwise that page shows bare IDs. Only whatever budget the
    # owned skyhooks did not use goes here; ours come first.
    spare = limit - resolved
    raidable = cached_raidable_skyhooks() or []
    if spare > 0 and raidable:
        known = set(
            EvePlanet.objects.filter(
                id__in=[r.get("planet_id") for r in raidable]
            ).values_list("id", flat=True)
        )
        for row in raidable:
            if spare <= 0:
                break
            planet_id = row.get("planet_id")
            if not planet_id or planet_id in known:
                continue
            if _resolve_planet(planet_id, memo):
                resolved += 1
            else:
                failed += 1
            spare -= 1

    remaining = Skyhook.objects.filter(eve_planet__isnull=True).count()
    if resolved or failed:
        logger.info(
            "Planet resolution: %s resolved, %s failed, %s left",
            resolved, failed, remaining,
        )
    return {"resolved": resolved, "failed": failed, "remaining": remaining}


# --------------------------------------------------------------------------
# Owner entry point
# --------------------------------------------------------------------------


def adopt_hubs_from_corptools(owner: Owner, type_memo, stats=None) -> set:
    """Fill hub details from allianceauth-corptools where its copy will do.

    Only hubs whose details are due are considered -- one refreshed inside
    ``DETAIL_FRESH`` needs nothing from anywhere. A hub is taken from corptools
    only when all of this holds, and otherwise is left for ESI, one hub at a
    time:

    * corptools audits this corporation and its copy is recent enough (see
      ``corptools_source.HUB_MAX_AGE``);
    * corptools has a row for it that passed validation (known power states,
      every type resolved);
    * ESI itself has read the hub within ``HUB_ESI_GAP``: corptools' time is
      that of its hub list, so a failing detail task would otherwise leave
      old data looking fresh for ever;
    * the copy is not older than what we already hold. Without this a run
      could take an Online reading from ESI and the next take an older Low
      one from corptools, clearing and re-raising the upgrade alert;
    * the row is not plausibly mid-write. corptools replaces a hub's reagents
      and upgrades by deleting them and inserting new ones, outside a
      transaction, so an empty list on its side where we hold some of that kind
      means we read it between the two. Each list is judged on its own, since
      the two are read by separate queries and either can be caught alone.

    ``stats`` gains ``hubs_from_corptools`` and ``corptools`` (the reason, or
    what was left to ESI) when corptools is installed, and stays untouched
    when it is not, so an install without it logs exactly what it always did.
    Returns the hub ids adopted.
    """
    now = timezone.now()
    copies = corptools_source.hub_copies(owner, now)
    if not copies.installed:
        return set()
    adopted: set = set()
    left = {}
    if copies.usable:
        for hub in _due(SovHub, owner, now):
            data = copies.rows.get(hub.hub_id)
            if data is None:
                left[hub.hub_id] = copies.rejected.get(hub.hub_id, "not in corptools")
                continue
            reason = _refuse_hub_copy(hub, data, copies.stamp, now)
            if reason:
                left[hub.hub_id] = reason
                continue
            # Always stored, even when the time is the one we took last: the
            # time is the list's, and the details under it may have been
            # rewritten since.
            _store_hub_detail(
                hub,
                data,
                type_memo,
                observed_at=copies.stamp,
                corptools_updated_at=copies.stamp,
            )
            adopted.add(hub.hub_id)
    if stats is not None:
        stats["hubs_from_corptools"] = len(adopted)
        if not copies.usable:
            stats["corptools"] = copies.reason
        elif left:
            stats["corptools"] = f"{len(left)} hub(s) left to ESI: {next(iter(left.values()))}"
        else:
            stats["corptools"] = "ok"
    return adopted


def _refuse_hub_copy(hub, data, stamp, now) -> str:
    """Why this hub's corptools copy must not be used, or '' when it may."""
    if data.get("solar_system_id") != hub.solar_system_id:
        return "corptools has it in another system"
    if hub.esi_read_at is None or now - hub.esi_read_at > corptools_source.HUB_ESI_GAP:
        return "ESI has not read it recently"
    if hub.corptools_updated_at is not None:
        if stamp < hub.corptools_updated_at:
            return "older than what Holdfast already holds"
    elif hub.esi_read_at > stamp - corptools_source.HUB_SETTLE:
        # What we hold is an ESI reading. The stamp is when corptools finished
        # its hub *list*; the details come from separate tasks after it, and
        # one that failed or is still queued leaves the old details under a new
        # stamp. So a stamp is no evidence of newer details unless it is also
        # a settle period past our read. Without this, a stale copy is adopted
        # again right after ESI read fresher data, flips the hub back, and
        # repeats the alert edges.
        return "older than what Holdfast already holds"
    # The detail-level time: the reagent bay's own last_updated, which corptools
    # copies from the same ESI field Holdfast stores. A copy whose bay is older
    # than ours has stale details whatever its list stamp says.
    held = hub.reagent_bay_updated_at
    if held is not None:
        copy_bay = (data.get("reagent_bay") or {}).get("last_updated")
        if copy_bay is None or copy_bay < held:
            return "its details are older than what Holdfast already holds"
        # CCP may not bump the bay time when fuel runs out or power changes, so
        # an equal time does not prove the upgrades are the same. Against an
        # ESI reading, a copy that disagrees on them is not adopted.
        if copy_bay == held and hub.corptools_updated_at is None:
            copy_states = {
                u.get("type_id"): u.get("power_state") for u in data.get("upgrades") or []
            }
            held_states = {u.type_id: u.power_state for u in hub.upgrades.all()}
            if copy_states != held_states:
                return "its upgrade states disagree with Holdfast's ESI reading"
    if not (data.get("reagent_bay") or {}).get("reagents") and hub.reagents.exists():
        return "no reagents; probably mid-write"
    if not data.get("upgrades") and hub.upgrades.exists():
        return "no upgrades; probably mid-write"
    return ""


def update_owner(owner: Owner, detail_budget: int = None, listings: bool = True) -> dict:
    """Refresh one corporation, within this run's detail-call budget.

    ``listings=False`` is the top-up run: it skips the two listings, which the
    hourly run has just pulled, and refreshes only structures still due. When
    nothing is due -- all adopted from corptools, or all refreshed within
    ``DETAIL_FRESH`` -- no token is fetched and ESI is not called at all.

    Returns counts plus ``complete``, which is False when the budget ran out
    before every structure was refreshed. That is a normal steady state for a
    large corporation, not an error -- the remainder is picked up next run.
    """
    if detail_budget is None:
        detail_budget = HOLDFAST_DETAIL_CALLS_PER_RUN
    # The setting is in detail calls, because that is what an operator can
    # picture. The budget counts tokens, because that is what ESI counts.
    budget = DetailBudget(detail_budget * TOKENS_PER_CALL)

    type_memo: dict = {}
    system_memo: dict = {}

    token = None
    if listings:
        token = owner.fetch_token()
        hubs = sync_sov_hub_listing(owner, token, system_memo)
        skyhooks = sync_skyhook_listing(owner, token)
    else:
        hubs = SovHub.objects.filter(owner=owner).count()
        skyhooks = Skyhook.objects.filter(owner=owner).count()

    stats = {"sov_hubs": hubs, "skyhooks": skyhooks}
    adopt_hubs_from_corptools(owner, type_memo, stats)

    hubs_done = hooks_done = 0
    if details_due_count(owner):
        if token is None:
            token = owner.fetch_token()
        hubs_done, hooks_done = refresh_details(owner, token, budget, type_memo)

    stats.update(
        {
            "hub_details": hubs_done,
            "skyhook_details": hooks_done,
            "complete": details_due_count(owner) == 0,
            "rate_limited": budget.exhausted_by_esi,
        }
    )
    return stats


# --------------------------------------------------------------------------
# Public routes -- no token needed
# --------------------------------------------------------------------------


# Which tracked set the sovereignty map was last fetched for. The rows in the
# database cannot tell: a newly tracked alliance has none yet, and an alliance
# holding no sovereignty never will.
SOV_MAP_TRACKED_KEY = "holdfast:sov_map_tracked"


def update_sov_systems() -> int:
    """Refresh the ADM board for tracked alliances from the public route.

    The tracked set comes from the settings page (see ``core.scope``), so it
    can change without the sovereignty map itself changing. That is why a 304
    still prunes, and why the map is fetched afresh whenever the tracked set
    is not the one the last fetch was made for: otherwise a newly tracked
    alliance's rows would never appear until the map's ETag moved. The set is
    remembered in the cache rather than read off the rows, so that an alliance
    with no sovereignty does not force a full fetch every run.
    """
    alliance_ids = tracked_alliance_ids()
    fetched_for = tuple(sorted(alliance_ids))
    if not alliance_ids:
        # Returning early here left the last tracked alliance's rows in place
        # for as long as nothing was tracked.
        stale, _ = SovSystem.objects.all().delete()
        if stale:
            logger.info("No tracked alliances; dropped %s sovereignty row(s)", stale)
        else:
            logger.info("No tracked alliances, skipping sovereignty map update")
        cache.set(SOV_MAP_TRACKED_KEY, fetched_for, None)
        return 0

    held = set(SovSystem.objects.values_list("alliance_id", flat=True).distinct())
    force_refresh = bool(held - alliance_ids) or (
        cache.get(SOV_MAP_TRACKED_KEY) != fetched_for
    )
    try:
        result = esi.client.Sovereignty.GetSovereigntySystems().result(
            force_refresh=force_refresh
        )
    except HTTPNotModified:
        logger.info("Sovereignty map unchanged")
        stale, _ = SovSystem.objects.exclude(alliance_id__in=alliance_ids).delete()
        if stale:
            logger.info("Dropped %s system(s) no longer held by a tracked alliance", stale)
        return SovSystem.objects.count()

    now = timezone.now()
    wanted = []
    for entry in result.solar_systems:
        data = entry.model_dump()
        claim = (data.get("claim") or {}).get("alliance")
        if not claim or claim.get("alliance_id") not in alliance_ids:
            continue
        wanted.append((data.get("solar_system_id"), claim))

    system_ids = [sid for sid, _ in wanted]
    known = {s.id: s for s in EveSolarSystem.objects.filter(id__in=system_ids)}
    system_memo = dict(known)

    for solar_system_id, claim in wanted:
        development = claim.get("development") or {}
        hub = claim.get("sovereignty_hub") or {}
        window = hub.get("vulnerability_window") or {}
        SovSystem.objects.update_or_create(
            solar_system_id=solar_system_id,
            defaults={
                "eve_solar_system": known.get(solar_system_id)
                or _resolve_system(solar_system_id, system_memo),
                "alliance_id": claim.get("alliance_id"),
                "corporation_id": claim.get("corporation_id"),
                "claimed_since": claim.get("claimed_since"),
                "is_capital_system": bool(claim.get("is_capital_system")),
                "activity_defense_multiplier": development.get(
                    "activity_defense_multiplier"
                ),
                "military_level": development.get("military_level"),
                "industrial_level": development.get("industrial_level"),
                "strategic_level": development.get("strategic_level"),
                "hub_id": hub.get("id"),
                "vulnerability_start": window.get("start"),
                "vulnerability_end": window.get("end"),
                "updated_at": now,
            },
        )

    stale, _ = SovSystem.objects.exclude(solar_system_id__in=system_ids).delete()
    if stale:
        logger.info("Dropped %s system(s) no longer held by a tracked alliance", stale)

    cache.set(SOV_MAP_TRACKED_KEY, fetched_for, None)
    return len(wanted)


def update_system_costs() -> int:
    """Refresh industry cost indices for systems our tracked alliances hold.

    One public call returns the whole cluster, so this costs the same whether
    an alliance holds five systems or five hundred -- which matters, because
    this app is meant to run for alliances much larger than the one it was
    written for.
    """
    tracked = SovSystem.objects.values_list("solar_system_id", "alliance_id")
    wanted = dict(tracked)
    if not wanted:
        # Nothing tracked means nothing to keep: the sovereignty rows these
        # were derived from are already gone.
        stale, _ = SystemCostIndex.objects.all().delete()
        if stale:
            logger.info("No tracked systems; dropped %s cost index row(s)", stale)
        else:
            logger.info("No tracked systems, skipping industry cost update")
        return 0

    try:
        result = esi.client.Industry.GetIndustrySystems().result()
    except HTTPNotModified:
        logger.info("Industry cost indices unchanged")
        return SystemCostIndex.objects.count()

    known = {s.id: s for s in EveSolarSystem.objects.filter(id__in=wanted)}
    now = timezone.now()
    written = 0

    for entry in result:
        data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
        solar_system_id = data.get("solar_system_id")
        if solar_system_id not in wanted:
            continue

        indices = {
            item.get("activity"): item.get("cost_index")
            for item in (data.get("cost_indices") or [])
            if item.get("activity")
        }
        columns = (
            "manufacturing",
            "reaction",
            "copying",
            "invention",
            "researching_time_efficiency",
            "researching_material_efficiency",
        )
        SystemCostIndex.objects.update_or_create(
            solar_system_id=solar_system_id,
            defaults={
                "eve_solar_system": known.get(solar_system_id),
                "alliance_id": wanted.get(solar_system_id),
                **{name: indices.get(name) for name in columns},
                "other_indices": {
                    k: v for k, v in indices.items() if k not in columns and k != "none"
                },
                "updated_at": now,
            },
        )
        written += 1

    stale, _ = SystemCostIndex.objects.exclude(solar_system_id__in=wanted).delete()
    if stale:
        logger.info("Dropped cost indices for %s system(s) no longer held", stale)
    return written


def update_campaigns() -> int:
    """Pull Entosis campaigns, keeping the ones aimed at systems we hold.

    A campaign against our own sovereignty hub is the only public signal that
    the hub has actually been reinforced -- the structure route reports the
    daily vulnerability window whether or not anyone turned up.
    """
    tracked = dict(SovSystem.objects.values_list("solar_system_id", "alliance_id"))
    try:
        result = esi.client.Sovereignty.GetSovereigntyCampaigns().result()
    except HTTPNotModified:
        return SovCampaign.objects.count()

    now = timezone.now()
    known = {s.id: s for s in EveSolarSystem.objects.filter(id__in=tracked)}
    seen = []

    for entry in result:
        data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
        solar_system_id = data.get("solar_system_id")
        if solar_system_id not in tracked:
            continue
        campaign_id = data.get("campaign_id")
        seen.append(campaign_id)
        SovCampaign.objects.update_or_create(
            campaign_id=campaign_id,
            defaults={
                "structure_id": data.get("structure_id"),
                "solar_system_id": solar_system_id,
                "eve_solar_system": known.get(solar_system_id),
                "constellation_id": data.get("constellation_id"),
                "event_type": data.get("event_type") or "",
                "defender_id": data.get("defender_id"),
                "defender_score": data.get("defender_score"),
                "attackers_score": data.get("attackers_score"),
                "start_time": data.get("start_time"),
                "updated_at": now,
            },
        )

    # A campaign that disappears has been resolved one way or the other.
    SovCampaign.objects.exclude(campaign_id__in=seen).delete()
    return len(seen)


def cached_raidable_skyhooks():
    """The list ``refresh_raidable`` last stored, or None before it has.

    Pages read this and nothing else: a web request must not wait on ESI, or
    turn an ESI error into a 500. Held in Redis rather than the database: it
    is ~200 rows that turn over every few minutes, and writing that to an SD
    card is pointless.
    """
    return cache.get(RAIDABLE_CACHE_KEY)


def refresh_raidable_skyhooks() -> list:
    """Fetch the public raidable list and store it for the pages.

    With a list stored, the ETag is used and a 304 means that list is still
    right: it is kept and stored again, with a fresh lifetime. Returning []
    there emptied the page until CCP next changed the list. With nothing
    stored there is nothing for a 304 to refer to, so the fetch is forced.
    """
    previous = cache.get(RAIDABLE_CACHE_KEY)
    try:
        result = esi.client.Activities.GetSkyhooksRaidable().result(
            force_refresh=previous is None
        )
    except HTTPNotModified:
        rows = previous
    else:
        rows = [entry.model_dump() for entry in result.skyhooks]
    cache.set(RAIDABLE_CACHE_KEY, rows, HOLDFAST_RAIDABLE_CACHE_SECONDS)
    return rows
