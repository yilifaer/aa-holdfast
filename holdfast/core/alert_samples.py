"""Test alerts that look like the real thing.

A test that posts "this is a test" proves the webhook is reachable and nothing
else. What an operator actually needs to check before trusting a channel is
whether a *real* alert is readable there: does the title say enough on a phone
lock screen, is the timer legible, does the ping land in the right room.

So each sample is built to the same shape as the genuine alert -- same title,
same description, same fields -- and filled from real rows in the database
wherever there are any. Only the footer says it was a test.
"""

import logging
from datetime import timedelta

from dhooks_lite import Field
from django.utils import timezone

from ..models import (
    AlertCategory,
    DenSlot,
    MercenaryDen,
    Skyhook,
    SovCampaign,
    SovHub,
    HoldfastConfig,
    SovSystem,
)
from .alerts import COLOR_DANGER, COLOR_INFO, COLOR_WARNING, _embed, _send

logger = logging.getLogger(__name__)

TEST_FOOTER = "TEST, no action needed"


def _hours(value):
    if value is None:
        return "unknown"
    if value < 1:
        return f"{value * 60:.0f} min"
    if value < 48:
        return f"{value:.1f} h"
    return f"{value / 24:.1f} d"


def _any_hub():
    return (
        SovHub.objects.select_related("owner__corporation", "eve_solar_system")
        .prefetch_related("reagents__eve_type", "upgrades__eve_type")
        .first()
    )


def _any_skyhook(with_reagents=True):
    qs = Skyhook.objects.select_related(
        "owner__corporation", "eve_planet", "eve_solar_system"
    ).prefetch_related("reagents__eve_type")
    if with_reagents:
        found = qs.filter(reagents__isnull=False).distinct().first()
        if found:
            return found
    return qs.first()


def _any_slot():
    return DenSlot.objects.select_related(
        "skyhook__eve_planet", "skyhook__eve_solar_system"
    ).first()


# --------------------------------------------------------------------------
# One builder per category, mirroring the real alert
# --------------------------------------------------------------------------


