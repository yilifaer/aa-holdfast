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

from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from dhooks_lite import Embed, Field, Footer
from dhooks_lite import Webhook as DiscordWebhook
from django.apps import apps
from django.utils import timezone

from ..app_settings import (
    HOLDFAST_SKYHOOK_MIN_UNSECURED,
)
from ..models import (
    CATEGORY_SECTIONS,
    AlertCategory,
    AlertLog,
    AlertRoute,
    DenEvent,
    HoldfastConfig,
    MercenaryDen,
    MercenaryTacticalOperation,
    Owner,
    PowerState,
    ReagentThreshold,
    Skyhook,
    SovCampaign,
    SovHub,
    SovSystem,
    Webhook,
)
from .scope import tracked_alliance_ids

logger = get_extension_logger(__name__)


def _active_owner_ids() -> set:
    """Owners whose structures may alert. Every check here walks *all* rows
    and disarms the ones outside this set, so the disarm set is exactly the
    complement of the firing set: a key left behind by a disabled owner would
    otherwise suppress the alert when it is switched back on still starved or
    still reinforced."""
    return set(Owner.objects.active().values_list("pk", flat=True))

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
# Pruning the alert log
# --------------------------------------------------------------------------
#
# A row here is what stops an alert repeating, so a row is only worth pruning
# once nothing could write the same key again. Two kinds of key:
#
# * Edge keys (upglow, reinf, adm, sovreinf, denreinf, bell:denreinf, siphon,
#   siphonfp) are deleted by their own check the run the condition clears --
#   it walks every row and disarms what no longer fires. One still standing
#   means the condition still holds, so it stays while its subject exists.
#   Once the subject is gone nothing disarms it, and it can go.
# * Stamp keys (fuel, theft, mto, bell:denattack) are never disarmed. They
#   stay while the check would still write the same key today: the hub still
#   predicts the same dry-out hour, the theft window has not closed, the
#   operation is still live, the attack is still pending.
#
# A key of no kind listed here (an older release's) ages out as before.


def _hub_exists(hub_id):
    return SovHub.objects.filter(hub_id=hub_id).exists()


def _skyhook_exists(skyhook_id):
    return Skyhook.objects.filter(skyhook_id=skyhook_id).exists()


def _den_exists(den_id):
    return MercenaryDen.objects.filter(den_id=den_id).exists()


def _fuel_still_predicted(hub_id, _severity, stamp):
    expires = (
        SovHub.objects.filter(hub_id=hub_id).values_list("fuel_expires_at", flat=True).first()
    )
    return bool(expires) and expires.strftime("%Y%m%d%H") == stamp


def _theft_window_ahead(skyhook_id, stamp):
    skyhook = Skyhook.objects.filter(skyhook_id=skyhook_id).first()
    return bool(
        skyhook
        and skyhook.theft_start
        and skyhook.theft_end
        and skyhook.theft_start.strftime("%Y%m%d%H%M") == stamp
        and skyhook.theft_end >= timezone.now()
    )


def _operation_live(operation_id):
    operation = MercenaryTacticalOperation.objects.filter(operation_id=operation_id).first()
    return bool(
        operation
        and operation.is_actionable
        and (operation.expires is None or operation.expires >= timezone.now())
    )


def _attack_pending(notification_id):
    return DenEvent.objects.filter(notification_id=notification_id, is_alerted=False).exists()


# kind -> (fields the key carries after the kind, whether the row still counts)
_KEEP_WHILE = {
    "fuel": (3, _fuel_still_predicted),
    "theft": (2, _theft_window_ahead),
    "mto": (1, _operation_live),
    "bell:denattack": (1, _attack_pending),
    "upglow": (1, _hub_exists),
    "reinf": (2, lambda skyhook_id, _state: _skyhook_exists(skyhook_id)),
    "adm": (1, lambda system_id: SovSystem.objects.filter(solar_system_id=system_id).exists()),
    "sovreinf": (1, lambda campaign_id: SovCampaign.objects.filter(campaign_id=campaign_id).exists()),
    "denreinf": (1, _den_exists),
    "bell:denreinf": (1, _den_exists),
    "siphon": (1, _skyhook_exists),
    "siphonfp": (2, lambda skyhook_id, _percent: _skyhook_exists(skyhook_id)),
}


