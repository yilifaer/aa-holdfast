"""Which alliance this install tracks, and which owners that puts in play.

One alliance per install. Until now "which alliances we track" was read off
whatever corporations happened to be registered, so registering a corporation
from another alliance -- one Station Manager alt is all it took -- silently
made that alliance tracked too: its whole sovereignty map came down, every
low-ADM system of theirs landed on the dashboard, and the ADM check posted one
Discord alert per system. The tracked alliance is a setting now, and every
question of the form "is this owner ours" comes through here.

This module imports only the models and settings, so the views can use it
without dragging the ESI client into every page load.
"""

from allianceauth.eveonline.models import EveAllianceInfo
from allianceauth.services.hooks import get_extension_logger
from django.db.models import Q

from ..app_settings import HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS
from ..models import HoldfastConfig

logger = get_extension_logger(__name__)

NOT_SET_LABEL = "(not set -- the first registered corporation decides)"


# --------------------------------------------------------------------------
# The tracked set
# --------------------------------------------------------------------------


def home_alliance_id():
    """The tracked alliance, or None. Reads without creating the config row,
    so an install that has never opened a settings page stays that way."""
    return (
        HoldfastConfig.objects.filter(pk=1)
        .values_list("home_alliance_id", flat=True)
        .first()
    )


def alliance_ids_from(values) -> set:
    """The alliance ids in a local.py list, as ints.

    An entry that is not a number -- a ticker, a None, a typo -- is skipped
    with a warning rather than raised on. Before 0.1.9 the list was used
    uncast, so such an entry was harmless; raising here would instead take
    every page and task down with it. The migration that bootstraps the
    tracked alliance carries a copy of this rule.
    """
    ids = set()
    for value in values:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring %r in HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS: not an alliance id",
                value,
            )
    return ids


def extra_alliance_ids() -> set:
    return alliance_ids_from(HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS)


def tracked_alliance_ids() -> set:
    """Alliances we keep sovereignty rows for: home plus the extras from
    local.py. Never derived from the registered owners."""
    ids = extra_alliance_ids()
    home = home_alliance_id()
    if home is not None:
        ids.add(home)
    return ids


def alliance_name(alliance_id):
    """The alliance's name when Auth knows it, else the id as text, else None.

    The id is stored rather than a foreign key precisely so nothing here
    depends on an EveAllianceInfo row existing.
    """
    if alliance_id is None:
        return None
    name = (
        EveAllianceInfo.objects.filter(alliance_id=alliance_id)
        .values_list("alliance_name", flat=True)
        .first()
    )
    return name or str(alliance_id)


def home_alliance_name():
    return alliance_name(home_alliance_id())


def _alliance_label(alliance_id) -> str:
    """Name and id together, for messages a manager will act on."""
    name = alliance_name(alliance_id)
    if name == str(alliance_id):
        return f"alliance {alliance_id}"
    return f"{name} ({alliance_id})"


# --------------------------------------------------------------------------
# Owner scope
# --------------------------------------------------------------------------


def owner_scope_q(tracked=None) -> Q:
    """Owners in scope: corporation in a tracked alliance, or in no alliance.

    An alliance-less holding corporation is legitimate and adds no tracked
    alliance of its own, so it is in scope on any install. Scope reads the
    corporation's alliance foreign key and nothing else -- one ORM filter,
    one source -- which Auth keeps current through its own corporation refresh.
    """
    if tracked is None:
        tracked = tracked_alliance_ids()
    return Q(corporation__alliance__isnull=True) | Q(
        corporation__alliance__alliance_id__in=tracked
    )


def owner_in_scope(owner, tracked=None) -> bool:
    if tracked is None:
        tracked = tracked_alliance_ids()
    alliance = owner.corporation.alliance
    return alliance is None or alliance.alliance_id in tracked


def inactive_reason(owner, tracked=None):
    """Why this owner is not being synced, or None when it is active.

    The wording is what lands in ``last_error`` and on the Tokens page, so it
    says what to do about it.
    """
    if not owner.is_enabled:
        return "Disabled on the Tokens page."
    return scope_reason(owner, tracked)


def scope_reason(owner, tracked=None):
    """Why this owner's corporation is outside the tracked set, or None.

    Separate from ``inactive_reason`` because the Tokens page renders it
    itself: the sync fan-out skips an inactive owner, so nothing ever writes
    the reason to ``last_error`` for an owner that fell out of scope after a
    settings change, and its row would otherwise keep showing the green
    result of its last sync.
    """
    if tracked is None:
        tracked = tracked_alliance_ids()
    if owner_in_scope(owner, tracked):
        return None
    home = home_alliance_id()
    if home is None:
        return "No tracked alliance is set; set one on the SOV settings page."
    alliance_id = owner.corporation.alliance.alliance_id
    return (
        f"Corporation is in {_alliance_label(alliance_id)}, outside the tracked "
        f"alliance {home_alliance_name()}; not synced. Change the tracked "
        f"alliance on the SOV settings page or add {alliance_id} to "
        "HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS."
    )


# --------------------------------------------------------------------------
# Choosing the tracked alliance
# --------------------------------------------------------------------------


