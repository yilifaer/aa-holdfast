"""Access rules and the querysets every section shares.

Permissions come in two independent flavours, and it is worth being explicit
about which is which:

* **Tier** -- how much of a section you may open. ``basic_access`` is the
  member-facing page of every section, ``*_officer`` is every page in that
  section, and ``*_manage`` adds the settings form. Higher tiers imply lower
  ones, so a manager never needs the officer permission granted alongside.
* **Scope** -- whose data you see. An officer sees their own alliance's
  registered corporations. That matters for an install shared by several
  alliances, which is the normal case once this is public rather than running
  for one group.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from ..core.scope import tracked_alliance_ids
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
    return _any_perm(user, "basic_access", "sov_officer", "sov_manage")


def skyhook_can_view_all(user) -> bool:
    return _any_perm(user, "skyhook_officer", "skyhook_manage")


def skyhook_can_manage(user) -> bool:
    return _any_perm(user, "skyhook_manage")


def skyhook_can_enter(user) -> bool:
    return _any_perm(user, "basic_access", "skyhook_officer", "skyhook_manage")


def den_can_view_all(user) -> bool:
    return _any_perm(user, "den_officer", "den_manage")


def den_can_manage(user) -> bool:
    return _any_perm(user, "den_manage")


def den_can_see_list(user) -> bool:
    return _any_perm(user, "den_member", "den_officer", "den_manage")


def den_can_claim(user) -> bool:
    """Applying for a slot is part of the user rung, not a permission of its
    own: whoever can see the den list, where the button is, may use it."""
    return den_can_see_list(user)


DEN_ENTER = ("basic_access", "den_member", "den_officer", "den_manage")


def den_can_enter(user) -> bool:
    return _any_perm(user, *DEN_ENTER)


def den_entrant_ids(user_ids) -> set:
    """Which of these users pass ``den_can_enter``, in one query.

    For lists of people -- the operator directory, the den fan-out -- where
    asking ``has_perm`` of each costs three queries a person. It answers the
    way Auth's StateBackend does: an active user holding one of the
    permissions directly, through a group or through their state, or a
    superuser. A test holds it to ``den_can_enter``.
    """
    from django.contrib.auth.models import Permission, User

    # Keys, not a queryset: Auth's State relates to its own proxy of
    # Permission.
    permissions = list(
        Permission.objects.filter(
            content_type__app_label="holdfast", codename__in=DEN_ENTER
        ).values_list("pk", flat=True)
    )
    return set(
        User.objects.filter(pk__in=list(user_ids), is_active=True)
        .filter(
            Q(is_superuser=True)
            | Q(user_permissions__in=permissions)
            | Q(groups__permissions__in=permissions)
            | Q(profile__state__permissions__in=permissions)
        )
        .values_list("pk", flat=True)
        .distinct()
    )


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

    Only active owners -- enabled and inside the tracked alliance -- are ever
    visible, so a disabled owner's structures leave every page and badge
    together (see ``OwnerQuerySet``). Within that, a member of a tracked
    alliance sees their own alliance's corporations and the alliance-less
    holding corporations, which belong to nobody else; a main with no alliance
    sees only its own corporation.
    """
    active = Owner.objects.active()
    if user.is_superuser:
        return active

    character = getattr(user.profile, "main_character", None)
    if not character:
        return Owner.objects.none()

    if character.alliance_id:
        if character.alliance_id not in tracked_alliance_ids():
            return Owner.objects.none()
        return active.filter(
            Q(corporation__alliance__alliance_id=character.alliance_id)
            | Q(corporation__alliance__isnull=True)
        )
    return active.filter(corporation__corporation_id=character.corporation_id)


def visible_alliance_ids(user):
    """Alliances whose sovereignty rows this user may see: the tracked set,
    narrowed to the user's own alliance unless they are a superuser."""
    tracked = tracked_alliance_ids()
    if user.is_superuser:
        return tracked
    character = getattr(user.profile, "main_character", None)
    if character and character.alliance_id:
        return {character.alliance_id} & tracked
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
    from ..core.alerts import route_channels
    from ..models import CATEGORY_SECTIONS, AlertCategory, AlertRoute

    rows = []
    for category in AlertCategory:
        if CATEGORY_SECTIONS.get(category) != section:
            continue
        route = AlertRoute.for_category(category)
        # For the form: routed, and nothing left that works.
        route.is_stranded = route.is_enabled and route_channels(route) == []
        rows.append(route)
    return rows


def report_test_results(request, results) -> None:
    """Say what a section's Send test alerts did, category by category.

    "No channel, or switched off" covered everything that did not arrive,
    including a channel Discord refused or had just been switched off for --
    which sent people to the routing table when the webhook was the problem.
    """
    from django.contrib import messages

    delivered = [name for name, delivery in results.items() if delivery.sent]
    failed = [
        f"{name} ({'; '.join(delivery.failures)})"
        for name, delivery in results.items()
        if delivery.failures
    ]
    if delivered:
        messages.success(
            request, f"Sent {len(delivered)} test alert(s): {', '.join(delivered)}"
        )
    if failed:
        messages.warning(request, f"Not sent: {', '.join(failed)}")


# Rendered only beside the routing table, which the settings pages leave out
# while no webhook exists. A form without it carried no routing at all.
ROUTING_SHOWN = "routing_shown"


def save_routes(request, section):
    """Apply the routing part of a settings form.

    A category nobody has routed goes to every enabled webhook -- silence is
    the failure nobody notices. Ticking a channel routes it; unticking every
    channel it had, or choosing "every enabled webhook", unroutes it. What
    does not unroute it is a row that shows nothing ticked because its
    webhooks were deleted: saving the page to change a fuel band must not
    quietly send leadership alerts to every channel.

    Nor does a form that never showed the routing table change anything. The
    page leaves the table out while no webhook exists, so a fresh install
    saving its tracked alliance posted no switches -- and every category of
    the section was switched off before its first webhook was ever added.
    """
    from ..models import Webhook

    if ROUTING_SHOWN not in request.POST:
        return
    webhook_ids = {str(w.pk) for w in Webhook.objects.all()}
    for route in routes_for_section(section):
        had_ticks = route.webhooks.exists()
        chosen = [
            value
            for value in request.POST.getlist(f"route_webhooks_{route.category}")
            if value in webhook_ids
        ]
        route.is_enabled = request.POST.get(f"route_enabled_{route.category}") == "on"
        if chosen:
            route.is_routed = True
        elif had_ticks or request.POST.get(f"route_all_{route.category}") == "on":
            route.is_routed = False
        route.save(update_fields=["is_enabled", "is_routed"])
        route.webhooks.set(chosen)
