"""Pulling data out of ESI and into our tables.

Everything here is synchronous and safe to call from a Celery task. The ESI
routes used are the Equinox ones that only exist from compatibility date
2026-05-19 onward; see ``holdfast.providers`` for how that date gets sent.

**Rate limiting shapes the design.** The ``corp-structure`` bucket allows 300
requests per 15 minutes per token, and a detail call is needed per structure.
A large alliance corporation can easily hold 150 hubs and skyhooks, so a naive
"refresh everything, every run" sync blows the budget and leaves half the rows
stale with no way to tell which half.

Instead each run does two things:

1. Pull the two listings (2 calls). These are complete, so they are what
   decides which structures exist -- creating stubs for new ones and deleting
   ones that are gone.
2. Spend a fixed detail-call budget on the structures whose details are
   stalest, oldest first.

Structures therefore rotate through refresh. With the default budget and a
15-minute schedule, every structure is refreshed comfortably inside the one
hour that CCP caches these routes for anyway, at about a fifth of the rate
limit -- leaving room for other apps sharing the same token's bucket.
"""

import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from esi.exceptions import HTTPNotModified
from eveuniverse.models import EvePlanet, EveSolarSystem, EveType

from ..app_settings import (
    HOLDFAST_DETAIL_CALLS_PER_RUN,
    HOLDFAST_SKYHOOK_MIN_UNSECURED,
    HOLDFAST_RAIDABLE_CACHE_SECONDS,
    HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS,
)
from ..models import (
    Owner,
    SovCampaign,
    SystemCostIndex,
    ReagentThreshold,
    Skyhook,
    SkyhookReagent,
    SovHub,
    SovHubReagent,
    SovHubUpgrade,
    SovSystem,
)
from ..providers import esi
from .siphon import detect_siphon

logger = logging.getLogger(__name__)

RAIDABLE_CACHE_KEY = "holdfast_raidable_skyhooks"

try:  # django-esi raises this once a bucket is exhausted
    from esi.exceptions import ESIBucketLimitException
except ImportError:  # pragma: no cover - older django-esi
    class ESIBucketLimitException(Exception):
        pass


class DetailBudget:
    """Counts down the detail calls one sync run is allowed to make."""

    def __init__(self, limit: int):
        self.remaining = max(int(limit), 0)
        self.spent = 0
        self.exhausted_by_esi = False

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.spent += 1
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


def refresh_sov_hub_details(owner: Owner, token, budget, type_memo) -> int:
    """Spend budget refreshing the stalest hubs first."""
    refreshed = 0
    stalest = SovHub.objects.filter(owner=owner).order_by(
        F("detail_updated_at").asc(nulls_first=True)
    )
    for hub in stalest:
        if not budget.take():
            break
        try:
            _fetch_sov_hub_detail(owner, token, hub, type_memo)
        except ESIBucketLimitException:
            budget.exhausted_by_esi = True
            logger.warning("%s: ESI bucket exhausted, stopping this run", owner)
            break
        refreshed += 1
    return refreshed


