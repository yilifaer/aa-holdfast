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
from datetime import timedelta

from dhooks_lite import Field
from django.utils import timezone

from ..models import (
    AlertCategory,
    DenEvent,
    MercenaryDen,
    MercenaryTacticalOperation,
    Skyhook,
    HoldfastConfig,
)
from .notifications import invalidate_badges, notify_user
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

logger = logging.getLogger(__name__)


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
        fields = [
            Field(name="Operator", value=str(event.den_character), inline=True),
        ]
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
                f"{event.timestamp:%Y-%m-%d %H:%M} UTC. It was not reinforced "
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
                    f" It comes out at {den.reinforce_end:%Y-%m-%d %H:%M} UTC."
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
    operations = MercenaryTacticalOperation.objects.filter(
        state=MercenaryTacticalOperation.State.AVAILABLE
    ).select_related("den__eve_planet", "den__eve_solar_system", "eve_type", "den_character")

    for operation in operations:
        if operation.expires and operation.expires < now:
            continue
        key = f"mto:{operation.operation_id}"
        if _already_sent(key):
            continue
        den = operation.den
        where = den.planet_name if den else str(operation.mercenary_den_id)
        expiry = ""
        delivered = _send(
            _embed(
                title=f"Tactical operation available: {where}",
                description=f"**{operation.type_name}** is up at **{where}**.{expiry}",
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

        shortfall = skyhook.workforce_shortfall
        percent = skyhook.workforce_shortfall_percent
        known_den = MercenaryDen.objects.filter(skyhook_id=skyhook.skyhook_id).first()
        slot = getattr(skyhook, "den_slot", None)

        if known_den or (slot and slot.hostile_den_recorded):
            # We already know what is sitting there; no need to raise it as a
            # mystery. Mark it so the alert does not queue up forever.
            _mark_sent(key)
            continue

        delivered = _send(
            _embed(
                title=f"Workforce shortfall: {skyhook.planet_name}",
                description=(
                    f"**{skyhook.planet_name}** has been producing "
                    f"**{shortfall:,} less workforce** "
                    f"({percent:.0f}% below its own peak).\n"
                    "No den of ours is anchored there. A hostile den at anarchy "
                    "level 2 or above siphons workforce exactly like this."
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

        known = " Already recorded as hostile." if (slot and slot.hostile_den_recorded) else ""
        delivered = _send(
            _embed(
                title=f"Den siphoning workforce: {skyhook.planet_name}",
                description=(
                    f"**{skyhook.planet_name}** is producing "
                    f"**{skyhook.effective_workforce:,}** workforce against a base of "
                    f"**{skyhook.workforce_base:,}** -- a mercenary den is taking "
                    f"**{skyhook.workforce_siphon_percent:.0f}%**, "
                    f"{skyhook.siphoned_amount:,} a cycle. "
                    f"No den of ours is anchored here.{known}"
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
                        value=slot.status_label if slot else "not a temperate planet",
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
