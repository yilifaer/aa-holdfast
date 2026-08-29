"""Alert evaluation and delivery.

Two flavours of alert live here:

* **Banded** alerts fire once per threshold band per event, keyed on something
  that changes when the underlying situation changes (a refuel pushes the
  predicted dry-out time into a different hour, so the key changes and the next
  band can fire again).
* **Edge-triggered** alerts fire once when a condition becomes true and are
  armed again only after the condition clears. Their AlertLog row is deleted
  the moment the condition goes away.

Either way an operator never gets the same warning twice an hour.
"""

import logging
from datetime import timedelta

from dhooks_lite import Embed, Field, Footer
from dhooks_lite import Webhook as DiscordWebhook
from django.utils import timezone

from ..app_settings import (
    HOLDFAST_SKYHOOK_MIN_UNSECURED,
)
from ..models import (
    CATEGORY_SECTIONS,
    AlertCategory,
    AlertLog,
    AlertRoute,
    HoldfastConfig,
    PowerState,
    ReagentThreshold,
    Skyhook,
    SovCampaign,
    SovHub,
    SovSystem,
    Webhook,
)

logger = logging.getLogger(__name__)

COLOR_INFO = 0x3A87AD
COLOR_WARNING = 0xF0AD4E
COLOR_DANGER = 0xD9534F

DOTLAN_SYSTEM = "https://evemaps.dotlan.net/system/{}"


# --------------------------------------------------------------------------
# Alert log bookkeeping
# --------------------------------------------------------------------------


def _already_sent(key: str) -> bool:
    return AlertLog.objects.filter(key=key).exists()


def _mark_sent(key: str) -> None:
    AlertLog.objects.update_or_create(key=key, defaults={"sent_at": timezone.now()})


def _disarm(key: str) -> None:
    """Forget an edge-triggered alert, and any state-stamped variants of it,
    so the condition can fire again the next time it appears."""
    AlertLog.objects.filter(key__startswith=key).delete()


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


SECTION_SWITCH = {
    "sov": "sov_discord_enabled",
    "skyhook": "skyhook_discord_enabled",
    "den": "den_discord_enabled",
}


def _webhooks_for(category):
    """Which channels this category goes to.

    A category with its own routing wins; otherwise every enabled webhook,
    so an install that has never touched the routing page keeps working.
    """
    if category is None:
        return list(Webhook.objects.filter(is_enabled=True))

    section = CATEGORY_SECTIONS.get(category)
    switch = SECTION_SWITCH.get(section)
    if switch and not getattr(HoldfastConfig.get_solo(), switch, True):
        return []

    route = AlertRoute.objects.filter(category=category).first()
    if route and not route.is_enabled:
        return []
    if route:
        chosen = list(route.webhooks.filter(is_enabled=True))
        if chosen:
            return chosen
    return list(Webhook.objects.filter(is_enabled=True))


def _send(embed: Embed, category=None) -> bool:
    """Post to the channels configured for this category.

    Callers must not record an alert as sent when this returns False, or a
    backlog of warnings would be silently swallowed while no webhook exists
    and never reappear once one is configured.
    """
    hooks = _webhooks_for(category)
    if not hooks:
        logger.info(
            "Alert raised but no channel is configured or enabled for %s: %s",
            category or "(uncategorised)",
            embed.title,
        )
        return False
    delivered = False
    for hook in hooks:
        content = None
        if hook.ping_type == Webhook.PingType.HERE:
            content = "@here"
        elif hook.ping_type == Webhook.PingType.EVERYONE:
            content = "@everyone"
        try:
            DiscordWebhook(hook.url).execute(
                content=content, embeds=[embed], username="SOV Monitor"
            )
            delivered = True
        except Exception:  # noqa: BLE001 - one broken hook must not stop the rest
            logger.exception("Failed to post to webhook %s", hook.name)
    return delivered


def moment(value) -> str:
    """One timestamp, written three ways.

    Every reader needs a different one of these. "In 3 days" is what you judge
    urgency by, the local time is what you put in your own calendar, and EVE
    time is the number people repeat to each other because the whole game runs
    on one clock. Discord localises the first two per reader; the third has to
    be spelled out.
    """
    if value is None:
        return "unknown"
    stamp = int(value.timestamp())
    return (
        f"<t:{stamp}:R>  |  <t:{stamp}:f>  |  "
        f"{value:%Y-%m-%d %H:%M} EVE"
    )


def moment_field(label, value) -> Field:
    return Field(name=label, value=moment(value), inline=False)


