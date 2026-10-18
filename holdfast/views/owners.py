"""Registering the tokens the app runs on.

Two kinds, and they are not interchangeable:

* A **corporation** token, from a character holding the in-game Station
  Manager role, covers that corporation's sovereignty hubs and skyhooks.
* A **character** token covers that character's own mercenary dens. There is no
  corporation equivalent, so every den operator registers separately.

Both are also reachable through charlink, for installs that use it. This page
stays regardless: charlink is optional, and an alliance should never be forced
to install a second app to use this one.
"""

import logging

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from esi.decorators import token_required

from ..app_settings import HOLDFAST_DEN_ESI_SCOPES, HOLDFAST_ESI_SCOPES
from ..core import attention
from ..core.scope import (
    NOT_SET_LABEL,
    alliance_name,
    extra_alliance_ids,
    home_alliance_name,
    registration_refusal,
    scope_reason,
    tracked_alliance_ids,
)
from ..models import DenCharacter, Owner
from ..tasks import update_den_character as update_den_character_task
from ..tasks import update_owner as update_owner_task
from .common import require_any, sov_can_manage

logger = logging.getLogger(__name__)


@require_any("manage_owners")
def index(request):
    tracked = tracked_alliance_ids()
    owners = list(
        Owner.objects.select_related(
            "corporation__alliance", "character_ownership__character"
        )
    )
    for owner in owners:
        # Disabled has its own column. This is for the owner that looks fine
        # and still never syncs: its corporation is outside the tracked set.
        # Computed here rather than read from last_error, because the sync
        # fan-out skips such an owner and so never writes it there.
        owner.scope_reason = scope_reason(owner, tracked)
        owner.in_scope = owner.scope_reason is None
    return render(
        request,
        "holdfast/owners.html",
        {
            "owners": owners,
            "den_characters": DenCharacter.objects.select_related(
                "character_ownership__character"
            ),
            "home_alliance_name": home_alliance_name(),
            "not_set_label": NOT_SET_LABEL,
            "extra_alliances": [
                alliance_name(alliance_id) for alliance_id in sorted(extra_alliance_ids())
            ],
            "can_manage_sov": sov_can_manage(request.user),
        },
    )


@require_any("manage_owners")
@require_POST
def toggle_owner(request, pk):
    """Switch an owner off or on without deleting anything.

    The Tokens page is install-wide by design -- manage_owners is not
    alliance-scoped and the page lists every owner -- so the scoped queryset
    here is all owners. Re-enabling one outside the tracked alliance is
    allowed: the usual reason is that the manager is about to change the
    tracked alliance, and refusing would dictate the click order. The badge
    and last_error already say it will not sync until then.
    """
    owner = get_object_or_404(Owner.objects.all(), pk=pk)
    owner.is_enabled = not owner.is_enabled
    owner.save(update_fields=["is_enabled"])
    # The sidebar badges are cached per user for a minute; the person who
    # clicked should not have to wait it out.
    attention.invalidate(request.user)
    messages.success(
        request,
        f"{owner} is now {'enabled' if owner.is_enabled else 'disabled'}."
        + ("" if owner.is_enabled else " Its structures stay, but are hidden."),
    )
    return redirect("holdfast:owners")


@require_any("manage_owners")
@token_required(scopes=HOLDFAST_ESI_SCOPES)
def add_owner(request, token):
    """Register a corporation. Needs Station Manager in game, which we cannot
    check here -- the first sync surfaces a 403 on the owners page instead."""
    character = _character_for(token)
    ownership = _ownership_for(request, character)
    if ownership is None:
        return redirect("holdfast:owners")

    try:
        corporation = EveCorporationInfo.objects.get(
            corporation_id=character.corporation_id
        )
    except EveCorporationInfo.DoesNotExist:
        corporation = EveCorporationInfo.objects.create_corporation(
            character.corporation_id
        )

    # Outside the tracked alliance means no Owner row at all -- an Owner is
    # what made a whole foreign sovereignty map appear on the dashboard.
    refusal = registration_refusal(corporation, character, request.user)
    if refusal is not None:
        messages.error(request, refusal)
        return redirect("holdfast:owners")

    # is_enabled is deliberately not in the defaults: re-adding a token, the
    # normal thing to do when one expires, must not switch on an owner that a
    # manager disabled on purpose.
    owner, created = Owner.objects.update_or_create(
        corporation=corporation,
        defaults={"character_ownership": ownership},
    )
    update_owner_task.delay(owner.pk)
    messages.success(
        request,
        f"{'Registered' if created else 'Updated'} {corporation} using {character}. "
        + (
            "The first sync is running now."
            if owner.is_enabled
            else "It is disabled; enable it on this page when ready."
        ),
    )
    return redirect("holdfast:owners")


@require_any("basic_access", "den_member", "den_officer", "den_manage")
@token_required(scopes=HOLDFAST_DEN_ESI_SCOPES)
def add_den_character(request, token):
    """Register a character's own dens without going through a slot claim."""
    character = _character_for(token)
    ownership = _ownership_for(request, character)
    if ownership is None:
        return redirect("holdfast:den_information")

    den_character, created = DenCharacter.objects.update_or_create(
        character_ownership=ownership, defaults={"is_enabled": True}
    )
    update_den_character_task.delay(den_character.pk)
    messages.success(
        request,
        f"{'Registered' if created else 'Refreshed'} {character}. "
        "Their dens will appear shortly.",
    )
    return redirect("holdfast:den_information")


def _character_for(token):
    try:
        return EveCharacter.objects.get(character_id=token.character_id)
    except EveCharacter.DoesNotExist:
        return EveCharacter.objects.create_character(token.character_id)


def _ownership_for(request, character):
    try:
        return CharacterOwnership.objects.get(user=request.user, character=character)
    except CharacterOwnership.DoesNotExist:
        messages.error(
            request,
            f"{character} is not linked to your account. Add it on your character "
            "page first, then register it here.",
        )
        return None
