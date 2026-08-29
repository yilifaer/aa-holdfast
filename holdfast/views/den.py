"""Mercenary den section.

Four tiers here rather than three, because a den operator is an ordinary
member who needs to see their own den and nothing else. The list page is
deliberately anonymous: it answers "is this ground taken" without telling
every member who is farming where.
"""

from datetime import timedelta

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from esi.decorators import token_required

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

from ..app_settings import HOLDFAST_DEN_ESI_SCOPES
from ..core.notifications import invalidate_badges, notify_user
from ..models import (
    TIMEZONE_CHOICES,
    DenCharacter,
    DenClaim,
    DenEvent,
    DenOperator,
    DenSlot,
    MercenaryDen,
    MercenaryTacticalOperation,
    Skyhook,
    HoldfastConfig,
)
from ..tasks import update_den_character as update_den_character_task
from ..models import Webhook
from .common import (
    routes_for_section,
    save_routes,
    den_can_manage,
    den_can_see_list,
    den_can_view_all,
    own_den_characters,
    own_dens,
    require_any,
    visible_slots,
)

DEN_ANY = ("den_basic", "den_member", "den_officer", "den_manage")
DEN_LIST = ("den_member", "den_officer", "den_manage")
DEN_FULL = ("den_officer", "den_manage")
DEN_ADMIN = ("den_manage",)


