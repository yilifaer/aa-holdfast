"""Read what allianceauth-corptools already fetched, instead of asking ESI again.

corptools audits corporations and characters on its own schedule and keeps the
sovereignty hubs, mercenary dens and tactical operations it finds. Holdfast
needs the same three things, so on an install that has corptools -- and only
there -- the details can come out of its tables for no ESI call at all.

This module reads corptools' tables and never imports it: models are looked up
through ``apps.get_model`` and every field Holdfast relies on is checked once
per process, so an install without corptools pays nothing, and one with an
older or changed corptools falls back to ESI with one warning instead of
raising inside a sync. Nothing here writes anywhere; the caller stores what it
accepts through the same functions its ESI path uses, so a copy cannot be
stored differently from a fetch.

What a copy has to pass before anyone uses it:

* corptools audits that corporation or character, and recorded an update time
  for that kind of data;
* the time is recent enough (the limits below, each justified);
* every enum value is one Holdfast knows and every type resolved. corptools
  stores states lower-cased and types as foreign keys that become NULL when the
  type is not in its table; a value we cannot map is a reason to ask ESI, not
  to guess.

The caller adds one more rule that needs Holdfast's own data: a copy never
replaces something newer. See ``core.esi_sync.adopt_hubs_from_corptools`` and
``core.den_sync``.

What corptools does not have stays on ESI: skyhooks, structure listings, den
notifications and the public routes.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import DatabaseError
from django.utils import timezone

from ..models import EvolutionLevel, MercenaryDen, MercenaryTacticalOperation, PowerState

logger = get_extension_logger(__name__)

# How old a corptools copy may be before Holdfast asks ESI itself. Each limit
# is corptools' own refresh interval plus slack for a late run.
#
# Hubs: corptools audits a corporation hourly, and the hub list sits at the
# end of a chain of other updates, so its time can fall well past the hour.
# 60 minutes plus 30.
#
# That time is when corptools finished the hub *list*, not the details. Each
# hub's details are fetched afterwards by a separate low-priority task that is
# rate-limited and that does not touch the time when it fails. So two more
# limits stand between the time and the data:
#
# * HUB_SETTLE: a list finished less than this long ago may still have its
#   details half-fetched, so it is not trusted yet.
# * HUB_ESI_GAP: whatever the time says, a hub is read from ESI itself at
#   least this often, which bounds how stale a failing detail task can leave us.
HUB_MAX_AGE = timedelta(minutes=90)
HUB_SETTLE = timedelta(minutes=20)
HUB_ESI_GAP = timedelta(hours=3)
# Dens: a character with dens is picked up at :15 and :45, but corptools skips
# a den it updated within the hour, so a den is really refreshed about every
# 90 minutes. Plus 10.
DEN_MAX_AGE = timedelta(minutes=100)
# Operations: refreshed on every character run, 30 minutes apart. Plus 15.
OPERATION_MAX_AGE = timedelta(minutes=45)
# A time in the future is a clock fault or a corrupt row, not a fresh copy.
# A few minutes is ordinary skew between two hosts.
FUTURE_SKEW = timedelta(minutes=5)

# What Holdfast reads from corptools, per model. Checked once per process.
REQUIRED_FIELDS = {
    "CorporationAudit": ("corporation", "update_timestamps"),
    "CharacterAudit": ("character", "update_timestamps"),
    "SovereigntyHub": (
        "corporation", "hub_id", "solar_system_id", "fuel_access_list_id",
        "power_allocated", "power_available", "workforce_allocated",
        "workforce_available", "vuln_window_start", "vuln_window_end",
        "reagent_last_updated", "workforce_transport", "reagents", "upgrades",
    ),
    "SovereigntyHubReagent": ("hub", "type_name", "amount", "burning_per_hour"),
    "SovereigntyHubUpgrade": ("hub", "type_name", "power_state"),
    "CharacterMercenaryDen": (
        "character", "den_id", "planet_id", "type_id", "state",
        "development_amount", "development_level", "anarchy_amount",
        "anarchy_level", "infomorph_amount", "reinforcement_end", "skyhook_id",
        "skyhook_corporation_id",
    ),
    "CharacterMercenaryTacticalOperation": (
        "character", "operation_id", "mercenary_den_id", "dungeon_type_id",
        "state", "expires",
    ),
}

_UNSET = object()
_models_cache = _UNSET


def reset():
    """Forget what was found about corptools. For tests."""
    global _models_cache
    _models_cache = _UNSET


def _models():
    """corptools' models by name, or None when they cannot be used.

    None for "not installed" costs one dictionary lookup and says nothing. None
    for "installed but not what we expect" is logged once per process.
    """
    global _models_cache
    if _models_cache is not _UNSET:
        return _models_cache
    if not apps.is_installed("corptools"):
        return None
    found = {}
    try:
        for name, names in REQUIRED_FIELDS.items():
            model = apps.get_model("corptools", name)
            for field_name in names:
                try:
                    model._meta.get_field(field_name)
                except FieldDoesNotExist:
                    raise _Unusable(name, field_name) from None
            found[name] = model
    except LookupError as exc:
        logger.warning(
            "corptools is installed but has no %s; Holdfast reads ESI instead", exc
        )
        _models_cache = None
    except _Unusable as exc:
        logger.warning(
            "corptools is installed but its %s has no %s; Holdfast reads ESI instead",
            exc.model,
            exc.field,
        )
        _models_cache = None
    else:
        _models_cache = found
    return _models_cache


class _Unusable(Exception):
    def __init__(self, model, field_name):
        super().__init__(model, field_name)
        self.model = model
        self.field = field_name


def available() -> bool:
    """Whether corptools is installed and looks like the version we read."""
    return _models() is not None


@dataclass
class Copies:
    """What corptools holds for one corporation or character.

    ``installed`` False means there is nothing to say and nothing to log.
    ``usable`` is the question the caller asks. ``rows`` is keyed by the
    structure's id and holds ESI-shaped dicts, which is what Holdfast's own
    store functions take. ``rejected`` maps a hub id to why only that hub was
    refused; a refusal of the whole set is in ``reason``.
    """

    installed: bool = False
    stamp: datetime = None
    rows: dict = field(default_factory=dict)
    reason: str = ""
    rejected: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.installed and self.stamp and not self.reason)


class _Reject(Exception):
    """One row, or the whole set, cannot be used."""


# --------------------------------------------------------------------------
# Queries -- one line each, so a test can replace them without a corptools.
# These take Alliance Auth primary keys (EveCorporationInfo and EveCharacter),
# not EVE ids.
# --------------------------------------------------------------------------


def _corporation_audit(models, corporation_pk):
    return models["CorporationAudit"].objects.filter(corporation_id=corporation_pk).first()


def _hub_rows(models, audit):
    return list(
        models["SovereigntyHub"]
        .objects.filter(corporation=audit)
        .prefetch_related("reagents", "upgrades")
    )


def _character_audit(models, character_pk):
    return models["CharacterAudit"].objects.filter(character_id=character_pk).first()


def _den_rows(models, audit):
    return list(models["CharacterMercenaryDen"].objects.filter(character=audit))


def _operation_rows(models, audit):
    return list(models["CharacterMercenaryTacticalOperation"].objects.filter(character=audit))


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def _stamp(audit, key):
    """corptools' own time for ``key``, as an aware datetime, or None."""
    raw = (audit.update_timestamps or {}).get(key)
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(value):
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