def _still_suppressing(key: str) -> bool:
    for kind, (count, keep) in _KEEP_WHILE.items():
        if not key.startswith(f"{kind}:"):
            continue
        fields = key[len(kind) + 1 :].split(":", count - 1)
        if len(fields) != count:
            return False
        try:
            return keep(*fields)
        except (ValueError, TypeError):  # a malformed id; it can stop nothing
            return False
    return False


def prune_alert_log(cutoff) -> int:
    """Delete rows older than ``cutoff`` that can no longer stop an alert."""
    doomed = [
        pk
        for pk, key in AlertLog.objects.filter(sent_at__lt=cutoff).values_list("pk", "key")
        if not _still_suppressing(key)
    ]
    deleted = 0
    # In slices: the first prune after an upgrade can face thousands of rows,
    # more than one IN list should carry.
    for start in range(0, len(doomed), 500):
        count, _ = AlertLog.objects.filter(pk__in=doomed[start : start + 500]).delete()
        deleted += count
    return deleted


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


SECTION_SWITCH = {
    "sov": "sov_discord_enabled",
    "skyhook": "skyhook_discord_enabled",
    "den": "den_discord_enabled",
}


def route_channels(route):
    """The working channels a route picks, or None when it picks none.

    None means nobody routed the category and it falls back to every enabled
    webhook. A routed category whose channels have all been deleted or
    switched off gets an empty list -- it goes nowhere. Falling back there
    sent a category meant for one private channel into every channel the
    moment its webhook went away, which is a leak, not a safety net.
    """
    picked = list(route.webhooks.all())
    if not picked and not route.is_routed:
        return None
    return [hook for hook in picked if hook.is_enabled]


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
    chosen = route_channels(route) if route else None
    if chosen is not None:
        return chosen
    return list(Webhook.objects.filter(is_enabled=True))


def stranded_routes(section=None):
    """Switched-on categories that were routed and have no working channel
    left, so their alerts go nowhere until somebody routes them again."""
    routes = AlertRoute.objects.filter(is_enabled=True).prefetch_related("webhooks")
    return [
        route
        for route in routes
        if (section is None or route.section == section) and route_channels(route) == []
    ]


def routes_left_without(hook):
    """Categories this webhook is the last working channel of.

    Asked before it is deleted or switched off, to say which alerts will stop.
    A webhook already off is nobody's working channel, so it strands nothing.
    """
    if not hook.is_enabled:
        return []
    return [
        route
        for route in AlertRoute.objects.filter(webhooks=hook, is_enabled=True)
        .prefetch_related("webhooks")
        .order_by("category")
        if not any(other.is_enabled and other.pk != hook.pk for other in route.webhooks.all())
    ]


def route_names(routes) -> str:
    return ", ".join(route.get_category_display() for route in routes)


def pin_routes_of(hook) -> None:
    """Mark every route that ticks this webhook as routed, before it goes.

    Deleting the webhook deletes the ticks; the flag is what keeps those
    categories from falling back to every channel afterwards. Routes saved
    since 0.1.12 carry it already -- this covers rows ticked in Django admin.
    """
    AlertRoute.objects.filter(webhooks=hook).update(is_routed=True)


def _discord_service_installed() -> bool:
    return apps.is_installed("allianceauth.services.modules.discord")


def _lookup_role_id(group):
    """The Discord role id for an Auth group, or None when the server has no
    role of that name. Auth's client keeps the guild's role list cached, so
    this is one request per process rather than one per group per alert."""
    from allianceauth.services.modules.discord.models import DiscordUser

    role = DiscordUser.objects.group_to_role(group)
    return role.get("id") if role else None


def _role_mentions(hook) -> list:
    """Role mentions for the groups on a webhook.

    A group whose role cannot be found is logged and skipped rather than
    failing the post: a fuel warning with one missing ping is still a fuel
    warning.
    """
    groups = list(hook.ping_groups.all())
    if not groups:
        return []
    if not _discord_service_installed():
        logger.warning(
            "Webhook %s pings groups, but Auth's Discord service is not installed",
            hook.name,
        )
        return []
    mentions = []
    for group in groups:
        try:
            role_id = _lookup_role_id(group)
        except Exception:  # noqa: BLE001 - one bad lookup must not drop the alert
            logger.warning(
                "Webhook %s: could not resolve a Discord role for group %r",
                hook.name,
                group.name,
                exc_info=True,
            )
            continue
        if role_id is None:
            logger.warning("Webhook %s: no Discord role named %r", hook.name, group.name)
            continue
        mentions.append(f"<@&{role_id}>")
    return mentions


