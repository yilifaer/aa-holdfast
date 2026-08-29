"""Skyhook section: dashboard, stealable skyhooks, timers, raid targets."""

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from eveuniverse.models import EvePlanet, EveSolarSystem

from ..core.esi_sync import get_raidable_skyhooks
from ..models import HoldfastConfig, ReagentThreshold, Skyhook, Webhook
from .common import (
    require_any,
    routes_for_section,
    save_routes,
    skyhook_can_manage,
    skyhook_can_view_all,
    stealable_skyhooks,
)

SKYHOOK_ANY = ("skyhook_basic", "skyhook_officer", "skyhook_manage")
SKYHOOK_FULL = ("skyhook_officer", "skyhook_manage")
SKYHOOK_ADMIN = ("skyhook_manage",)


def _context(request, **extra):
    base = {
        "config": HoldfastConfig.get_solo(),
        "can_manage": skyhook_can_manage(request.user),
        "can_view_all": skyhook_can_view_all(request.user),
    }
    base.update(extra)
    return base


def _thresholds():
    return {r.type_id: r for r in ReagentThreshold.objects.select_related("eve_type")}


def _tripped(skyhook, rules):
    """Reagents on this skyhook that clear their own alerting bar."""
    hits = []
    for reagent in skyhook.reagents.all():
        rule = rules.get(reagent.type_id)
        if rule is None or not rule.is_enabled:
            continue
        if reagent.unsecured_stock >= rule.min_unsecured:
            hits.append((reagent, rule.min_unsecured))
    return hits


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@require_any(*SKYHOOK_FULL)
def dashboard(request):
    """Everything stealable inside the configured horizon, plus anything under attack."""
    config = HoldfastConfig.get_solo()
    hours = config.skyhook_theft_horizon_hours
    now = timezone.now()
    horizon = now + timedelta(hours=hours)
    rules = _thresholds()
    items = []
    upcoming = []

    for skyhook in stealable_skyhooks(request.user):
        if skyhook.is_under_attack:
            items.append(
                {
                    "severity": "critical",
                    "when": skyhook.reinforce_end,
                    "kind": skyhook.get_state_display(),
                    "where": skyhook.planet_name,
                    "system": skyhook.system_name,
                    "detail": "out of reinforcement"
                    if skyhook.reinforce_end
                    else "under attack",
                    "url": "holdfast:skyhook_list",
                }
            )

        if not skyhook.theft_start or skyhook.theft_start > horizon:
            continue
        if skyhook.theft_end and skyhook.theft_end < now:
            continue

        hits = _tripped(skyhook, rules)
        upcoming.append(
            {
                "skyhook": skyhook,
                "is_open": skyhook.is_theft_window_open,
                "tripped": hits,
                "total": skyhook.total_unsecured,
            }
        )
        if hits:
            items.append(
                {
                    "severity": "danger" if skyhook.is_theft_window_open else "warning",
                    "when": skyhook.theft_start,
                    "kind": "Lootable"
                    + (" now" if skyhook.is_theft_window_open else " soon"),
                    "where": skyhook.planet_name,
                    "system": skyhook.system_name,
                    "detail": ", ".join(
                        f"{r.type_name} {r.unsecured_stock:,}" for r, _ in hits
                    ),
                    "url": "holdfast:skyhook_list",
                }
            )

    upcoming.sort(key=lambda u: (not u["is_open"], u["skyhook"].theft_start or now))
    order = {"critical": 0, "danger": 1, "warning": 2, "info": 3}
    far = now + timedelta(days=3650)
    items.sort(key=lambda i: (order.get(i["severity"], 9), i["when"] or far))

    tracked = list(stealable_skyhooks(request.user))
    return render(
        request,
        "holdfast/skyhook/dashboard.html",
        _context(
            request,
            items=items,
            upcoming=upcoming,
            horizon_hours=hours,
            item_count=len(items),
            open_now=sum(1 for u in upcoming if u["is_open"]),
            tracked_count=len(tracked),
            attacked_count=sum(1 for s in tracked if s.is_under_attack),
            now=now,
        ),
    )


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@require_any(*SKYHOOK_FULL)
def skyhook_list(request):
    """Only skyhooks that hold something stealable.

    A workforce or power skyhook has no stock and no theft window, so listing
    it here is 350 rows of noise on a real alliance.
    """
    rows = sorted(
        stealable_skyhooks(request.user),
        key=lambda s: (s.theft_start is None, s.theft_start or timezone.now()),
    )
    return render(
        request,
        "holdfast/skyhook/list.html",
        _context(request, skyhooks=rows, now=timezone.now(), rules=_thresholds()),
    )


@require_any(*SKYHOOK_FULL)
def timers(request):
    """Only skyhooks currently in reinforcement."""
    now = timezone.now()
    entries = [
        {
            "when": skyhook.reinforce_end,
            "where": skyhook.planet_name,
            "system": skyhook.system_name,
            "state": skyhook.get_state_display(),
            "corporation": skyhook.owner.corporation.corporation_name,
        }
        for skyhook in stealable_skyhooks(request.user).filter(
            reinforce_end__gte=now
        )
    ]
    entries.sort(key=lambda e: e["when"])
    return render(
        request,
        "holdfast/skyhook/timers.html",
        _context(request, entries=entries, now=now),
    )