def _fetch_sov_hub_detail(owner, token, hub, type_memo):
    try:
        detail = esi.client.Structures.GetCorporationsStructuresSovereigntyHubsDetail(
            sovereignty_hub_id=hub.hub_id,
            corporation_id=owner.corporation.corporation_id,
            token=token,
        ).result()
    except HTTPNotModified:
        # Unchanged since our last ETag; the call was still spent, so record
        # the visit or this hub would be picked first again forever.
        SovHub.objects.filter(hub_id=hub.hub_id).update(detail_updated_at=timezone.now())
        return
    data = detail.model_dump()

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

        upgrade_ids = []
        for upgrade in data.get("upgrades") or []:
            type_id = upgrade.get("type_id")
            if type_id is None:
                continue
            upgrade_ids.append(type_id)
            SovHubUpgrade.objects.update_or_create(
                hub=hub,
                type_id=type_id,
                defaults={
                    "eve_type": _resolve_type(type_id, type_memo),
                    "power_state": upgrade.get("power_state") or "Unspecified",
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


def refresh_skyhook_details(owner: Owner, token, budget, type_memo) -> int:
    refreshed = 0
    stalest = Skyhook.objects.filter(owner=owner).order_by(
        F("detail_updated_at").asc(nulls_first=True)
    )
    for skyhook in stalest:
        if not budget.take():
            break
        try:
            _fetch_skyhook_detail(owner, token, skyhook, type_memo)
        except ESIBucketLimitException:
            budget.exhausted_by_esi = True
            logger.warning("%s: ESI bucket exhausted, stopping this run", owner)
            break
        refreshed += 1
    return refreshed


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

    with transaction.atomic():
        Skyhook.objects.filter(skyhook_id=skyhook.skyhook_id).update(
            is_active=bool(data.get("is_active")),
            effective_workforce=workforce,
            workforce_siphon_percent=siphon_percent,
            workforce_base=siphon_base,
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
    if spare > 0:
        known = set(
            EvePlanet.objects.filter(
                id__in=[r.get("planet_id") for r in get_raidable_skyhooks()]
            ).values_list("id", flat=True)
        )
        for row in get_raidable_skyhooks():
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


def update_owner(owner: Owner, detail_budget: int = None) -> dict:
    """Refresh one corporation, within this run's detail-call budget.

    Returns counts plus ``complete``, which is False when the budget ran out
    before every structure was refreshed. That is a normal steady state for a
    large corporation, not an error -- the remainder is picked up next run.
    """
    token = owner.fetch_token()
    if detail_budget is None:
        detail_budget = HOLDFAST_DETAIL_CALLS_PER_RUN
    budget = DetailBudget(detail_budget)

    type_memo: dict = {}
    system_memo: dict = {}

    hubs = sync_sov_hub_listing(owner, token, system_memo)
    skyhooks = sync_skyhook_listing(owner, token)

    total = hubs + skyhooks
    # Split the budget proportionally so neither list starves the other.
    hub_share = round(budget.remaining * (hubs / total)) if total else 0
    hub_budget = DetailBudget(min(hub_share, budget.remaining))
    hubs_done = refresh_sov_hub_details(owner, token, hub_budget, type_memo)
    budget.remaining -= hub_budget.spent
    budget.exhausted_by_esi = hub_budget.exhausted_by_esi

    hooks_done = 0
    if not budget.exhausted_by_esi:
        hook_budget = DetailBudget(budget.remaining)
        hooks_done = refresh_skyhook_details(owner, token, hook_budget, type_memo)
        budget.remaining -= hook_budget.spent
        budget.exhausted_by_esi = hook_budget.exhausted_by_esi

    return {
        "sov_hubs": hubs,
        "skyhooks": skyhooks,
        "hub_details": hubs_done,
        "skyhook_details": hooks_done,
        "complete": (hubs_done + hooks_done) >= total,
        "rate_limited": budget.exhausted_by_esi,
    }


# --------------------------------------------------------------------------
# Public routes -- no token needed
# --------------------------------------------------------------------------


def tracked_alliance_ids() -> set:
    """Alliances we keep sovereignty rows for."""
    ids = set(HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS)
    for owner in Owner.objects.filter(is_enabled=True).select_related(
        "corporation__alliance"
    ):
        if owner.alliance_id:
            ids.add(owner.alliance_id)
    return ids


def update_sov_systems() -> int:
    """Refresh the ADM board for tracked alliances from the public route."""
    alliance_ids = tracked_alliance_ids()
    if not alliance_ids:
        logger.info("No tracked alliances, skipping sovereignty map update")
        return 0

    try:
        result = esi.client.Sovereignty.GetSovereigntySystems().result()
    except HTTPNotModified:
        logger.info("Sovereignty map unchanged")
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


def get_raidable_skyhooks(force_refresh: bool = False) -> list:
    """The public list of skyhooks in or approaching a theft window.

    Held in Redis rather than the database: it is ~200 rows that turn over
    every few minutes, and writing that to an SD card hourly is pointless.
    """
    if not force_refresh:
        cached = cache.get(RAIDABLE_CACHE_KEY)
        if cached is not None:
            return cached

    try:
        result = esi.client.Activities.GetSkyhooksRaidable().result(
            force_refresh=force_refresh
        )
    except HTTPNotModified:
        return cache.get(RAIDABLE_CACHE_KEY) or []

    rows = [entry.model_dump() for entry in result.skyhooks]
    cache.set(RAIDABLE_CACHE_KEY, rows, HOLDFAST_RAIDABLE_CACHE_SECONDS)
    return rows