def _minutes(delta):
    return f"{int(delta.total_seconds() // 60)} minutes"


def _stale_reason(stamp, now, max_age) -> str:
    if stamp is None:
        return "no update recorded"
    age = now - stamp
    if age < -FUTURE_SKEW:
        return "update time is in the future"
    if age > max_age:
        return f"{_minutes(age)} old (limit {_minutes(max_age)})"
    return ""


def _enum(value, choices, label):
    """The Holdfast spelling of an enum corptools stored lower-cased."""
    known = {c.lower(): c for c in choices}
    mapped = known.get(str(value).lower()) if value is not None else None
    if mapped is None:
        raise _Reject(f"unknown {label} {value!r}")
    return mapped


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def _guarded(what, build):
    """Run a reader; a database error means "use ESI", not "fail the sync"."""
    try:
        return build()
    except DatabaseError as exc:
        logger.warning("corptools: could not read %s (%s); using ESI", what, exc)
        return Copies(installed=True, reason=f"could not read {what}")


def hub_copies(owner, now=None) -> Copies:
    """Sovereignty hub details for one corporation, in ESI's shape."""
    models = _models()
    if models is None:
        return Copies()
    now = now or timezone.now()
    return _guarded("sovereignty hubs", lambda: _hub_copies(models, owner, now))


def den_copies(den_character, now=None) -> Copies:
    """A character's mercenary dens, in ESI's shape."""
    models = _models()
    if models is None:
        return Copies()
    now = now or timezone.now()
    return _guarded("mercenary dens", lambda: _den_copies(models, den_character, now))


def operation_copies(den_character, now=None) -> Copies:
    """A character's mercenary tactical operations, in ESI's shape."""
    models = _models()
    if models is None:
        return Copies()
    now = now or timezone.now()
    return _guarded(
        "mercenary tactical operations",
        lambda: _operation_copies(models, den_character, now),
    )


