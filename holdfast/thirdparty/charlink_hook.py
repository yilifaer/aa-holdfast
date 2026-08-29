"""charlink integration.

charlink gives members one page where they authorise every app at once. This
module is only ever imported by charlink itself, so an install without it
carries no cost and no import error -- the manual registration page in
``views/owners.py`` remains the supported path either way.

One entry, not two. The corporation scope and the den scopes cover
different things -- a corporation's hubs and skyhooks versus one character's
own mercenary dens -- but splitting them on the charlink page asked members a
question they cannot answer, namely whether they hold an in-game corporation
role. What gets registered is decided here instead, from the permissions the
user already has.
"""

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from charlink.app_imports.utils import AppImport, LoginImport
from charlink.utils import users_with_permissions
from django.contrib.auth.models import Permission
from django.db.models import Exists, OuterRef

from ..app_settings import HOLDFAST_DEN_ESI_SCOPES, HOLDFAST_ESI_SCOPES
from ..models import DenCharacter, Owner
from ..tasks import update_den_character as update_den_character_task
from ..tasks import update_owner as update_owner_task


def _permission(codename):
    return Permission.objects.get(
        content_type__app_label="holdfast", codename=codename
    )


def _ownership(token):
    character = EveCharacter.objects.get_character_by_id(token.character_id)
    if character is None:
        character = EveCharacter.objects.create_character(token.character_id)
    return CharacterOwnership.objects.get(character=character), character


def _add_corporation(request, token):
    ownership, character = _ownership(token)
    try:
        corporation = EveCorporationInfo.objects.get(
            corporation_id=character.corporation_id
        )
    except EveCorporationInfo.DoesNotExist:
        corporation = EveCorporationInfo.objects.create_corporation(
            character.corporation_id
        )
    owner, _created = Owner.objects.update_or_create(
        corporation=corporation,
        defaults={"character_ownership": ownership, "is_enabled": True},
    )
    update_owner_task.delay(owner.pk)


def _add_den_character(request, token):
    ownership, _character = _ownership(token)
    den_character, _created = DenCharacter.objects.update_or_create(
        character_ownership=ownership, defaults={"is_enabled": True}
    )
    update_den_character_task.delay(den_character.pk)


def _corporation_added(character: EveCharacter) -> bool:
    return Owner.objects.filter(
        character_ownership__character=character
    ).exists()


def _den_added(character: EveCharacter) -> bool:
    return DenCharacter.objects.filter(
        character_ownership__character=character
    ).exists()


def _add_all(request, token):
    """One checkbox, both jobs -- decided by what the person is allowed to do.

    Every authorised character is registered as a den operator, because den
    routes are character-scoped and that is the common case. A corporation
    owner is registered as well when the user holds `manage_owners`; whether
    the character actually has the in-game Station Manager role only shows up
    on the first sync, which ESI answers with a 403 if they do not.
    """
    ownership, character = _ownership(token)

    den_character, _created = DenCharacter.objects.update_or_create(
        character_ownership=ownership, defaults={"is_enabled": True}
    )
    update_den_character_task.delay(den_character.pk)

    if not request.user.has_perm("holdfast.manage_owners"):
        return

    try:
        corporation = EveCorporationInfo.objects.get(
            corporation_id=character.corporation_id
        )
    except EveCorporationInfo.DoesNotExist:
        corporation = EveCorporationInfo.objects.create_corporation(
            character.corporation_id
        )
    owner, _created = Owner.objects.update_or_create(
        corporation=corporation,
        defaults={"character_ownership": ownership, "is_enabled": True},
    )
    update_owner_task.delay(owner.pk)


app_import = AppImport(
    "holdfast",
    [
        LoginImport(
            app_label="holdfast",
            unique_id="tools",
            field_label="SOV / Skyhook / Den tools",
            add_character=_add_all,
            # Both sets at once. Splitting them into two checkboxes made the
            # page ask a question most members cannot answer -- whether they
            # hold a corporation role -- and adding a scope later would mean
            # everyone re-authorising anyway.
            scopes=HOLDFAST_ESI_SCOPES + HOLDFAST_DEN_ESI_SCOPES,
            check_permissions=lambda user: (
                user.has_perm("holdfast.den_claim")
                or user.has_perm("holdfast.den_basic")
                or user.has_perm("holdfast.manage_owners")
            ),
            is_character_added=_den_added,
            is_character_added_annotation=Exists(
                DenCharacter.objects.filter(
                    character_ownership__character_id=OuterRef("pk")
                )
            ),
            get_users_with_perms=lambda: users_with_permissions(
                _permission("den_claim")
            ),
            # Off by default: this is not a scope every alt needs, and ticking
            # it for everyone would burn ESI calls on characters that will
            # never anchor anything.
            default_initial_selection=False,
        ),
    ],
)
