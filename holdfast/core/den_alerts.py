"""Alerts about mercenary dens and workforce siphoning.

Three den events matter, and each has exactly one authoritative source. They
are deliberately *not* cross-wired, or every event would fire twice:

===================  ====================================  =====================
Event                Source used for the Discord alert     Why
===================  ====================================  =====================
Under attack now     ``MercenaryDenAttacked`` notification  The den route carries
                                                            no HP, so this is
                                                            the only signal.
Reinforced           den route, ``state == Paused``         Survives notification
                                                            roll-off; the
                                                            notification is
                                                            recorded silently.
New tactical op      MTO route, ``state == Available``      Carries the expiry
                                                            and dungeon type the
                                                            notification lacks.
===================  ====================================  =====================
"""

import logging
import re
from datetime import timedelta

from dhooks_lite import Field
from django.utils import timezone

from ..models import (
    AlertCategory,
    DenEvent,
    HoldfastConfig,
    MercenaryDen,
    MercenaryTacticalOperation,
    Skyhook,
)
from .alerts import (
    COLOR_DANGER,
    COLOR_INFO,
    COLOR_WARNING,
    _already_sent,
    _disarm,
    _embed,
    _mark_sent,
    _send,
)
from .den_sync import parse_notification_body
from .notifications import invalidate_badges, notify_user

logger = logging.getLogger(__name__)


def _strip_markup(value):
    """EVE writes names as showinfo links; the channel wants the name."""
    if not value:
        return ""
    return re.sub(r"<[^>]+>", "", str(value)).strip()


def check_den_attacks() -> int:
    """Post the one thing only notifications can tell us: it is being shot."""
    sent = 0
    pending = DenEvent.objects.filter(
        is_alerted=False, kind=DenEvent.Kind.ATTACKED
    ).select_related("den__eve_planet", "den__eve_solar_system", "den_character")

    for event in pending:
        den = event.den
        where = den.planet_name if den else "an unknown planet"
        system = den.system_name if den else None
        body = parse_notification_body(event.text)
        fields = [
            Field(name="Operator", value=str(event.den_character), inline=True),
        ]

        # The notification carries the damage state and who is shooting. That
        # is the whole reason to read notifications rather than the den route,
        # which has no HP at all -- so none of it should go to waste.
        layers = [
            (name, body.get(f"{key}Percentage"))
            for name, key in (("Shield", "shield"), ("Armor", "armor"), ("Hull", "hull"))
        ]
        if any(value is not None for _, value in layers):
            fields.append(
                Field(
                    name="Shield / Armor / Hull",
                    value=" / ".join(
                        f"{value:.0f}%" if value is not None else "?"
                        for _, value in layers
                    ),
                    inline=True,
                )
            )
        attacker = _strip_markup(
            body.get("aggressorCorporationName") or body.get("aggressorAllianceName")
        )
        if attacker:
            fields.append(Field(name="Attacker", value=attacker, inline=True))
        if den:
            fields.append(Field(name="System", value=den.system_name, inline=True))
            fields.append(
                Field(
                    name="Anarchy / Development",
                    value=f"{den.anarchy_number} / {den.development_number}",
                    inline=True,
                )
            )
        delivered = _send(
            _embed(
                title=f"Mercenary den under attack: {where}",
                description=(
                    f"A den at **{where}** is being shot right now. "
                    "It is not reinforced yet, so there is still a window to help."
                ),
                color=COLOR_DANGER,
                fields=fields,
                moments=[("Seen at", event.timestamp)],
                system_name=system,
            ),
            category=AlertCategory.DEN_ATTACK,
        )
        # The operator hears about their own den through Auth's bell too, so
        # it is waiting for them next login even if they missed the channel.
        notify_user(
            event.den_character.user,
            title=f"Your mercenary den is under attack: {where}",
            message=(
                f"A den at {where} was being shot at "
                f"{event.timestamp:%Y-%m-%d %H:%M} EVE. It was not reinforced "
                "at the time, so there may still be a window to save it."
            ),
            level="danger",
        )
        invalidate_badges(event.den_character.user)

        if not delivered:
            continue
        event.is_alerted = True
        event.save(update_fields=["is_alerted"])
        sent += 1

    # The other two kinds are kept for the on-screen timeline only; their
    # Discord alert comes from a route that carries more detail.
    DenEvent.objects.filter(is_alerted=False).exclude(
        kind=DenEvent.Kind.ATTACKED
    ).update(is_alerted=True)

    return sent