def _hub_copies(models, owner, now) -> Copies:
    audit = _corporation_audit(models, owner.corporation_id)
    if audit is None:
        return Copies(installed=True, reason="not audited by corptools")
    stamp = _stamp(audit, "sovereignty_hubs")
    reason = _stale_reason(stamp, now, HUB_MAX_AGE)
    if not reason and now - stamp < HUB_SETTLE:
        reason = (
            f"hub list finished {_minutes(now - stamp)} ago; "
            "details may still be being written"
        )
    if reason:
        return Copies(installed=True, stamp=stamp, reason=reason)

    copies = Copies(installed=True, stamp=stamp)
    for row in _hub_rows(models, audit):
        try:
            copies.rows[row.hub_id] = _hub_data(row)
        except _Reject as exc:
            copies.rejected[row.hub_id] = str(exc)
    return copies


def _hub_data(row) -> dict:
    reagents = []
    for reagent in row.reagents.all():
        if reagent.type_name_id is None:
            raise _Reject("a reagent has no type")
        reagents.append(
            {
                "type_id": reagent.type_name_id,
                "amount": reagent.amount,
                "burning_per_hour": reagent.burning_per_hour,
            }
        )
    upgrades = []
    for upgrade in row.upgrades.all():
        if upgrade.type_name_id is None:
            raise _Reject("an upgrade has no type")
        upgrades.append(
            {
                "type_id": upgrade.type_name_id,
                "power_state": _enum(upgrade.power_state, PowerState.values, "power state"),
            }
        )
    return {
        "id": row.hub_id,
        "solar_system_id": row.solar_system_id,
        "fuel_access_list_id": row.fuel_access_list_id,
        "resources": {
            "power": {"available": row.power_available, "allocated": row.power_allocated},
            "workforce": {
                "available": row.workforce_available,
                "allocated": row.workforce_allocated,
            },
        },
        "vulnerability_window": {"start": row.vuln_window_start, "end": row.vuln_window_end},
        "reagent_bay": {"last_updated": row.reagent_last_updated, "reagents": reagents},
        "workforce_transport": row.workforce_transport or {},
        "upgrades": upgrades,
    }


def _character_audit_for(models, den_character):
    return _character_audit(models, den_character.character_ownership.character_id)


def _den_copies(models, den_character, now) -> Copies:
    audit = _character_audit_for(models, den_character)
    if audit is None:
        return Copies(installed=True, reason="not audited by corptools")
    stamp = _stamp(audit, "mercenary_dens")
    reason = _stale_reason(stamp, now, DEN_MAX_AGE)
    if reason:
        return Copies(installed=True, stamp=stamp, reason=reason)

    copies = Copies(installed=True, stamp=stamp)
    try:
        for row in _den_rows(models, audit):
            copies.rows[row.den_id] = _den_data(row)
    except _Reject as exc:
        copies.rows = {}
        copies.reason = str(exc)
    return copies


def _den_data(row) -> dict:
    return {
        "id": row.den_id,
        "type_id": row.type_id,
        "planet_id": row.planet_id,
        "state": _enum(row.state, MercenaryDen.State.values, "state"),
        "evolution": {
            "development": {
                "level": _enum(row.development_level, EvolutionLevel.values, "development level"),
                "amount": row.development_amount,
            },
            "anarchy": {
                "level": _enum(row.anarchy_level, EvolutionLevel.values, "anarchy level"),
                "amount": row.anarchy_amount,
            },
        },
        "infomorphs": {"amount": row.infomorph_amount},
        "skyhook": {"id": row.skyhook_id, "corporation_id": row.skyhook_corporation_id},
        "reinforcement_timer": {"end": row.reinforcement_end},
    }


def _operation_copies(models, den_character, now) -> Copies:
    audit = _character_audit_for(models, den_character)
    if audit is None:
        return Copies(installed=True, reason="not audited by corptools")
    stamp = _stamp(audit, "mercenary_tactical_operations")
    reason = _stale_reason(stamp, now, OPERATION_MAX_AGE)
    if reason:
        return Copies(installed=True, stamp=stamp, reason=reason)

    copies = Copies(installed=True, stamp=stamp)
    try:
        for row in _operation_rows(models, audit):
            copies.rows[str(row.operation_id)] = {
                "id": str(row.operation_id),
                "mercenary_den_id": row.mercenary_den_id,
                "dungeon_type_id": row.dungeon_type_id,
                "state": _enum(
                    row.state, MercenaryTacticalOperation.State.values, "state"
                ),
                "expires": row.expires,
            }
    except _Reject as exc:
        copies.rows = {}
        copies.reason = str(exc)
    return copies


def normalise_operation_id(value) -> str:
    """One spelling for an operation id, whichever side wrote it.

    corptools holds a UUID and prints it with hyphens in lower case; ESI's
    value, which Holdfast stored as a string, may differ in case or hyphens.
    Compared as UUIDs they match; anything that is not one falls back to
    lower case.
    """
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return str(value).lower()