def _content(hook):
    """The text in front of the embed: pings, or nothing."""
    parts = []
    if hook.ping_type == Webhook.PingType.HERE:
        parts.append("@here")
    elif hook.ping_type == Webhook.PingType.EVERYONE:
        parts.append("@everyone")
    parts.extend(_role_mentions(hook))
    return " ".join(parts) or None


# What became of one post to one webhook.
SENT = "sent"
REFUSED = "refused"  # Discord answered, and not with a success
SWITCHED_OFF = "switched off"  # Discord said the webhook is gone; see GONE
UNREACHABLE = "unreachable"  # no answer at all, or it broke on our side

def _is_network_error(exc) -> bool:
    """requests' errors, and urllib3's when one gets past requests.

    Their text carries the request URL, and a webhook URL is its own password,
    so these are logged by name alone. requests' own are OSErrors; urllib3's
    are known by their module, which spares importing two packages this app
    does not otherwise depend on.
    """
    return isinstance(exc, OSError) or type(exc).__module__.split(".")[0] in (
        "requests",
        "urllib3",
    )


def deliver(hook, embed: Embed) -> str:
    """Post one embed to one webhook, with that webhook's pings in front, and
    say what became of it.

    The single delivery step, shared by real alerts and the test button on
    the Webhooks page -- so the test proves the path a real alert takes.
    """
    try:
        response = DiscordWebhook(hook.url).execute(
            content=_content(hook), embeds=[embed], username="SOV Monitor"
        )
    except Exception as exc:  # noqa: BLE001 - one broken hook must not stop the rest
        if _is_network_error(exc):
            # Name the error, not the address.
            logger.error("Failed to post to webhook %s: %s", hook.name, type(exc).__name__)
        else:
            # Not the network, so not the URL in the text either -- and
            # without the traceback a bug here is a line nobody can act on.
            logger.exception("Failed to post to webhook %s", hook.name)
        return UNREACHABLE
    # dhooks-lite raises only for network trouble; a 404 from a deleted
    # webhook, a 401 or a 429 comes back as a response. Counting those as
    # delivered lost the alert for good -- it was recorded as sent -- and made
    # the Send test button report success for a dead channel.
    if not getattr(response, "status_ok", True):
        status = getattr(response, "status_code", None)
        if status in GONE and _switch_off(hook, status):
            return SWITCHED_OFF
        logger.warning("Webhook %s refused the post: HTTP %s", hook.name, status or "?")
        return REFUSED
    return SENT


def post_to(hook, embed: Embed) -> bool:
    """``deliver``, answering only whether Discord took it."""
    return deliver(hook, embed) == SENT


# Answers that mean the webhook will never work again as saved: deleted in
# Discord, or its token reset. 429 and 5xx are Discord being busy, and the
# next run is the retry.
GONE = {
    404: "Discord answered HTTP 404: the webhook was deleted in Discord",
    401: "Discord answered HTTP 401: the webhook token is no longer valid",
}


def _switch_off(hook, status) -> bool:
    """Stop posting to a webhook Discord says is gone, and record why.

    Retrying it every run filled the log with the same refusal and kept every
    alert routed to it pending. Switched off, it drops out of routing like any
    disabled webhook; the Webhooks page shows the reason until somebody fixes
    the URL and ticks Enabled again.

    False when the URL Discord refused is no longer the saved one: somebody
    put in a new one while the post was on its way, and the answer is about
    the old.
    """
    stranded = routes_left_without(hook)
    reason = GONE[status]
    now = timezone.now()
    # An update, not save(): an edit form saved at the same moment must not
    # be overwritten with this instance's stale fields. And only where the
    # URL is still the one that was refused, or a fix saved during the post
    # would be switched off with the old URL's reason.
    switched = Webhook.objects.filter(pk=hook.pk, url=hook.url).update(
        is_enabled=False, disabled_reason=reason, disabled_at=now
    )
    if not switched:
        return False
    hook.is_enabled, hook.disabled_reason, hook.disabled_at = False, reason, now
    logger.warning(
        "Webhook %s switched off: %s.%s",
        hook.name,
        reason,
        f" These alerts now have no working channel: {route_names(stranded)}."
        if stranded
        else "",
    )
    return True


NO_CHANNEL = "no channel: none configured, or the category or its section is switched off"