def _sov_fuel():
    hub = _any_hub()
    now = timezone.now()
    system = hub.system_name if hub else "J-ABCD"
    corporation = (
        hub.owner.corporation.corporation_name if hub else "Example Corporation"
    )
    expires = (hub.fuel_expires_at if hub and hub.fuel_expires_at else now + timedelta(days=2.6))
    hours = (expires - now).total_seconds() / 3600
    reagents = (
        [
            Field(
                name=r.type_name,
                value=f"{r.amount:,} left, burning {r.burning_per_hour:,}/h "
                f"({_hours(r.hours_left)})",
                inline=False,
            )
            for r in hub.reagents.all()
        ]
        if hub and hub.reagents.exists()
        else [
            Field(
                name="Magmatic Gas",
                value="128,400 left, burning 205/h (26.1 d)",
                inline=False,
            )
        ]
    )
    return _embed(
        title=f"Sov hub fuel danger: {system}",
        description=(
            f"**{system}** runs dry in **{_hours(hours)}**.\n"
            f"Held by {corporation}."
        ),
        color=COLOR_DANGER,
        fields=reagents,
        moments=[("Runs dry", expires)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _sov_upgrade():
    hub = _any_hub()
    system = hub.system_name if hub else "J-ABCD"
    upgrades = list(hub.upgrades.all())[:2] if hub else []
    fields = [
        Field(name=u.type_name, value="Low", inline=True) for u in upgrades
    ] or [Field(name="Cynosural Navigation", value="Low", inline=True)]
    fields += [
        Field(
            name="Power",
            value=f"{hub.power_allocated:,} / {hub.power_available:,}"
            if hub
            else "2,000 / 1,860",
            inline=True,
        ),
        Field(
            name="Workforce",
            value=f"{hub.workforce_allocated:,} / {hub.workforce_available:,}"
            if hub
            else "19,900 / 17,400",
            inline=True,
        ),
    ]
    return _embed(
        title=f"Sov upgrades unpowered: {system}",
        description=(
            f"{len(fields) - 2} upgrade(s) in **{system}** are in `Low` state "
            "-- not enough fuel, power or workforce."
        ),
        color=COLOR_DANGER,
        fields=fields,
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _sov_adm():
    config = HoldfastConfig.get_solo()
    system = (
        SovSystem.objects.select_related("eve_solar_system")
        .order_by("activity_defense_multiplier")
        .first()
    )
    name = system.system_name if system else "J-ABCD"
    adm = system.activity_defense_multiplier if system else 2.10
    threshold = config.adm_alert_threshold or 3.0
    return _embed(
        title=f"ADM low: {name}",
        description=(
            f"**{name}** is sitting at ADM **{adm:.2f}**, below the "
            f"{threshold} threshold."
        ),
        color=COLOR_WARNING,
        fields=[
            Field(name="Military", value=str(system.military_level if system else 3), inline=True),
            Field(name="Industrial", value=str(system.industrial_level if system else 0), inline=True),
            Field(name="Strategic", value=str(system.strategic_level if system else 5), inline=True),
        ],
        system_name=name,
        footer_note=TEST_FOOTER,
    )


def _sov_reinforced():
    campaign = SovCampaign.objects.select_related("eve_solar_system").first()
    hub = _any_hub()
    system = (
        campaign.system_name if campaign else (hub.system_name if hub else "J-ABCD")
    )
    start = campaign.start_time if campaign else timezone.now() + timedelta(hours=19)
    return _embed(
        title=f"Sov hub reinforced: {system}",
        description=(
            f"An Entosis defence event is running in **{system}**."
        ),
        color=COLOR_DANGER,
        fields=[
            Field(
                name="Defender score",
                value=f"{campaign.defender_score or 0:.2f}" if campaign else "0.00",
                inline=True,
            ),
            Field(
                name="Attacker score",
                value=f"{campaign.attackers_score or 0:.2f}" if campaign else "0.00",
                inline=True,
            ),
        ],
        moments=[("Event starts", start)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _skyhook_theft():
    skyhook = _any_skyhook()
    now = timezone.now()
    planet = skyhook.planet_name if skyhook else "J-ABCD IV"
    system = skyhook.system_name if skyhook else "J-ABCD"
    start = skyhook.theft_start if skyhook and skyhook.theft_start else now + timedelta(minutes=40)
    end = skyhook.theft_end if skyhook and skyhook.theft_end else start + timedelta(hours=2)
    reagents = list(skyhook.reagents.all()) if skyhook else []
    headline = max(reagents, key=lambda r: r.unsecured_stock) if reagents else None
    amount = headline.unsecured_stock if headline else 137_218
    name = headline.type_name if headline else "Magmatic Gas"
    fields = (
        [
            Field(
                name=f"{r.type_name} (bar 100,000)",
                value=f"{r.unsecured_stock:,} unsecured / {r.secured_stock:,} secured",
                inline=False,
            )
            for r in reagents
        ]
        if reagents
        else [
            Field(
                name="Magmatic Gas (bar 100,000)",
                value="137,218 unsecured / 41,520 secured",
                inline=False,
            )
        ]
    )
    return _embed(
        title=f"Skyhook lootable soon: {planet}",
        description=(
            f"**{amount:,}** unsecured {name} at **{planet}**."
        ),
        color=COLOR_WARNING,
        fields=fields,
        moments=[("Window opens", start), ("Window closes", end)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _skyhook_attack():
    skyhook = _any_skyhook(with_reagents=False)
    planet = skyhook.planet_name if skyhook else "J-ABCD IV"
    system = skyhook.system_name if skyhook else "J-ABCD"
    corporation = (
        skyhook.owner.corporation.corporation_name if skyhook else "Example Corporation"
    )
    out = timezone.now() + timedelta(hours=31)
    return _embed(
        title=f"Skyhook under attack: {planet}",
        description=(
            f"**{planet}** is now `Armor reinforced`."
        ),
        color=COLOR_DANGER,
        fields=[
            Field(name="System", value=system, inline=True),
            Field(name="Corporation", value=corporation, inline=True),
        ],
        moments=[("Comes out", out)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _den_attack():
    den = MercenaryDen.objects.select_related(
        "eve_planet", "eve_solar_system", "den_character"
    ).first()
    slot = _any_slot()
    planet = (
        den.planet_name if den else (slot.planet_name if slot else "J-ABCD IV")
    )
    system = den.system_name if den else (slot.system_name if slot else "J-ABCD")
    operator = str(den.den_character) if den else "Example Character"
    now = timezone.now()
    return _embed(
        title=f"Mercenary den under attack: {planet}",
        description=(
            f"A den at **{planet}** is being shot right now. "
            "It is not reinforced yet, so there is still a window to help."
        ),
        color=COLOR_DANGER,
        fields=[
            Field(name="Operator", value=operator, inline=True),
            Field(name="System", value=system, inline=True),
            Field(name="Anarchy / Development", value="2 / 3", inline=True),
        ],
        moments=[("Seen at", now)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _den_reinforced():
    den = MercenaryDen.objects.select_related(
        "eve_planet", "eve_solar_system", "den_character"
    ).first()
    slot = _any_slot()
    planet = den.planet_name if den else (slot.planet_name if slot else "J-ABCD IV")
    system = den.system_name if den else (slot.system_name if slot else "J-ABCD")
    operator = str(den.den_character) if den else "Example Character"
    out = timezone.now() + timedelta(hours=27)
    return _embed(
        title=f"Mercenary den reinforced: {planet}",
        description=(
            f"**{planet}** is reinforced."
        ),
        color=COLOR_DANGER,
        fields=[
            Field(name="System", value=system, inline=True),
            Field(name="Operator", value=operator, inline=True),
            Field(name="On our ground", value="yes", inline=True),
        ],
        moments=[("Comes out", out)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _den_mto():
    den = MercenaryDen.objects.select_related(
        "eve_planet", "eve_solar_system", "den_character"
    ).first()
    slot = _any_slot()
    planet = den.planet_name if den else (slot.planet_name if slot else "J-ABCD IV")
    system = den.system_name if den else (slot.system_name if slot else "J-ABCD")
    operator = str(den.den_character) if den else "Example Character"
    expires = timezone.now() + timedelta(hours=20)
    return _embed(
        title=f"Tactical operation available: {planet}",
        description=(
            f"**Mercenary Tactical Operation** is up at **{planet}**."
        ),
        color=COLOR_INFO,
        fields=[
            Field(name="Operator", value=operator, inline=True),
            Field(name="System", value=system, inline=True),
        ],
        moments=[("Expires", expires)],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


def _den_siphon():
    skyhook = (
        Skyhook.objects.filter(workforce_siphon_percent__isnull=False)
        .select_related("eve_planet", "eve_solar_system", "owner__corporation")
        .first()
    )
    # The slot under the skyhook this sample is built from, not just any slot:
    # a test ping that names one planet and describes a different planet's
    # ground is worse than no sample at all.
    slot = getattr(skyhook, "den_slot", None) or _any_slot()
    planet = (
        skyhook.planet_name if skyhook else (slot.planet_name if slot else "J-ABCD IV")
    )
    system = skyhook.system_name if skyhook else (slot.system_name if slot else "J-ABCD")
    now_value = skyhook.effective_workforce if skyhook else 7_605
    base = skyhook.workforce_base if skyhook else 8_450
    percent = skyhook.workforce_siphon_percent if skyhook else 10
    corporation = (
        skyhook.owner.corporation.corporation_name if skyhook else "Example Corporation"
    )
    whose = slot.holder_label if slot else "unknown"
    return _embed(
        title=f"Den siphoning workforce: {planet}",
        description=(
            f"**{planet}** is producing **{now_value:,}** workforce against a "
            f"base of **{base:,}** -- a mercenary den is taking "
            f"**{percent:.0f}%**, {base - now_value:,} a cycle. "
            # An alert that fired at all is proof a den is there. The only
            # open question is whose, so the sentence never says "none".
            + (
                f"Run by {whose}."
                if whose != "unknown"
                else "A den is here -- this alert is proof of it -- "
                "but nobody has recorded whose."
            )
        ),
        color=COLOR_WARNING,
        fields=[
            Field(name="System", value=system, inline=True),
            Field(name="Corporation", value=corporation, inline=True),
            Field(name="Den slot", value=whose, inline=True),
        ],
        system_name=system,
        footer_note=TEST_FOOTER,
    )


BUILDERS = {
    AlertCategory.SOV_FUEL: _sov_fuel,
    AlertCategory.SOV_UPGRADE: _sov_upgrade,
    AlertCategory.SOV_ADM: _sov_adm,
    AlertCategory.SOV_REINFORCED: _sov_reinforced,
    AlertCategory.SKYHOOK_THEFT: _skyhook_theft,
    AlertCategory.SKYHOOK_ATTACK: _skyhook_attack,
    AlertCategory.DEN_ATTACK: _den_attack,
    AlertCategory.DEN_REINFORCED: _den_reinforced,
    AlertCategory.DEN_MTO: _den_mto,
    AlertCategory.DEN_SIPHON: _den_siphon,
}


def send_sample(category) -> bool:
    """Post a realistic sample of one category down its configured route."""
    builder = BUILDERS.get(category)
    if builder is None:
        logger.warning("No sample builder for %s", category)
        return False
    try:
        embed = builder()
    except Exception:  # noqa: BLE001 - a broken sample must not break the page
        logger.exception("Could not build a sample for %s", category)
        return False
    return _send(embed, category=category)
