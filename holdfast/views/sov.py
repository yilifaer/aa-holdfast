"""Sovereignty section: fuel, ADM, timers, system cost, settings."""

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    HoldfastConfig,
    PowerState,
    Skyhook,
    SovCampaign,
    SovSystem,
    SystemCostIndex,
    Webhook,
)
from .common import (
    require_any,
    routes_for_section,
    save_routes,
    sov_can_manage,
    sov_can_view_all,
    visible_alliance_ids,
    visible_hubs,
)

SOV_ANY = ("sov_basic", "sov_officer", "sov_manage")
SOV_FULL = ("sov_officer", "sov_manage")
SOV_ADMIN = ("sov_manage",)


def _context(request, **extra):
    base = {
        "config": HoldfastConfig.get_solo(),
        "can_manage": sov_can_manage(request.user),
        "can_view_all": sov_can_view_all(request.user),
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@require_any(*SOV_FULL)
def dashboard(request):
    """What in sovereignty wants a human's attention, worst first."""
    config = HoldfastConfig.get_solo()
    user = request.user
    items = []

    # Fuel, in the same bands that colour the fuel page.
    for hub in visible_hubs(user):
        severity = config.fuel_severity(hub.hours_of_fuel_left)
        if severity:
            items.append(
                {
                    "severity": severity,
                    "when": hub.fuel_expires_at,
                    "kind": "Hub runs dry",
                    "where": hub.system_name,
                    "system": hub.system_name,
                    "detail": ", ".join(
                        f"{r.type_name} {r.amount:,}" for r in hub.reagents.all()
                    )
                    or "nothing burning",
                    "url": "holdfast:sov_fuel",
                }
            )

        starved = [u for u in hub.upgrades.all() if u.power_state == PowerState.LOW]
        if starved and config.notify_upgrade_offline:
            # If a den is siphoning workforce in this same system, say so --
            # that is a different problem with a different fix from "nobody
            # hauled fuel".
            siphoned = Skyhook.objects.filter(
                eve_solar_system_id=hub.solar_system_id,
                workforce_siphon_percent__isnull=False,
            ).exists()
            items.append(
                {
                    "severity": "danger",
                    "when": None,
                    "kind": f"{len(starved)} upgrade(s) unpowered",
                    "where": hub.system_name,
                    "system": hub.system_name,
                    "detail": (
                        ", ".join(u.type_name for u in starved)
                        + (
                            "  -- a den is siphoning workforce in this system"
                            if siphoned and config.notify_upgrade_den_caused
                            else ""
                        )
                    ),
                    "url": "holdfast:sov_fuel",
                }
            )

        if hub.is_overallocated:
            items.append(
                {
                    "severity": "warning",
                    "when": None,
                    "kind": "Hub over-allocated",
                    "where": hub.system_name,
                    "system": hub.system_name,
                    "detail": (
                        f"power {hub.power_allocated:,}/{hub.power_available:,}, "
                        f"workforce {hub.workforce_allocated:,}/{hub.workforce_available:,}"
                    ),
                    "url": "holdfast:sov_fuel",
                }
            )

    if config.adm_alert_threshold is not None:
        low = SovSystem.objects.filter(
            alliance_id__in=visible_alliance_ids(user),
            activity_defense_multiplier__lt=config.adm_alert_threshold,
        ).select_related("eve_solar_system")
        for system in low:
            items.append(
                {
                    "severity": "warning",
                    "when": None,
                    "kind": "ADM below threshold",
                    "where": system.system_name,
                    "system": system.system_name,
                    "detail": f"ADM {system.activity_defense_multiplier:.2f}",
                    "url": "holdfast:sov_adm",
                }
            )

    order = {"critical": 0, "danger": 1, "warning": 2, "info": 3}
    far = timezone.now() + timedelta(days=3650)
    items.sort(key=lambda i: (order.get(i["severity"], 9), i["when"] or far))

    hubs = list(visible_hubs(user))
    severities = [config.fuel_severity(h.hours_of_fuel_left) for h in hubs]
    return render(
        request,
        "holdfast/sov/dashboard.html",
        _context(
            request,
            items=items,
            item_count=len(items),
            critical_count=sum(1 for i in items if i["severity"] == "critical"),
            hub_count=len(hubs),
            fuel_danger=sum(1 for s in severities if s in ("danger", "critical")),
            fuel_warning=sum(1 for s in severities if s == "warning"),
            # Reinforced means a defence event exists, not "has a window".
            # Every hub has a vulnerability window every day, so counting those
            # reported all 51 systems as reinforced.
            reinforced_count=SovCampaign.objects.filter(
                event_type=SovCampaign.EventType.IHUB,
                solar_system_id__in=SovSystem.objects.filter(
                    alliance_id__in=visible_alliance_ids(user)
                ).values("solar_system_id"),
            ).count(),
            vulnerable_count=sum(
                1
                for hub in hubs
                if hub.vulnerability_start
                and hub.vulnerability_end
                and hub.vulnerability_start <= timezone.now() < hub.vulnerability_end
            ),
        ),
    )


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@require_any(*SOV_FULL)
def fuel(request):
    config = HoldfastConfig.get_solo()
    hubs = sorted(
        visible_hubs(request.user),
        key=lambda h: (h.fuel_expires_at is None, h.fuel_expires_at or timezone.now()),
    )
    rows = []
    for hub in hubs:
        hours = hub.hours_of_fuel_left
        # The bar is scaled against the amber band, so a full bar means
        # comfortably outside every threshold that would raise an alert.
        percent = 100
        if hours is not None and config.fuel_warning_days:
            percent = max(min(hours / 24 / config.fuel_warning_days * 100, 100), 2)
        rows.append(
            {"hub": hub, "severity": config.fuel_severity(hours), "percent": percent}
        )
    return render(request, "holdfast/sov/fuel.html", _context(request, rows=rows))


@require_any(*SOV_FULL)
def adm(request):
    config = HoldfastConfig.get_solo()
    systems = (
        SovSystem.objects.filter(alliance_id__in=visible_alliance_ids(request.user))
        .select_related("eve_solar_system")
        .order_by("activity_defense_multiplier")
    )
    return render(
        request,
        "holdfast/sov/adm.html",
        _context(request, systems=systems, threshold=config.adm_alert_threshold),
    )


@require_any(*SOV_FULL)
def timers(request):
    """Status of every system we hold, not just the ones on fire.

    Three states, each from a different source:

    * **reinforced** -- an Entosis campaign exists against the hub. The
      structure route has no state field, so a campaign is the only public
      evidence that somebody actually knocked it into a defence event.
    * **vulnerable** -- the daily window is open right now. Attackable, but
      nobody has necessarily turned up.
    * **safe** -- neither of those.
    """
    now = timezone.now()
    alliance_ids = visible_alliance_ids(request.user)

    campaigns = {
        campaign.solar_system_id: campaign
        for campaign in SovCampaign.objects.filter(
            event_type=SovCampaign.EventType.IHUB
        ).select_related("eve_solar_system")
    }
    hubs = {hub.solar_system_id: hub for hub in visible_hubs(request.user)}

    rows = []
    for system in SovSystem.objects.filter(
        alliance_id__in=alliance_ids
    ).select_related("eve_solar_system"):
        campaign = campaigns.get(system.solar_system_id)
        hub = hubs.get(system.solar_system_id)
        window_start = system.vulnerability_start
        window_end = system.vulnerability_end
        if hub and hub.vulnerability_start:
            window_start, window_end = hub.vulnerability_start, hub.vulnerability_end

        is_open = bool(window_start and window_end and window_start <= now < window_end)
        if campaign:
            status, severity = "reinforced", "critical"
        elif is_open:
            status, severity = "vulnerable", "warning"
        else:
            status, severity = "safe", "ok"

        rows.append(
            {
                "system": system.system_name,
                "status": status,
                "severity": severity,
                "window_start": window_start,
                "window_end": window_end,
                "campaign": campaign,
                "adm": system.activity_defense_multiplier,
                "corporation": hub.owner.corporation.corporation_name if hub else "",
                "is_capital": system.is_capital_system,
            }
        )

    weight = {"critical": 0, "warning": 1, "ok": 2}
    rows.sort(key=lambda r: (weight[r["severity"]], r["system"]))

    return render(
        request,
        "holdfast/sov/timers.html",
        _context(
            request,
            rows=rows,
            now=now,
            reinforced=sum(1 for r in rows if r["status"] == "reinforced"),
            vulnerable=sum(1 for r in rows if r["status"] == "vulnerable"),
            safe=sum(1 for r in rows if r["status"] == "safe"),
        ),
    )


@require_any(*SOV_ANY)
def system_cost(request):
    """The one sovereignty page an ordinary member can open."""
    rows = (
        SystemCostIndex.objects.filter(
            alliance_id__in=visible_alliance_ids(request.user)
        )
        .select_related("eve_solar_system__eve_constellation__eve_region")
        .order_by("manufacturing")
    )
    return render(
        request, "holdfast/sov/system_cost.html", _context(request, rows=rows)
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


FLOAT_FIELDS = (
    "fuel_warning_days",
    "fuel_danger_days",
    "fuel_critical_days",
)
BOOL_FIELDS = (
    "notify_upgrade_offline",
    "notify_upgrade_den_caused",
    "sov_discord_enabled",
)


@require_any(*SOV_ADMIN)
def settings_view(request):
    return render(
        request,
        "holdfast/sov/settings.html",
        _context(
            request,
            routes=routes_for_section("sov"),
            webhooks=Webhook.objects.all(),
        ),
    )


@require_any(*SOV_ADMIN)
@require_POST
def settings_save(request):
    config = HoldfastConfig.get_solo()
    errors = []

    for name in FLOAT_FIELDS:
        raw = request.POST.get(name, "").strip()
        try:
            value = float(raw)
        except ValueError:
            errors.append(f"{name}: '{raw}' is not a number")
            continue
        if value <= 0:
            errors.append(f"{name} must be greater than zero")
            continue
        setattr(config, name, value)

    raw = request.POST.get("adm_alert_threshold", "").strip()
    if raw == "":
        config.adm_alert_threshold = None
    else:
        try:
            config.adm_alert_threshold = float(raw)
        except ValueError:
            errors.append(f"ADM threshold: '{raw}' is not a number")

    for name in BOOL_FIELDS:
        setattr(config, name, request.POST.get(name) == "on")

    # Bands that cross over would make "critical" wider than "warning", which
    # reads as nonsense on the board and fires the wrong alert first.
    if not errors and not (
        config.fuel_critical_days < config.fuel_danger_days < config.fuel_warning_days
    ):
        errors.append("Bands must widen: critical < danger < warning")

    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("holdfast:sov_settings")

    save_routes(request, "sov")
    config.save()
    messages.success(request, "Sovereignty settings saved.")
    return redirect("holdfast:sov_settings")


@require_any(*SOV_ANY)
def home(request):
    """Landing page for the sidebar entry.

    Which page counts as "the front page" depends on the tier: an officer wants
    the dashboard, an ordinary member can only open the one public-facing page,
    so send each of them somewhere they are actually allowed to be.
    """
    if sov_can_view_all(request.user):
        return redirect("holdfast:sov_dashboard")
    return redirect("holdfast:sov_system_cost")


@require_any(*SOV_ADMIN)
@require_POST
def settings_test(request):
    """Fire one sample alert per category in this section.

    Uses the real delivery path, so a message arriving proves the routing --
    not that a test-only shortcut works.
    """
    from ..core.alerts import send_test_for_section

    results = send_test_for_section("sov")
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
    return redirect("holdfast:sov_settings")