def _embed(
    title, description, color, fields=None, system_name=None, moments=None,
    footer_note=None,
) -> Embed:
    """Build an embed.

    ``moments`` is a list of ``(label, datetime)`` for the times this alert is
    actually about -- when a hub runs dry, when a window opens. The time the
    message happened to be posted is not one of them; nobody needs it, and it
    crowded out the times that matter.
    """
    all_fields = list(fields or [])
    for label, value in moments or []:
        if value is not None:
            all_fields.append(moment_field(label, value))
    return Embed(
        title=title,
        description=description,
        color=color,
        url=DOTLAN_SYSTEM.format(system_name.replace(" ", "_")) if system_name else None,
        fields=all_fields,
        footer=Footer(text=f"aa-holdfast - {footer_note}" if footer_note else "aa-holdfast"),
        timestamp=timezone.now(),
    )


def _hours(value) -> str:
    if value is None:
        return "unknown"
    if value < 1:
        return f"{value * 60:.0f} min"
    if value < 48:
        return f"{value:.1f} h"
    return f"{value / 24:.1f} d"


# --------------------------------------------------------------------------
# Sovereignty hub checks
# --------------------------------------------------------------------------


def check_hub_fuel() -> int:
    """Warn as a hub crosses each fuel band.

    The bands live in HoldfastConfig and are the same numbers that colour the
    dashboard, so what an operator sees on screen and what Discord shouts about
    can never drift apart.
    """
    sent = 0
    config = HoldfastConfig.get_solo()
    # Tightest band first. Widest-first plus the already-sent check walked a
    # hub up the ladder one run at a time: a hub first seen with twelve hours
    # of fuel got "warning", then ten minutes later "danger", then "critical" --
    # three messages, starting with the least alarming, for a situation that
    # was critical when we found it.
    bands = [
        ("critical", config.fuel_critical_days),
        ("danger", config.fuel_danger_days),
        ("warning", config.fuel_warning_days),
    ]
    colors = {
        "warning": COLOR_WARNING,
        "danger": COLOR_DANGER,
        "critical": COLOR_DANGER,
    }

    for hub in SovHub.objects.select_related("owner__corporation", "eve_solar_system"):
        hours = hub.hours_of_fuel_left
        if hours is None or not hub.fuel_expires_at:
            continue
        days = hours / 24
        # Bucket the predicted dry-out time by hour. Refuelling moves it, which
        # rotates the key and re-arms every band.
        stamp = hub.fuel_expires_at.strftime("%Y%m%d%H")

        # The tightest band this hub currently sits inside, and only that one.
        # Falling through to the next band when the tightest was already sent
        # is what produced a run of three messages for one hub: it has nothing
        # new to say, so it says nothing.
        severity = None
        for candidate, days_limit in bands:
            if days <= days_limit:
                severity = candidate
                break
        if severity is None:
            continue

        key = f"fuel:{hub.hub_id}:{severity}:{stamp}"
        if not _already_sent(key):
            reagents = [
                Field(
                    name=r.type_name,
                    value=f"{r.amount:,} left, burning {r.burning_per_hour:,}/h "
                    f"({_hours(r.hours_left)})",
                    inline=False,
                )
                for r in hub.reagents.all()
            ]
            delivered = _send(
                _embed(
                    title=f"Sov hub fuel {severity}: {hub.system_name}",
                    description=(
                        f"**{hub.system_name}** runs dry in "
                        f"**{_hours(hours)}**.\n"
                        f"Held by {hub.owner.corporation.corporation_name}."
                    ),
                    color=colors[severity],
                    fields=reagents,
                    moments=[("Runs dry", hub.fuel_expires_at)],
                    system_name=hub.system_name,
                ),
                category=AlertCategory.SOV_FUEL,
            )
            if delivered:
                _mark_sent(key)
                sent += 1
    return sent


def _den_cause_field(hub, config):
    """Name the siphon, when there is one and the setting asks for it.

    Returns a list, so it drops straight into ``fields=`` whether or not it
    found anything.
    """
    if not config.notify_upgrade_den_caused:
        return []
    from .siphon import siphons_in_system

    siphoned = siphons_in_system(hub)
    if not siphoned:
        return []
    return [
        Field(
            name="Likely cause",
            value="\n".join(
                f"{s.planet_name}: {s.siphon_estimate[0]:.0f}% of its workforce "
                f"({s.den_slot.holder_label if getattr(s, 'den_slot', None) else 'unknown'})"
                for s in siphoned
            )[:1024],
            inline=False,
        )
    ]