def _context(request, **extra):
    base = {
        "config": HoldfastConfig.get_solo(),
        "can_manage": den_can_manage(request.user),
        "can_view_all": den_can_view_all(request.user),
        "can_see_list": den_can_see_list(request.user),
        "can_claim": request.user.has_perm("holdfast.den_claim"),
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@require_any(*DEN_LIST)
def dashboard(request):
    """Yours by default; an officer additionally sees the alliance picture."""
    now = timezone.now()
    mine = list(own_dens(request.user))
    items = []

    for den in mine:
        if den.state == MercenaryDen.State.PAUSED:
            items.append(
                {
                    "severity": "critical",
                    "when": den.reinforce_end,
                    "kind": "Your den is reinforced",
                    "where": den.planet_name,
                    "system": den.system_name,
                    "detail": "out of reinforcement",
                }
            )
        elif den.state == MercenaryDen.State.DISABLED:
            items.append(
                {
                    "severity": "warning",
                    "when": None,
                    "kind": "Your den is disabled",
                    "where": den.planet_name,
                    "system": den.system_name,
                    "detail": "the character no longer has the skill for it",
                }
            )

    # A den at anarchy 2 or above starts taking workforce from the skyhook it
    # sits on, and on our own ground that lands on our own sovereignty. The
    # operator is the one person who can do something about it, so tell them
    # here rather than only on the officer board.
    for den in mine:
        if not den.is_siphoning:
            continue
        skyhook = den.slot.skyhook if den.slot else None
        percent, taken, _certainty = (
            skyhook.siphon_estimate if skyhook else (None, None, None)
        )
        items.append(
            {
                "severity": "warning",
                "when": None,
                "kind": "Your den is taking our own workforce",
                "where": den.planet_name,
                "system": den.system_name,
                "detail": (
                    f"anarchy {den.anarchy_number} -- "
                    + (
                        f"{percent:.0f}% of this skyhook, {taken:,} a cycle"
                        if percent and taken
                        else "the skyhook under it is losing output"
                    )
                ),
            }
        )

    for event in DenEvent.objects.filter(
        den_character__in=own_den_characters(request.user),
        kind=DenEvent.Kind.ATTACKED,
        timestamp__gte=now - timedelta(hours=24),
    ).select_related("den__eve_planet", "den__eve_solar_system"):
        items.append(
            {
                "severity": "critical",
                "when": event.timestamp,
                "kind": "Your den was attacked",
                "where": event.den.planet_name if event.den else "unknown planet",
                "system": event.den.system_name if event.den else None,
                "detail": "not reinforced at the time -- check it",
            }
        )

    # Both live states count. "Started" is not finished -- it is an operation
    # already running against a clock, which is if anything more urgent than
    # one nobody has picked up yet.
    for operation in MercenaryTacticalOperation.objects.filter(
        den_character__in=own_den_characters(request.user),
        state__in=(
            MercenaryTacticalOperation.State.AVAILABLE,
            MercenaryTacticalOperation.State.STARTED,
        ),
    ).select_related("den__eve_planet", "den__eve_solar_system"):
        if operation.expires and operation.expires < now:
            continue
        soon = operation.expires and operation.expires < now + timedelta(hours=24)
        started = operation.state == MercenaryTacticalOperation.State.STARTED
        items.append(
            {
                "severity": "warning" if soon else "info",
                "when": operation.expires,
                "kind": "Tactical operation running"
                if started
                else "Tactical operation available",
                "where": operation.den.planet_name if operation.den else "?",
                "system": operation.den.system_name if operation.den else None,
                "detail": operation.type_name
                + (" -- started, not finished" if started else " -- not started yet"),
            }
        )

    officer_items = []
    if den_can_view_all(request.user):
        slots = visible_slots(request.user)
        for slot in slots:
            if slot.is_overdue:
                claim = slot.approved_claim
                officer_items.append(
                    {
                        "severity": "warning",
                        "when": claim.decided_at if claim else None,
                        "kind": "Approved but nothing anchored",
                        "where": slot.planet_name,
                        "system": slot.system_name,
                        "detail": claim.character.character_name if claim else "",
                    }
                )
        for skyhook in Skyhook.objects.filter(
            Q(workforce_siphon_percent__isnull=False)
            | Q(workforce_dropped_at__isnull=False),
            den_slot__in=slots,
        ).select_related("eve_planet", "eve_solar_system", "den_slot"):
            percent, taken, certainty = skyhook.siphon_estimate
            if percent is None:
                continue
            slot = skyhook.den_slot
            holder = slot.holder_name if slot else None
            officer_items.append(
                {
                    # A den we know about is a conversation; an unexplained
                    # one might be someone else's.
                    "severity": "warning" if holder else "danger",
                    "when": None,
                    "kind": "Den siphoning our workforce",
                    "where": skyhook.planet_name,
                    "system": skyhook.system_name,
                    "detail": (
                        f"{percent:.0f}% taken, {taken:,} a cycle"
                        f" -- {certainty}"
                        + (f", run by {holder}" if holder else ", owner unknown")
                    ),
                }
            )
        pending = DenClaim.objects.filter(
            slot__in=slots, status=DenClaim.Status.PENDING
        ).count()
        if pending:
            officer_items.append(
                {
                    "severity": "info",
                    "when": None,
                    "kind": f"{pending} claim(s) awaiting a decision",
                    "where": "Den admin",
                    "system": None,
                    "detail": "",
                }
            )

    order = {"critical": 0, "danger": 1, "warning": 2, "info": 3}
    far = now + timedelta(days=3650)
    for bucket in (items, officer_items):
        bucket.sort(key=lambda i: (order.get(i["severity"], 9), i["when"] or far))

    return render(
        request,
        "holdfast/den/dashboard.html",
        _context(
            request,
            items=items,
            officer_items=officer_items,
            my_den_count=len(mine),
            my_characters=own_den_characters(request.user),
        ),
    )


# --------------------------------------------------------------------------
# Own dens
# --------------------------------------------------------------------------


@require_any(*DEN_ANY)
def information(request):
    """Your own dens, in as much detail as ESI gives us."""
    dens = list(own_dens(request.user))
    characters = own_den_characters(request.user)
    claims = DenClaim.objects.filter(
        user=request.user, dismissed_at__isnull=True
    ).select_related("slot__skyhook__eve_planet", "character")
    return render(
        request,
        "holdfast/den/information.html",
        _context(
            request,
            dens=dens,
            characters=characters,
            claims=claims,
            operator=DenOperator.for_user(request.user),
            timezones=TIMEZONE_CHOICES,
        ),
    )


@require_any(*DEN_ANY)
def timers(request):
    """Your own den clocks: reinforcement, attacks and operations."""
    now = timezone.now()
    characters = own_den_characters(request.user)
    scope_all = den_can_view_all(request.user) and request.GET.get("scope") == "all"

    dens = MercenaryDen.objects.select_related(
        "eve_planet", "eve_solar_system", "den_character"
    )
    events = DenEvent.objects.select_related("den__eve_planet", "den_character")
    operations = MercenaryTacticalOperation.objects.select_related(
        "den__eve_planet", "den__eve_solar_system", "den_character"
    )
    if not scope_all:
        dens = dens.filter(den_character__in=characters)
        events = events.filter(den_character__in=characters)
        operations = operations.filter(den_character__in=characters)

    entries = []
    for den in dens.filter(reinforce_end__gte=now):
        entries.append(
            {
                "when": den.reinforce_end,
                "kind": "Den out of reinforcement",
                "where": den.planet_name,
                "system": den.system_name,
                "detail": str(den.den_character),
            }
        )
    for operation in operations.filter(expires__gte=now).exclude(
        state__in=[
            MercenaryTacticalOperation.State.COMPLETED,
            MercenaryTacticalOperation.State.REMOVED,
        ]
    ):
        entries.append(
            {
                "when": operation.expires,
                "kind": f"MTO {operation.get_state_display().lower()}",
                "where": operation.den.planet_name if operation.den else "?",
                "system": operation.den.system_name if operation.den else None,
                "detail": operation.type_name,
            }
        )
    entries.sort(key=lambda e: e["when"])

    recent = events.filter(timestamp__gte=now - timedelta(days=7)).order_by(
        "-timestamp"
    )[:40]
    return render(
        request,
        "holdfast/den/timers.html",
        _context(request, entries=entries, events=recent, now=now, scope_all=scope_all),
    )


# --------------------------------------------------------------------------
# The anonymous list
# --------------------------------------------------------------------------


@require_any(*DEN_LIST)
def den_list(request):
    """Which ground is taken -- without saying who is standing on it.

    Members need to know where they could put a den, and whether a site is
    hurting the alliance. They do not need a directory of who farms what.
    """
    rows = []
    for slot in visible_slots(request.user):
        skyhook = slot.skyhook
        siphon_percent, siphoned_amount, _certainty = skyhook.siphon_estimate
        rows.append(
            {
                "planet": slot.planet_name,
                "system": slot.system_name,
                "is_free": slot.status == DenSlot.Status.FREE,
                "status": "available"
                if slot.status == DenSlot.Status.FREE
                else "taken",
                "is_hostile": slot.recorded_den and slot.recorded_hostile,
                "siphon_percent": siphon_percent,
                "siphoned_amount": siphoned_amount,
                "is_impacting": siphon_percent is not None,
                "is_claimable": slot.is_claimable,
                "slot_pk": slot.pk,
            }
        )
    rows.sort(key=lambda r: (not r["is_free"], r["planet"]))
    return render(
        request,
        "holdfast/den/den_list.html",
        _context(
            request,
            rows=rows,
            free_count=sum(1 for r in rows if r["is_free"]),
            impacting_count=sum(1 for r in rows if r["is_impacting"]),
        ),
    )


# --------------------------------------------------------------------------
# Admin page
# --------------------------------------------------------------------------


@require_any(*DEN_FULL)
def admin_page(request):
    slots = list(visible_slots(request.user))
    pending = DenClaim.objects.filter(
        slot__in=slots, status=DenClaim.Status.PENDING
    ).select_related("slot__skyhook__eve_planet", "character", "user")

    # Contact card: everything Alliance Auth already knows, plus the timezone
    # the operator set themselves.
    # Grouped by person, not by character: four alts belonging to one member
    # are one contact card, not four identical ones.
    operators = []
    seen_users = {}
    for den_character in DenCharacter.objects.select_related(
        "character_ownership__character", "character_ownership__user"
    ):
        user = den_character.user
        entry = seen_users.get(user.pk)
        if entry is None:
            operator = DenOperator.for_user(user)
            entry = {
                "username": user.username,
                "main": getattr(user.profile, "main_character", None),
                "discord": den_character.discord_name,
                "timezone": operator.get_timezone_display() if operator.timezone else "",
                "local_time": operator.local_time,
                "note": operator.contact_note,
                "characters": [],
                "dens": [],
            }
            seen_users[user.pk] = entry
            operators.append(entry)
        entry["characters"].append(den_character.character.character_name)
        entry["dens"].extend(den_character.dens.all())

    siphoned = [
        skyhook
        for skyhook in Skyhook.objects.filter(
            Q(workforce_siphon_percent__isnull=False)
            | Q(workforce_dropped_at__isnull=False),
            den_slot__in=slots,
        ).select_related("eve_planet", "eve_solar_system", "den_slot")
        if skyhook.is_siphon_suspected
    ]

    # A siphon only matters to sovereignty once it actually knocks something
    # out, so flag the systems where both things are true at once.
    sov_impacted = []
    for skyhook in siphoned:
        starved = [
            upgrade
            for hub in skyhook.owner.sov_hubs.filter(
                solar_system_id=skyhook.eve_solar_system_id
            )
            for upgrade in hub.upgrades.all()
            if upgrade.power_state == "Low"
        ]
        if starved:
            sov_impacted.append({"skyhook": skyhook, "upgrades": starved})

    return render(
        request,
        "holdfast/den/admin.html",
        _context(
            request,
            slots=slots,
            pending_claims=pending,
            operators=operators,
            siphoned=siphoned,
            sov_impacted=sov_impacted,
        ),
    )


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


# Order matters: the permission check has to sit outside the token flow, or an
# unauthorised user gets bounced through EVE SSO before being turned away.
@require_any("den_claim")
@token_required(scopes=HOLDFAST_DEN_ESI_SCOPES)
def claim_slot(request, token, slot_pk):
    """Apply for a slot. Authorisation is collected up front.

    Taking the token at claim time rather than after approval means an approved
    claim is live immediately, instead of leaving a manager chasing someone to
    come back and authorise.
    """
    slot = get_object_or_404(DenSlot, pk=slot_pk)
    if not slot.is_claimable:
        messages.error(request, f"{slot.planet_name} is not available.")
        return redirect("holdfast:den_list")

    try:
        character = EveCharacter.objects.get(character_id=token.character_id)
    except EveCharacter.DoesNotExist:
        character = EveCharacter.objects.create_character(token.character_id)

    try:
        ownership = CharacterOwnership.objects.get(
            user=request.user, character=character
        )
    except CharacterOwnership.DoesNotExist:
        messages.error(
            request,
            f"{character} is not linked to your account. Add it on your character "
            "page first.",
        )
        return redirect("holdfast:den_list")

    if DenClaim.objects.filter(
        slot=slot, user=request.user, status=DenClaim.Status.PENDING
    ).exists():
        messages.info(request, f"You already have a claim pending on {slot.planet_name}.")
        return redirect("holdfast:den_list")

    den_character, created = DenCharacter.objects.get_or_create(
        character_ownership=ownership, defaults={"is_enabled": True}
    )
    if created:
        update_den_character_task.delay(den_character.pk)

    DenClaim.objects.create(slot=slot, user=request.user, character=character)
    messages.success(
        request,
        f"Claim submitted for {slot.planet_name} as {character}. "
        "A den manager will review it.",
    )
    return redirect("holdfast:den_list")


@require_any(*DEN_ANY)
@require_POST
def save_contact(request):
    """One set of contact details per person.

    Timezone belongs to the human, not to each alt -- somebody running four
    characters is still awake at one time of day.
    """
    operator = DenOperator.for_user(request.user)
    zone = request.POST.get("timezone", "").strip()
    if zone and zone not in dict(TIMEZONE_CHOICES):
        messages.error(request, f"'{zone}' is not one of the offered regions.")
        return redirect("holdfast:den_information")
    operator.timezone = zone
    operator.contact_note = request.POST.get("contact_note", "")[:200]
    operator.updated_at = timezone.now()
    operator.save(update_fields=["timezone", "contact_note", "updated_at"])
    messages.success(request, "Contact details saved.")
    return redirect("holdfast:den_information")


@require_any("den_claim")
@require_POST
def withdraw_claim(request, claim_pk):
    claim = get_object_or_404(DenClaim, pk=claim_pk, user=request.user)
    if claim.status != DenClaim.Status.PENDING:
        messages.error(request, "That claim has already been decided.")
        return redirect("holdfast:den_information")
    claim.status = DenClaim.Status.WITHDRAWN
    claim.decided_at = timezone.now()
    claim.save(update_fields=["status", "decided_at"])
    messages.success(request, f"Withdrew your claim on {claim.slot.planet_name}.")
    return redirect("holdfast:den_information")


@require_any(*DEN_ANY)
@require_POST
def dismiss_claim(request, claim_pk):
    """Clear a decided claim off your own page.

    A rejection you can do nothing about should not sit there forever. The row
    survives for anyone reviewing the history; it just stops following the
    applicant around.
    """
    claim = get_object_or_404(DenClaim, pk=claim_pk, user=request.user)
    if not claim.is_dismissable:
        messages.error(request, "That claim is still open. Withdraw it instead.")
        return redirect("holdfast:den_information")
    claim.dismissed_at = timezone.now()
    claim.save(update_fields=["dismissed_at"])
    messages.success(request, f"Cleared your claim on {claim.slot.planet_name}.")
    return redirect("holdfast:den_information")


@require_any(*DEN_ADMIN)
@require_POST
def decide_claim(request, claim_pk, decision):
    claim = get_object_or_404(DenClaim, pk=claim_pk)
    now = timezone.now()
    note = request.POST.get("decision_note", "")[:500]

    # Revoking acts on a claim that was granted, so it is the one decision
    # that starts from something other than pending.
    if decision == "revoke":
        if claim.status != DenClaim.Status.APPROVED:
            messages.error(request, "Only an approved claim can be revoked.")
            return redirect("holdfast:den_admin")
    elif claim.status != DenClaim.Status.PENDING:
        messages.error(request, "That claim has already been decided.")
        return redirect("holdfast:den_admin")

    if decision == "revoke":
        claim.status = DenClaim.Status.REVOKED
        messages.success(
            request,
            f"Revoked {claim.character}'s claim on {claim.slot.planet_name}. "
            "The slot is free again once the den comes down.",
        )
    elif decision == "approve":
        claim.status = DenClaim.Status.APPROVED
        # One slot, one holder: everyone else queued on it is turned down here
        # rather than left hanging.
        rejected = (
            DenClaim.objects.filter(slot=claim.slot, status=DenClaim.Status.PENDING)
            .exclude(pk=claim.pk)
            .update(
                status=DenClaim.Status.REJECTED,
                decided_at=now,
                decided_by=request.user,
                decision_note="Slot awarded to another applicant.",
            )
        )
        messages.success(
            request,
            f"Approved {claim.character} for {claim.slot.planet_name}"
            + (f", rejected {rejected} other applicant(s)." if rejected else "."),
        )
    else:
        claim.status = DenClaim.Status.REJECTED
        messages.success(request, f"Rejected the claim on {claim.slot.planet_name}.")

    claim.decided_at = now
    claim.decided_by = request.user
    claim.decision_note = note
    claim.save(update_fields=["status", "decided_at", "decided_by", "decision_note"])

    # The applicant hears about it through Auth's own bell rather than a
    # channel ping -- a rejection is nobody else's business.
    approved = claim.status == DenClaim.Status.APPROVED
    revoked = claim.status == DenClaim.Status.REVOKED
    where = f"{claim.slot.planet_name} in {claim.slot.system_name}"
    notify_user(
        claim.user,
        title=(
            f"Den site revoked: {claim.slot.planet_name}"
            if revoked
            else f"Den site approved: {claim.slot.planet_name}"
            if approved
            else f"Den site declined: {claim.slot.planet_name}"
        ),
        message=(
            (
                f"Your claim on {where} has been revoked. If you have a den "
                "anchored there, please unanchor it -- the slot stays marked "
                "as yours until you do."
            )
            if revoked
            else (
                f"Your claim on {where} was approved for {claim.character}. "
                "You can anchor a den there now."
            )
            if approved
            else f"Your claim on {where} was not granted."
        )
        + (f"\n\nNote from the manager: {note}" if note else ""),
        level="success" if approved else "warning",
    )
    invalidate_badges(claim.user)
    invalidate_badges(request.user)
    return redirect("holdfast:den_admin")


@require_any(*DEN_ADMIN)
@require_POST
def record_den(request, slot_pk):
    """Record, or clear, a den ESI will never show us.

    Both kinds go through here: a hostile den, and a friendly one whose
    operator has not registered a token. The form says which.
    """
    slot = get_object_or_404(DenSlot, pk=slot_pk)
    present = request.POST.get("present") == "1"
    slot.recorded_den = present
    slot.recorded_hostile = present and request.POST.get("hostile") == "1"
    slot.recorded_owner_note = request.POST.get("owner_note", "")[:200] if present else ""
    slot.recorded_corporation_note = (
        request.POST.get("corporation_note", "")[:200] if present else ""
    )
    slot.recorded_by = request.user if present else None
    slot.recorded_at = timezone.now() if present else None
    slot.save(
        update_fields=[
            "recorded_den",
            "recorded_hostile",
            "recorded_owner_note",
            "recorded_corporation_note",
            "recorded_by",
            "recorded_at",
        ]
    )
    messages.success(
        request,
        f"{'Recorded' if present else 'Cleared'} a den at {slot.planet_name}.",
    )
    return redirect("holdfast:den_admin")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


BOOL_FIELDS = (
    "notify_den_skyhook_impact",
    "notify_den_sov_impact",
    "den_discord_enabled",
)


@require_any(*DEN_ADMIN)
def settings_view(request):
    return render(
        request,
        "holdfast/den/settings.html",
        _context(
            request,
            routes=routes_for_section("den"),
            webhooks=Webhook.objects.all(),
        ),
    )


@require_any(*DEN_ADMIN)
@require_POST
def settings_save(request):
    config = HoldfastConfig.get_solo()
    errors = []

    for name, caster, floor in (
        ("den_anchor_grace_days", int, 1),
        ("workforce_drop_percent", float, 0),
        ("workforce_drop_grace_hours", int, 0),
    ):
        raw = request.POST.get(name, "").strip()
        try:
            value = caster(raw)
            if value < floor:
                raise ValueError
            setattr(config, name, value)
        except ValueError:
            errors.append(f"{name}: '{raw}' is not valid")

    for name in BOOL_FIELDS:
        setattr(config, name, request.POST.get(name) == "on")

    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("holdfast:den_settings")

    save_routes(request, "den")
    config.save()
    messages.success(request, "Den settings saved.")
    return redirect("holdfast:den_settings")


def _timezone_choices():
    return TIMEZONE_CHOICES


@require_any(*DEN_ANY)
def home(request):
    """Landing page for the sidebar entry, matched to the viewer's tier."""
    if den_can_see_list(request.user):
        return redirect("holdfast:den_dashboard")
    return redirect("holdfast:den_information")


@require_any(*DEN_ADMIN)
@require_POST
def settings_test(request):
    """Fire one sample alert per category in this section.

    Uses the real delivery path, so a message arriving proves the routing --
    not that a test-only shortcut works.
    """
    from ..core.alerts import send_test_for_section

    results = send_test_for_section("den")
    delivered = [name for name, ok in results.items() if ok]
    skipped = [name for name, ok in results.items() if not ok]
    if delivered:
        messages.success(
            request, f"Sent {len(delivered)} test alert(s): {', '.join(delivered)}"
        )
    if skipped:
        messages.warning(
            request,
            f"Not sent (no channel, or switched off): {', '.join(skipped)}",
        )
    return redirect("holdfast:den_settings")