@require_any(*SKYHOOK_ANY)
def raid_targets(request):
    """The public raidable list, with real planet names instead of raw IDs."""
    rows = get_raidable_skyhooks()
    now = timezone.now()

    planet_ids = [r.get("planet_id") for r in rows if r.get("planet_id")]
    names = dict(
        EvePlanet.objects.filter(id__in=planet_ids).values_list("id", "name")
    )
    system_ids = {r.get("solar_system_id") for r in rows if r.get("solar_system_id")}
    systems = dict(
        EveSolarSystem.objects.filter(id__in=system_ids).values_list("id", "name")
    )
    ours = set(
        Skyhook.objects.filter(planet_id__in=planet_ids).values_list(
            "planet_id", flat=True
        )
    )

    entries = []
    unresolved = 0
    for row in rows:
        planet_id = row.get("planet_id")
        window = row.get("theft_vulnerability") or {}
        name = names.get(planet_id)
        if not name:
            unresolved += 1
        entries.append(
            {
                "planet": name or f"planet {planet_id}",
                "planet_id": planet_id,
                "is_named": bool(name),
                "system": systems.get(
                    row.get("solar_system_id"), row.get("solar_system_id")
                ),
                "start": window.get("start"),
                "end": window.get("end"),
                "is_open": bool(
                    window.get("start")
                    and window.get("end")
                    and window["start"] <= now < window["end"]
                ),
                "is_ours": planet_id in ours,
            }
        )
    entries.sort(key=lambda e: (e["start"] is None, e["start"] or now))
    return render(
        request,
        "holdfast/skyhook/raid_targets.html",
        _context(request, entries=entries, now=now, unresolved=unresolved),
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@require_any(*SKYHOOK_ADMIN)
def settings_view(request):
    return render(
        request,
        "holdfast/skyhook/settings.html",
        _context(
            request,
            thresholds=ReagentThreshold.objects.select_related("eve_type"),
            routes=routes_for_section("skyhook"),
            webhooks=Webhook.objects.all(),
        ),
    )


@require_any(*SKYHOOK_ADMIN)
@require_POST
def settings_save(request):
    config = HoldfastConfig.get_solo()
    errors = []

    raw = request.POST.get("skyhook_theft_horizon_hours", "").strip()
    try:
        hours = int(raw)
        if not 1 <= hours <= 168:
            raise ValueError
        config.skyhook_theft_horizon_hours = hours
    except ValueError:
        errors.append(f"Horizon: '{raw}' is not a whole number of hours from 1 to 168")

    raw = request.POST.get("skyhook_theft_lead_minutes", "").strip()
    try:
        minutes = int(raw)
        if minutes < 0:
            raise ValueError
        config.skyhook_theft_lead_minutes = minutes
    except ValueError:
        errors.append(f"Lead time: '{raw}' is not a whole number of minutes")

    config.skyhook_discord_enabled = request.POST.get("skyhook_discord_enabled") == "on"

    # One bar per reagent -- a hauler will cross the region for magmatic gas
    # and shrug at the same count of something cheap.
    for threshold in ReagentThreshold.objects.all():
        field = f"threshold_{threshold.type_id}"
        raw = request.POST.get(field, "").strip()
        if raw != "":
            try:
                value = int(raw.replace(",", ""))
                if value < 0:
                    raise ValueError
                threshold.min_unsecured = value
            except ValueError:
                errors.append(f"{threshold.type_name}: '{raw}' is not a whole number")
                continue
        threshold.is_enabled = request.POST.get(f"enabled_{threshold.type_id}") == "on"
        threshold.save(update_fields=["min_unsecured", "is_enabled"])

    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("holdfast:skyhook_settings")

    save_routes(request, "skyhook")
    config.save()
    messages.success(request, "Skyhook settings saved.")
    return redirect("holdfast:skyhook_settings")


@require_any(*SKYHOOK_ANY)
def home(request):
    """Landing page for the sidebar entry.

    Which page counts as "the front page" depends on the tier: an officer wants
    the dashboard, an ordinary member can only open the one public-facing page,
    so send each of them somewhere they are actually allowed to be.
    """
    if skyhook_can_view_all(request.user):
        return redirect("holdfast:skyhook_dashboard")
    return redirect("holdfast:skyhook_raid")


@require_any(*SKYHOOK_ADMIN)
@require_POST
def settings_test(request):
    """Fire one sample alert per category in this section.

    Uses the real delivery path, so a message arriving proves the routing --
    not that a test-only shortcut works.
    """
    from ..core.alerts import send_test_for_section

    results = send_test_for_section("skyhook")
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
    return redirect("holdfast:skyhook_settings")