def bootstrap_home_alliance(owner_model, config_model, extras=()):
    """Set the tracked alliance from what is already registered.

    Takes the Owner and HoldfastConfig classes as arguments so the migration
    can hand it historical models and the settings page the live ones. Does
    nothing when a tracked alliance is already set. Returns the alliance id it
    chose, or None when it left things alone.

    The alliance most enabled owners are in wins; a tie goes to the one
    registered first, by primary key rather than the model's alphabetical
    ordering. Majority, not earliest, because the install this was written
    for had one foreign corporation among eight of its own, and nobody knew
    which had been registered first -- picking wrong would have stopped all
    eight. The corporation's alliance is read from its foreign key; when Auth
    has not filled that in yet, the token character's alliance stands in. An
    alliance already listed in the extras is skipped: making it home would
    then refuse the real one.
    """
    current = (
        config_model.objects.filter(pk=1)
        .values_list("home_alliance_id", flat=True)
        .first()
    )
    if current is not None:
        return None

    extras = alliance_ids_from(extras)
    owners = (
        owner_model.objects.filter(is_enabled=True)
        .order_by("pk")
        .select_related("corporation__alliance", "character_ownership__character")
    )
    chosen = _majority_alliance(owners, extras)
    if chosen is None:
        return None
    config, _created = config_model.objects.get_or_create(pk=1)
    config.home_alliance_id = chosen
    config.save(update_fields=["home_alliance_id"])
    return chosen


def _majority_alliance(owners, extras):
    """The alliance most of these owners are in; ties go to the earliest.

    Shared with nothing else on purpose -- the migration carries its own copy
    so that it never changes under an install that has already run it.
    """
    votes = {}  # alliance id -> [owners in it, pk of the first of them]
    for owner in owners:
        alliance = owner.corporation.alliance
        candidate = alliance.alliance_id if alliance is not None else None
        if candidate is None and owner.character_ownership is not None:
            candidate = owner.character_ownership.character.alliance_id
        if candidate is None or candidate in extras:
            continue
        votes.setdefault(candidate, [0, owner.pk])[0] += 1
    if not votes:
        return None
    return max(votes, key=lambda alliance_id: (votes[alliance_id][0], -votes[alliance_id][1]))


def _adopt_home_alliance(*candidates) -> None:
    """First registration decides: the first candidate that is a real alliance
    and not already an extra becomes the tracked alliance.

    Callers pass the corporation's alliance first and the registrant's own
    last. The registrant matters for an alliance-less holding corporation
    registered first: with nothing adopted, the tracked set stays empty, the
    person who just registered it sees none of its structures, and nothing
    says why. A home member's own alliance is the obvious answer.
    """
    extras = extra_alliance_ids()
    for candidate in candidates:
        if candidate and candidate not in extras:
            config = HoldfastConfig.get_solo()
            config.home_alliance_id = candidate
            config.save(update_fields=["home_alliance_id"])
            return


# --------------------------------------------------------------------------
# Registration guard
# --------------------------------------------------------------------------


def registration_refusal(corporation, character, user):
    """Why this corporation may not be registered, or None when it may.

    Both token registration paths -- the Tokens page and charlink -- call this
    before creating an Owner row. Django admin does not: admins can do anything,
    and the admin form is not a registration path.

    Scope reads ``corporation.alliance``, which Auth sets when it first
    creates the corporation row and refreshes on its own schedule -- every six
    hours on a stock install. So the corporation is refreshed from ESI first,
    on every registration: one etag-aware public call, and the row scope will
    read is then current. Agreement between the row and the character's
    ``alliance_id`` proves nothing -- a row made before the corporation joined
    an alliance and a character row that lags say None and None together, and
    that once admitted a corporation in a foreign alliance as alliance-less.

    After a successful refresh the row is the only source: a character row
    that still says the old alliance is stale, and refusing on it would lock
    a legitimate corporation out until Auth's next character update. Only
    when the refresh fails does the character's alliance still count, since
    the row may then be the stale one and either can name the real alliance.

    When no tracked alliance is set yet the registration is allowed and, as a
    side effect, decides it: the corporation's alliance becomes home, or the
    registrant's own when the corporation has none.
    """
    refreshed = _refresh_corporation(corporation)
    fk_alliance = _corporation_alliance_id(corporation)
    character_alliance = character.alliance_id or None
    if refreshed:
        sources = (fk_alliance,)
    else:
        sources = (fk_alliance, character_alliance)
    candidates = {value for value in sources if value}

    main = getattr(user.profile, "main_character", None)
    main_alliance = main.alliance_id if main else None

    home = home_alliance_id()
    if home is None:
        _adopt_home_alliance(*sources, main_alliance)
        return None

    tracked = tracked_alliance_ids()
    foreign = candidates - tracked
    if foreign:
        alliance_id = fk_alliance if fk_alliance in foreign else next(iter(foreign))
        return (
            f"{corporation.corporation_name} is in {_alliance_label(alliance_id)}, "
            f"outside the tracked alliance {home_alliance_name()}. Change the "
            "tracked alliance on the SOV settings page, or add "
            f"{alliance_id} to HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS in local.py."
        )

    if not candidates:
        # A home member may register an alliance-less holding corporation; an
        # outsider may not.
        if main_alliance not in tracked:
            where = (
                f"add {main_alliance} to" if main_alliance else "add your alliance to"
            )
            return (
                f"{corporation.corporation_name} has no alliance, and your main "
                f"character is not in the tracked alliance {home_alliance_name()}. "
                "Only a member of the tracked alliance may register a corporation "
                "with no alliance; change the tracked alliance on the SOV settings "
                f"page, or {where} HOLDFAST_TRACK_EXTRA_ALLIANCE_IDS in local.py."
            )
    return None


def _refresh_corporation(corporation) -> bool:
    """Bring the corporation row up to date from ESI. False when that failed,
    in which case the row is whatever Auth last saw and the guard says so."""
    try:
        corporation.update_corporation()
    except Exception:  # noqa: BLE001 - the guard still runs on what we have
        logger.warning(
            "Could not refresh %s before registering it", corporation, exc_info=True
        )
        return False
    return True


def _corporation_alliance_id(corporation):
    alliance = corporation.alliance
    return alliance.alliance_id if alliance is not None else None
