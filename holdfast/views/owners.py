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
from django.shortcuts import redirect, render
from esi.decorators import token_required

from ..app_settings import HOLDFAST_DEN_ESI_SCOPES, HOLDFAST_ESI_SCOPES
from ..models import DenCharacter, Owner
from ..tasks import update_den_character as update_den_character_task
from ..tasks import update_owner as update_owner_task
from .common import require_any

logger = logging.getLogger(__name__)


@require_any("manage_owners")
def index(request):
    return render(
        request,
        "holdfast/owners.html",
        {
            "owners": Owner.objects.select_related("corporation", "character_ownership"),
            "den_characters": DenCharacter.objects.select_related(
                "character_ownership__character"
            ),
        },
    )


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

    owner, created = Owner.objects.update_or_create(
        corporation=corporation,
        defaults={"character_ownership": ownership, "is_enabled": True},
    )
    update_owner_task.delay(owner.pk)
    messages.success(
        request,
        f"{'Registered' if created else 'Updated'} {corporation} using {character}. "
        "The first sync is running now.",
    )
    return redirect("holdfast:owners")


@require_any("den_claim", "den_basic", "den_member", "den_officer", "den_manage")
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