class Delivery:
    """What became of one alert, channel by channel.

    The alert checks need one answer -- did any channel take it -- and get it
    from ``_send``. The test paths need the rest: "no channel configured" for
    a channel Discord refused, or one it said is gone and that was just
    switched off, sent people looking in the wrong place.
    """

    def __init__(self, outcomes=(), problem=""):
        self.outcomes = list(outcomes)  # (webhook, outcome) pairs
        # Why nothing was posted at all, when it was not the routing.
        self.problem = problem

    @property
    def sent(self) -> bool:
        return any(outcome == SENT for _hook, outcome in self.outcomes)

    @property
    def failures(self) -> list:
        """One line per channel that did not take it, or why there was none."""
        if self.problem:
            return [self.problem]
        if not self.outcomes:
            return [NO_CHANNEL]
        return [
            describe_failure(hook, outcome) for hook, outcome in self.outcomes if outcome != SENT
        ]


def describe_failure(hook, outcome) -> str:
    if outcome == SWITCHED_OFF:
        return f"{hook.name}: switched off, {hook.disabled_reason}"
    if outcome == REFUSED:
        return f"{hook.name}: refused by Discord"
    return f"{hook.name}: Discord could not be reached"


def _dispatch(embed: Embed, category=None) -> Delivery:
    """Post to the channels configured for this category, and say how each
    one answered."""
    hooks = _webhooks_for(category)
    if not hooks:
        logger.info(
            "Alert raised but no channel is configured or enabled for %s: %s",
            category or "(uncategorised)",
            embed.title,
        )
        return Delivery()
    return Delivery((hook, deliver(hook, embed)) for hook in hooks)


def _send(embed: Embed, category=None) -> bool:
    """Post to the channels configured for this category.

    Callers must not record an alert as sent when this returns False, or a
    backlog of warnings would be silently swallowed while no webhook exists
    and never reappear once one is configured.
    """
    return _dispatch(embed, category).sent


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

    # Stamp keys, nothing to disarm: an inactive owner's hubs are simply not
    # looked at.
    for hub in SovHub.objects.filter(owner_id__in=_active_owner_ids()).select_related(
        "owner__corporation", "eve_solar_system"
    ):
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
    active = _active_owner_ids()
    for hub in SovHub.objects.select_related("owner__corporation", "eve_solar_system"):
        starved = [u for u in hub.upgrades.all() if u.power_state == PowerState.LOW]
        key = f"upglow:{hub.hub_id}"
        if hub.owner_id not in active or not starved:
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

    # Keys carry the window's start, so there is nothing to disarm here.
    candidates = Skyhook.objects.filter(
        owner_id__in=_active_owner_ids(),
        theft_start__isnull=False,
        theft_start__lte=horizon,
        theft_end__gte=now,
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
    active = _active_owner_ids()
    for skyhook in Skyhook.objects.select_related(
        "owner__corporation", "eve_planet", "eve_solar_system"
    ):
        key = f"reinf:{skyhook.skyhook_id}"
        if skyhook.owner_id not in active or not skyhook.is_under_attack:
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
    # Explicitly the tracked set, not whatever rows the last map update left:
    # the settings page can narrow it between two runs.
    tracked = tracked_alliance_ids()
    for system in SovSystem.objects.select_related("eve_solar_system"):
        adm = system.activity_defense_multiplier
        key = f"adm:{system.solar_system_id}"
        if system.alliance_id not in tracked or adm is None or adm >= threshold:
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

    "Ours" is the active owners' hubs, and no active hubs means nothing is
    ours -- not everything. This used to fall back to every IHUB campaign in
    a tracked system when the set was empty, which the Disable button turned
    into a one-click way to get an alert per campaign, hubs of corporations
    nobody ever registered included.
    """
    sent = 0
    ours = set(
        SovHub.objects.filter(owner_id__in=_active_owner_ids()).values_list(
            "hub_id", flat=True
        )
    )
    live = set()

    for campaign in SovCampaign.objects.filter(
        event_type=SovCampaign.EventType.IHUB
    ).select_related("eve_solar_system"):
        if campaign.structure_id not in ours:
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


def send_test(category) -> Delivery:
    """Post a realistic sample of one category down its configured route.

    The sample is built to the same shape as the genuine alert and filled from
    real rows where the database has any, so what this checks is whether a real
    alert would be readable in that channel -- not merely whether the webhook
    responds.
    """
    from .alert_samples import send_sample

    return send_sample(category)


def send_test_for_section(section) -> dict:
    """Fire one sample per category in a section. Returns a Delivery per
    category."""
    results = {}
    for category in AlertCategory:
        if CATEGORY_SECTIONS.get(category) != section:
            continue
        results[str(category)] = send_test(category)
    return results