def check_den_reinforced() -> int:
    """Edge-triggered on the den route, which outlives notification roll-off."""
    sent = 0
    for den in MercenaryDen.objects.select_related(
        "eve_planet", "eve_solar_system", "den_character"
    ):
        key = f"denreinf:{den.den_id}"
        if den.state != MercenaryDen.State.PAUSED:
            _disarm(key)
            continue
        if _already_sent(key):
            continue
        when = ""
        delivered = _send(
            _embed(
                title=f"Mercenary den reinforced: {den.planet_name}",
                description=f"**{den.planet_name}** is reinforced.{when}",
                color=COLOR_DANGER,
                fields=[
                    Field(name="System", value=den.system_name, inline=True),
                    Field(name="Operator", value=str(den.den_character), inline=True),
                    Field(
                        name="On our ground",
                        value="yes" if den.is_on_our_ground else "no",
                        inline=True,
                    ),
                ],
                moments=[("Comes out", den.reinforce_end)],
                system_name=den.system_name,
            ),
            category=AlertCategory.DEN_REINFORCED,
        )
        notify_user(
            den.den_character.user,
            title=f"Your mercenary den is reinforced: {den.planet_name}",
            message=(
                f"{den.planet_name} in {den.system_name} is reinforced."
                + (
                    f" It comes out at {den.reinforce_end:%Y-%m-%d %H:%M} EVE."
                    if den.reinforce_end
                    else ""
                )
            ),
            level="danger",
        )
        invalidate_badges(den.den_character.user)

        if not delivered:
            continue
        _mark_sent(key)
        sent += 1
    return sent


