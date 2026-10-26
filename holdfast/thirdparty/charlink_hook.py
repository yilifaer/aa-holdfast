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
from django.contrib import messages
from django.db.models import Exists, OuterRef, Q

from ..app_settings import HOLDFAST_DEN_ESI_SCOPES, HOLDFAST_ESI_SCOPES
from ..core.scope import registration_refusal
from ..models import DenCharacter, Owner
from ..tasks import enqueue
from ..tasks import update_den_character as update_den_character_task
from ..tasks import update_owner as update_owner_task
from ..views.common import den_can_enter


def _ownership(request, token):
    """The requesting user's own link to the token's character.

    By user as well as character, like the manual path: charlink's login flow
    should only ever hand over a character the user owns, and this is where
    that stops being an assumption. Raising is right here -- nothing has been
    written yet, so charlink's "Failed to add character" is true.
    """
    character = EveCharacter.objects.get_character_by_id(token.character_id)
    if character is None:
        character = EveCharacter.objects.create_character(token.character_id)
    try:
        ownership = CharacterOwnership.objects.get(user=request.user, character=character)
    except CharacterOwnership.DoesNotExist:
        messages.error(request, f"{character} is not linked to your account.")
        raise
    return ownership, character


def _added(character: EveCharacter) -> bool:
    """Linked here: a den character, or the token of a registered corporation
    -- which is all a ``manage_owners`` holder without den access gets."""
    return (
        DenCharacter.objects.filter(character_ownership__character=character).exists()
        or Owner.objects.filter(character_ownership__character=character).exists()
    )


# The same answer, for charlink's list of every character. It has to be one
# Exists: charlink's validation asserts the type and drops the whole entry
# from the CharLink page otherwise. Owner's link to the ownership has no
# reverse accessor, hence the subquery for that half.
_ADDED_ANNOTATION = Exists(
    CharacterOwnership.objects.filter(character_id=OuterRef("pk")).filter(
        Q(holdfast_den_character__isnull=False)
        | Q(
            pk__in=Owner.objects.filter(character_ownership__isnull=False).values(
                "character_ownership_id"
            )
        )
    )
)


def _add_all(request, token):
    """One checkbox, both jobs -- decided by what the person is allowed to do.

    Every authorised character of a user with den access is registered as a
    den operator, because den routes are character-scoped and that is the
    common case. One without den access -- a ``manage_owners`` holder only --
    gets no den character: the Tokens page would not let them add one, and
    the den fan-out skips them, so it would have synced once and then sat
    enabled and never synced again. A corporation owner is registered when
    the user holds `manage_owners` and the corporation has none yet; an
    existing owner keeps its token character.
    Whether the character actually has the in-game Station Manager role only
    shows up on the first sync, which ESI answers with a 403 if they do not.

    A corporation outside the tracked alliance is refused the same way the
    Tokens page refuses it -- but not by raising. charlink swallows any
    exception from here into "Failed to add character", which loses the
    reason and, since the den half has by then succeeded, is also untrue. The
    refusal goes out as a warning of its own and the function returns
    normally, so charlink's success line covers the den part that did happen.
    """
    ownership, character = _ownership(request, token)

    # Decided before anything is written: the guard may refresh the
    # corporation from ESI, and its answer does not depend on the den half.
    corporation = refusal = None
    if request.user.has_perm("holdfast.manage_owners"):
        corporation = _corporation_for(character)
        refusal = registration_refusal(corporation, character, request.user)

    if den_can_enter(request.user):
        den_character, _created = DenCharacter.objects.update_or_create(
            character_ownership=ownership, defaults={"is_enabled": True}
        )
        # Saved either way; a broker that is down must not turn that into
        # charlink's "Failed to add character".
        if not enqueue(update_den_character_task, den_character.pk):
            messages.info(request, f"{character}'s dens appear after the next scheduled sync.")

    if corporation is None:
        return
    if refusal is not None:
        messages.warning(request, refusal)
        return

    # Never is_enabled: a re-added token must not switch on an owner a
    # manager disabled, for the reason views/owners gives.
    owner, created = Owner.objects.get_or_create(
        corporation=corporation, defaults={"character_ownership": ownership}
    )
    if not created and owner.character_ownership_id not in (None, ownership.pk):
        # charlink links every character a person has, and a director's alt in
        # the same corporation usually lacks the Station Manager role; taking
        # the token over would 403 the corporation's next sync. Switching is a
        # deliberate act, done on the Tokens page.
        messages.info(
            request,
            f"{corporation} is already registered with "
            f"{owner.character_ownership.character}; that stays its token. To "
            f"switch it to {character}, use Add corporation on the Tokens page "
            f"with {character}.",
        )
        return
    if owner.character_ownership_id is None:
        # Its character was unlinked, so there is no token to replace and the
        # corporation does not sync until somebody supplies one.
        owner.character_ownership = ownership
        owner.save(update_fields=["character_ownership"])
    if not owner.is_enabled:
        messages.info(
            request,
            f"{corporation} is disabled; enable it on the Tokens page when ready.",
        )
    if not enqueue(update_owner_task, owner.pk):
        messages.info(request, f"{corporation}'s first sync runs with the next scheduled sync.")


def _corporation_for(character):
    try:
        return EveCorporationInfo.objects.get(corporation_id=character.corporation_id)
    except EveCorporationInfo.DoesNotExist:
        return EveCorporationInfo.objects.create_corporation(character.corporation_id)


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
                den_can_enter(user) or user.has_perm("holdfast.manage_owners")
            ),
            is_character_added=_added,
            is_character_added_annotation=_ADDED_ANNOTATION,
            get_users_with_perms=lambda: users_with_permissions(
                [
                    "holdfast.basic_access",
                    "holdfast.den_member",
                    "holdfast.den_officer",
                    "holdfast.den_manage",
                    "holdfast.manage_owners",
                ],
                require_all=False,
            ),
            # On by default, so a member who links everything in one go links
            # this too. Most characters will never anchor a den; what keeps
            # that from costing three ESI calls each every twenty minutes is
            # DenCharacter.idle_until, not this flag.
            default_initial_selection=True,
        ),
    ],
)