def check_hub_upgrades() -> int:
    """Edge-triggered warning for upgrades that went unpowered."""
    config = HoldfastConfig.get_solo()
    if not config.notify_upgrade_offline:
        return 0
    sent = 0
    for hub in SovHub.objects.select_related("owner__corporation", "eve_solar_system"):
        starved = [u for u in hub.upgrades.all() if u.power_state == PowerState.LOW]
        key = f"upglow:{hub.hub_id}"
        if not starved:
            _disarm(key)
            continue
        if _already_sent(key):
            continue
        delivered = _send(
            _embed(
                title=f"Sov upgrades unpowered: {hub.system_name}",
                description=(
                    f"{len(starved)} upgrade(s) in **{hub.system_name}** are in "
                    "`Low` state -- not enough fuel, power or workforce."
                ),
                color=COLOR_DANGER,
                fields=[
                    Field(name=u.type_name, value=u.power_state, inline=True)
                    for u in starved
                ]
                + [
                    Field(
                        name="Power",
                        value=f"{hub.power_allocated:,} / {hub.power_available:,}",
                        inline=True,
                    ),
                    Field(
                        name="Workforce",
                        value=f"{hub.workforce_allocated:,} / {hub.workforce_available:,}",
                        inline=True,
                    ),
                ]
                + _den_cause_field(hub, config),
                system_name=hub.system_name,
            ),
            category=AlertCategory.SOV_UPGRADE,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1
    return sent


# --------------------------------------------------------------------------
# Skyhook checks
# --------------------------------------------------------------------------


def check_skyhook_theft() -> int:
    """Remind us to collect before an owned skyhook becomes lootable.

    Each reagent is judged against its own bar from ReagentThreshold, so a
    hook sitting on a pile of Magmatic Gas can alert while one holding a
    handful of something cheap stays quiet. Reagents with no row configured
    fall back to HOLDFAST_SKYHOOK_MIN_UNSECURED.
    """
    sent = 0
    config = HoldfastConfig.get_solo()
    now = timezone.now()
    horizon = now + timedelta(minutes=config.skyhook_theft_lead_minutes)
    rules = {r.type_id: r for r in ReagentThreshold.objects.select_related("eve_type")}

    candidates = Skyhook.objects.filter(
        theft_start__isnull=False, theft_start__lte=horizon, theft_end__gte=now
    ).select_related("owner__corporation", "eve_planet", "eve_solar_system")

    for skyhook in candidates:
        tripped = []
        for reagent in skyhook.reagents.all():
            rule = rules.get(reagent.type_id)
            if rule is not None and not rule.is_enabled:
                continue
            bar = rule.min_unsecured if rule else HOLDFAST_SKYHOOK_MIN_UNSECURED
            if reagent.unsecured_stock >= bar:
                tripped.append((reagent, bar))
        if not tripped:
            continue

        key = f"theft:{skyhook.skyhook_id}:{skyhook.theft_start.strftime('%Y%m%d%H%M')}"
        if _already_sent(key):
            continue

        headline = max(tripped, key=lambda pair: pair[0].unsecured_stock)[0]
        delivered = _send(
            _embed(
                title=f"Skyhook lootable soon: {skyhook.planet_name}",
                description=(
                    f"**{headline.unsecured_stock:,}** unsecured "
                    f"{headline.type_name} at **{skyhook.planet_name}**."
                ),
                color=COLOR_WARNING,
                fields=[
                    Field(
                        name=f"{reagent.type_name} (bar {bar:,})",
                        value=(
                            f"{reagent.unsecured_stock:,} unsecured / "
                            f"{reagent.secured_stock:,} secured"
                        ),
                        inline=False,
                    )
                    for reagent, bar in tripped
                ],
                moments=[
                    ("Window opens", skyhook.theft_start),
                    ("Window closes", skyhook.theft_end),
                ],
                system_name=skyhook.system_name,
            ),
            category=AlertCategory.SKYHOOK_THEFT,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1
    return sent


def check_skyhook_attacks() -> int:
    """Fire when an owned skyhook enters a reinforced state."""
    sent = 0
    for skyhook in Skyhook.objects.select_related(
        "owner__corporation", "eve_planet", "eve_solar_system"
    ):
        key = f"reinf:{skyhook.skyhook_id}"
        if not skyhook.is_under_attack:
            _disarm(key)
            continue
        stamped = f"{key}:{skyhook.state}"
        if _already_sent(stamped):
            continue
        when = ""
        delivered = _send(
            _embed(
                title=f"Skyhook under attack: {skyhook.planet_name}",
                description=(
                    f"**{skyhook.planet_name}** is now "
                    f"`{skyhook.get_state_display()}`.{when}"
                ),
                color=COLOR_DANGER,
                fields=[
                    Field(name="System", value=skyhook.system_name, inline=True),
                    Field(
                        name="Corporation",
                        value=skyhook.owner.corporation.corporation_name,
                        inline=True,
                    ),
                ],
                moments=[("Comes out", skyhook.reinforce_end)],
                system_name=skyhook.system_name,
            ),
            category=AlertCategory.SKYHOOK_ATTACK,
        )
        if not delivered:
            continue
        _mark_sent(stamped)
        sent += 1
    return sent


# --------------------------------------------------------------------------
# ADM check
# --------------------------------------------------------------------------


def check_adm() -> int:
    # Read from the settings page, not from local.py. Leaving the field empty
    # there is how an alliance turns this off, and it did nothing.
    threshold = HoldfastConfig.get_solo().adm_alert_threshold
    if threshold is None:
        return 0
    sent = 0
    for system in SovSystem.objects.select_related("eve_solar_system"):
        adm = system.activity_defense_multiplier
        key = f"adm:{system.solar_system_id}"
        if adm is None or adm >= threshold:
            _disarm(key)
            continue
        if _already_sent(key):
            continue
        delivered = _send(
            _embed(
                title=f"ADM low: {system.system_name}",
                description=(
                    f"**{system.system_name}** is sitting at ADM **{adm:.2f}**, "
                    f"below the {threshold} threshold."
                ),
                color=COLOR_WARNING,
                fields=[
                    Field(name="Military", value=str(system.military_level), inline=True),
                    Field(
                        name="Industrial", value=str(system.industrial_level), inline=True
                    ),
                    Field(
                        name="Strategic", value=str(system.strategic_level), inline=True
                    ),
                ],
                system_name=system.system_name,
            ),
            category=AlertCategory.SOV_ADM,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1
    return sent


def check_hub_reinforced() -> int:
    """Fire when an Entosis defence event appears against one of our hubs.

    This is the only public signal that a hub was actually attacked -- the
    structure route reports the daily vulnerability window whether or not
    anyone turned up. Edge-triggered on the campaign existing, so it fires once
    per event and re-arms when the campaign resolves.
    """
    sent = 0
    ours = set(SovHub.objects.values_list("hub_id", flat=True))
    live = set()

    for campaign in SovCampaign.objects.filter(
        event_type=SovCampaign.EventType.IHUB
    ).select_related("eve_solar_system"):
        if ours and campaign.structure_id not in ours:
            continue
        key = f"sovreinf:{campaign.campaign_id}"
        live.add(key)
        if _already_sent(key):
            continue
        delivered = _send(
            _embed(
                title=f"Sov hub reinforced: {campaign.system_name}",
                description=(
                    f"An Entosis defence event is running in "
                    f"**{campaign.system_name}**."
                ),
                color=COLOR_DANGER,
                fields=[
                    Field(
                        name="Defender score",
                        value=f"{campaign.defender_score or 0:.2f}",
                        inline=True,
                    ),
                    Field(
                        name="Attacker score",
                        value=f"{campaign.attackers_score or 0:.2f}",
                        inline=True,
                    ),
                ],
                moments=[("Event starts", campaign.start_time)],
                system_name=campaign.system_name,
            ),
            category=AlertCategory.SOV_REINFORCED,
        )
        if not delivered:
            continue
        _mark_sent(key)
        sent += 1

    # A campaign that has gone means the event resolved, one way or another.
    AlertLog.objects.filter(key__startswith="sovreinf:").exclude(
        key__in=live
    ).delete()
    return sent


def run_all_checks() -> dict:
    # Imported here rather than at module level: den_alerts reuses the
    # delivery helpers above, so a top-level import would be circular.
    from .den_alerts import run_den_checks

    results = {
        "hub_fuel": check_hub_fuel(),
        "hub_upgrades": check_hub_upgrades(),
        "hub_reinforced": check_hub_reinforced(),
        "skyhook_theft": check_skyhook_theft(),
        "skyhook_attacks": check_skyhook_attacks(),
        "adm": check_adm(),
    }
    results.update(run_den_checks())
    return results


def send_test(category) -> bool:
    """Post a realistic sample of one category down its configured route.

    The sample is built to the same shape as the genuine alert and filled from
    real rows where the database has any, so what this checks is whether a real
    alert would be readable in that channel -- not merely whether the webhook
    responds.
    """
    from .alert_samples import send_sample

    return send_sample(category)


def send_test_for_section(section) -> dict:
    """Fire one sample per category in a section. Returns per-category results."""
    results = {}
    for category in AlertCategory:
        if CATEGORY_SECTIONS.get(category) != section:
            continue
        results[str(category)] = send_test(category)
    return results
