"""Access rules and the querysets every section shares.

Permissions come in two independent flavours, and it is worth being explicit
about which is which:

* **Tier** -- how much of a section you may open. ``*_basic`` is the
  member-facing page, ``*_officer`` is every page in that section, and
  ``*_manage`` adds the settings form. Higher tiers imply lower ones, so a
  manager never needs the officer permission granted alongside.
* **Scope** -- whose data you see. An officer sees their own alliance's
  registered corporations. That matters for an install shared by several
  alliances, which is the normal case once this is public rather than running
  for one group.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from ..models import DenSlot, Owner, Skyhook, SovHub


# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------


def _any_perm(user, *codenames) -> bool:
    return any(user.has_perm(f"holdfast.{name}") for name in codenames)


def sov_can_view_all(user) -> bool:
    return _any_perm(user, "sov_officer", "sov_manage")


def sov_can_manage(user) -> bool:
    return _any_perm(user, "sov_manage")


def sov_can_enter(user) -> bool:
    return _any_perm(user, "sov_basic", "sov_officer", "sov_manage")


def skyhook_can_view_all(user) -> bool:
    return _any_perm(user, "skyhook_officer", "skyhook_manage")


def skyhook_can_manage(user) -> bool:
    return _any_perm(user, "skyhook_manage")


def skyhook_can_enter(user) -> bool:
    return _any_perm(user, "skyhook_basic", "skyhook_officer", "skyhook_manage")


def den_can_view_all(user) -> bool:
    return _any_perm(user, "den_officer", "den_manage")


def den_can_manage(user) -> bool:
    return _any_perm(user, "den_manage")


def den_can_see_list(user) -> bool:
    return _any_perm(user, "den_member", "den_officer", "den_manage")


def den_can_enter(user) -> bool:
    return _any_perm(user, "den_basic", "den_member", "den_officer", "den_manage")


def require_any(*codenames):
    """Like permission_required, but any one of the listed permissions will do.

    Django's own decorator ANDs its arguments, which is the wrong shape for
    tiers where holding the higher permission should be enough on its own.
    """

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not _any_perm(request.user, *codenames):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def visible_owners(user):
    """Registered corporations whose data this user may see.

    Scoped to the user's own alliance rather than everything on the server, so
    an install serving more than one alliance keeps them apart by default.
    """
    if user.is_superuser:
        return Owner.objects.all()

    character = getattr(user.profile, "main_character", None)
    if not character:
        return Owner.objects.none()

    if character.alliance_id:
        return Owner.objects.filter(
            corporation__alliance__alliance_id=character.alliance_id
        )
    return Owner.objects.filter(corporation__corporation_id=character.corporation_id)


def visible_alliance_ids(user):
    if user.is_superuser:
        return set(
            Owner.objects.exclude(corporation__alliance__isnull=True).values_list(
                "corporation__alliance__alliance_id", flat=True
            )
        )
    character = getattr(user.profile, "main_character", None)
    if character and character.alliance_id:
        return {character.alliance_id}
    return set()


def visible_hubs(user):
    return (
        SovHub.objects.filter(owner__in=visible_owners(user))
        .select_related("owner__corporation", "eve_solar_system")
        .prefetch_related("reagents__eve_type", "upgrades__eve_type")
    )


def visible_skyhooks(user):
    return (
        Skyhook.objects.filter(owner__in=visible_owners(user))
        .select_related("owner__corporation", "eve_planet", "eve_solar_system")
        .prefetch_related("reagents__eve_type")
    )


def stealable_skyhooks(user):
    """Only the ones that actually hold something a raider can take.

    Workforce and power flow straight to the sovereignty hub -- there is no
    physical stock, and ESI gives those skyhooks no theft window at all. On a
    real 413-skyhook alliance that is 51 rows instead of 413.
    """
    return visible_skyhooks(user).filter(reagents__isnull=False).distinct()


def visible_slots(user):
    return (
        DenSlot.objects.filter(skyhook__owner__in=visible_owners(user))
        .select_related(
            "skyhook__eve_planet",
            "skyhook__eve_solar_system",
            "skyhook__owner__corporation",
        )
        .prefetch_related("dens__eve_character", "claims__character")
    )


def own_den_characters(user):
    from ..models import DenCharacter

    return DenCharacter.objects.filter(
        character_ownership__user=user
    ).select_related("character_ownership__character")


def own_dens(user):
    from ..models import MercenaryDen

    return MercenaryDen.objects.filter(
        den_character__character_ownership__user=user
    ).select_related("eve_planet", "eve_solar_system", "den_character", "slot")


# --------------------------------------------------------------------------
# Discord routing, shared by the three settings pages
# --------------------------------------------------------------------------


def routes_for_section(section):
    """Routing rows for one section, created on demand so the form is complete.

    Every category gets a row whether or not anyone has configured it, because
    a settings page that only lists what somebody already touched is useless
    the first time you open it.
    """
    from ..models import CATEGORY_SECTIONS, AlertCategory, AlertRoute

    rows = []
    for category in AlertCategory:
        if CATEGORY_SECTIONS.get(category) != section:
            continue
        route = AlertRoute.for_category(category)
        rows.append(route)
    return rows


def save_routes(request, section):
    """Apply the routing part of a settings form.

    A category with nothing ticked falls back to every enabled webhook, which
    is spelled out on the form -- silence is the failure nobody notices.
    """
    from ..models import Webhook

    webhook_ids = {str(w.pk) for w in Webhook.objects.all()}
    for route in routes_for_section(section):
        route.is_enabled = request.POST.get(f"route_enabled_{route.category}") == "on"
        route.save(update_fields=["is_enabled"])
        chosen = [
            value
            for value in request.POST.getlist(f"route_webhooks_{route.category}")
            if value in webhook_ids
        ]
        route.webhooks.set(chosen)