def check_mto_available() -> int:
    """Tell the operator an MTO is up, with its expiry."""
    sent = 0
    now = timezone.now()
    # Both live states, for the same reason the dashboard counts both: an
    # operation somebody has started is still running against its expiry, and
    # ours arrive from ESI already marked Started.
    operations = MercenaryTacticalOperation.objects.filter(
        state__in=(
            MercenaryTacticalOperation.State.AVAILABLE,
            MercenaryTacticalOperation.State.STARTED,
        )
    ).select_related("den__eve_planet", "den__eve_solar_system", "den_character")

    for operation in operations:
        if operation.expires and operation.expires < now:
            continue
        key = f"mto:{operation.operation_id}"
        if _already_sent(key):
            continue
        den = operation.den
        where = den.planet_name if den else str(operation.mercenary_den_id)
        verb = (
            "running"
            if operation.state == MercenaryTacticalOperation.State.STARTED
            else "available"
        )
        expiry = ""
        delivered = _send(
            _embed(
                title=f"Tactical operation {verb}: {where}",
                description=f"**{operation.type_name}** is {verb} at **{where}**.{expiry}",
                color=COLOR_INFO,
                fields=[
                    Field(name="Operator", value=str(operation.den_character), inline=True),
                    Field(
                        name="System",
                        value=den.system_name if den else "?",
                        inline=True,
                    ),
                ],
                moments=[("Expires", operation.expires)],
                system_name=den.system_name if den else None,
            ),
            category=AlertCategory.DEN_MTO,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1
    return sent


def check_workforce_siphon() -> int:
    """Flag a skyhook whose workforce has sat below its own peak for a while.

    This is the only automatic way to notice someone else's den: ESI will not
    show it, but from anarchy level 2 the den starts taking the skyhook's
    workforce, and that shows up as a shortfall against the skyhook's own
    historic best.
    """
    sent = 0
    config = HoldfastConfig.get_solo()
    cutoff = timezone.now() - timedelta(hours=config.workforce_drop_grace_hours)

    for skyhook in Skyhook.objects.filter(
        workforce_dropped_at__isnull=False
    ).select_related("eve_planet", "eve_solar_system", "owner__corporation"):
        key = f"siphon:{skyhook.skyhook_id}"
        if skyhook.workforce_dropped_at > cutoff:
            continue  # not yet persistent enough to be worth shouting about
        if _already_sent(key):
            continue

        percent, shortfall, certainty = skyhook.siphon_estimate
        if percent is None:
            percent = skyhook.workforce_shortfall_percent
            shortfall = skyhook.workforce_shortfall
        known_den = MercenaryDen.objects.filter(skyhook_id=skyhook.skyhook_id).first()
        slot = getattr(skyhook, "den_slot", None)
        holder = slot.holder_name if slot else None
        ours = bool(known_den or (slot and slot.recorded_den))

        # Knowing whose den it is changes the wording, not whether to speak.
        # An earlier version fell silent here, reasoning that a den we already
        # know about is no mystery -- but once a census is loaded that covers
        # nearly every slot, and the alert went quiet everywhere. A den that
        # has *started* taking workforce is news either way: it has just
        # reached anarchy 2, and the output came off our own sovereignty.
        delivered = _send(
            _embed(
                title=(
                    f"Our own den is siphoning: {skyhook.planet_name}"
                    if ours
                    else f"Workforce shortfall: {skyhook.planet_name}"
                ),
                description=(
                    (
                        f"The den at **{skyhook.planet_name}** has started "
                        f"taking workforce: **{shortfall:,} a cycle** off this "
                        f"skyhook.\n"
                        f"{f'**{holder}**' if holder else 'A den we know about'}"
                        " runs it. That output was feeding our own "
                        "sovereignty, so this is worth a word rather than a "
                        "fleet."
                    )
                    if ours
                    else (
                        f"**{skyhook.planet_name}** has been producing "
                        f"**{shortfall:,} less workforce** "
                        f"({percent:.0f}% below its own peak).\n"
                        "No den of ours is anchored there. A hostile den at "
                        "anarchy level 2 or above siphons workforce exactly "
                        "like this."
                    )
                ),
                color=COLOR_WARNING,
                fields=[
                    Field(name="System", value=skyhook.system_name, inline=True),
                    Field(
                        name="Now / peak",
                        value=f"{skyhook.effective_workforce:,} / "
                        f"{skyhook.workforce_high_water:,}",
                        inline=True,
                    ),
                    Field(
                        name="Corporation",
                        value=skyhook.owner.corporation.corporation_name,
                        inline=True,
                    ),
                    Field(
                        name="How we know",
                        value={
                            "measured": "the workforce is not a round number "
                            "-- arithmetic, not a guess",
                            "inferred": "it fell to exactly 90/80/70% of a "
                            "peak we recorded ourselves",
                            "suspected": "below its own peak, but no siphon "
                            "rate explains the figure",
                        }.get(certainty, "below its own peak"),
                        inline=False,
                    ),
                ],
                moments=[("Dropping since", skyhook.workforce_dropped_at)],
                system_name=skyhook.system_name,
            ),
            category=AlertCategory.DEN_SIPHON,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1

    # Recovered skyhooks re-arm.
    for skyhook in Skyhook.objects.filter(workforce_dropped_at__isnull=True):
        _disarm(f"siphon:{skyhook.skyhook_id}")

    return sent


def check_siphoned_skyhooks() -> int:
    """Fire on the workforce fingerprint: an un-round output means a den.

    Unlike the high-water check this needs no history, so it catches a den that
    was already sitting there before this app ever ran. See core.siphon for why
    the arithmetic works.
    """
    sent = 0
    for skyhook in Skyhook.objects.filter(
        workforce_siphon_percent__isnull=False
    ).select_related("eve_planet", "eve_solar_system", "owner__corporation"):
        key = f"siphonfp:{skyhook.skyhook_id}:{skyhook.workforce_siphon_percent}"
        if _already_sent(key):
            continue

        slot = getattr(skyhook, "den_slot", None)
        ours = MercenaryDen.objects.filter(skyhook_id=skyhook.skyhook_id).first()
        if ours:
            # One of our own operators, already accounted for.
            _mark_sent(key)
            continue

        # Who is on it, if anyone knows. Saying "no den of ours is anchored
        # here" while the field underneath names the operator reads as a
        # contradiction, so the sentence follows what we actually know.
        whose = slot.holder_label if slot else "unknown"
        delivered = _send(
            _embed(
                title=f"Den siphoning workforce: {skyhook.planet_name}",
                description=(
                    f"**{skyhook.planet_name}** is producing "
                    f"**{skyhook.effective_workforce:,}** workforce against a base of "
                    f"**{skyhook.workforce_base:,}** -- a mercenary den is taking "
                    f"**{skyhook.workforce_siphon_percent:.0f}%**, "
                    f"{skyhook.siphoned_amount:,} a cycle. "
                    + (
                        f"Run by {whose}."
                        if whose != "unknown"
                        else "A den is here -- this alert is proof of it -- "
                        "but nobody has recorded whose."
                    )
                ),
                color=COLOR_WARNING,
                fields=[
                    Field(name="System", value=skyhook.system_name, inline=True),
                    Field(
                        name="Corporation",
                        value=skyhook.owner.corporation.corporation_name,
                        inline=True,
                    ),
                    Field(
                        name="Den slot",
                        value=slot.holder_label if slot else "not a temperate planet",
                        inline=True,
                    ),
                ],
                system_name=skyhook.system_name,
            ),
            category=AlertCategory.DEN_SIPHON,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1

    # Anything that went back to a round number has had its den removed.
    for skyhook in Skyhook.objects.filter(workforce_siphon_percent__isnull=True):
        _disarm(f"siphonfp:{skyhook.skyhook_id}")

    return sent


def run_den_checks() -> dict:
    return {
        "den_attacks": check_den_attacks(),
        "den_reinforced": check_den_reinforced(),
        "mto_available": check_mto_available(),
        "workforce_siphon": check_workforce_siphon(),
        "siphon_fingerprint": check_siphoned_skyhooks(),
    }
