"""The Webhooks page: where Discord channels are added, changed and tried out.

Until now a webhook could only be made in Django admin, which the people who
run the alliance's alerts do not have. The URL is still a credential, so the
page is open to section managers only -- the same people who can already
route alerts to a webhook, and nobody else -- and it never shows a saved URL
in full (see ``forms.WebhookUrlForm``).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..core.alert_samples import TEST_FOOTER
from ..core.alerts import (
    COLOR_INFO,
    REFUSED,
    SENT,
    SWITCHED_OFF,
    _embed,
    deliver,
    pin_routes_of,
    route_names,
    routes_left_without,
    stranded_routes,
)
from ..forms import WebhookForm
from ..models import Webhook
from .common import require_any

MANAGERS = ("sov_manage", "skyhook_manage", "den_manage")


def _page(request, form, editing=None):
    return render(
        request,
        "holdfast/webhooks.html",
        {
            "webhooks": Webhook.objects.prefetch_related("ping_groups").order_by("name"),
            "form": form,
            "editing": editing,
            "stranded": stranded_routes(),
        },
    )


def _warn_stranded(request, routes):
    """Say which alerts a delete or a switch-off has just cut off."""
    if routes:
        messages.warning(
            request,
            f"These alerts now have no working channel and go nowhere: "
            f"{route_names(routes)}. Route them to another webhook on their "
            "section's settings page.",
        )


@require_any(*MANAGERS)
def index(request):
    return _page(request, WebhookForm())


@require_any(*MANAGERS)
@require_POST
def add(request):
    form = WebhookForm(request.POST)
    if not form.is_valid():
        return _page(request, form)
    hook = form.save()
    messages.success(
        request,
        f"Added {hook.name}. Every category nobody has routed yet goes there, "
        "along with every other enabled webhook, until it is routed on its "
        "section's settings page.",
    )
    return redirect("holdfast:webhooks")


@require_any(*MANAGERS)
def edit(request, pk):
    # Webhooks belong to the install, not to an alliance, so the scoped
    # queryset here is all of them -- as on the Tokens page.
    hook = get_object_or_404(Webhook.objects.all(), pk=pk)
    # Asked before the form touches the instance: validating it writes the
    # posted values onto the hook, switch included.
    would_strand = routes_left_without(hook) if request.method == "POST" else []
    form = WebhookForm(request.POST or None, instance=hook)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {hook.name}.")
        if not hook.is_enabled:
            _warn_stranded(request, would_strand)
        return redirect("holdfast:webhooks")
    return _page(request, form, editing=hook)


@require_any(*MANAGERS)
@require_POST
def delete(request, pk):
    hook = get_object_or_404(Webhook.objects.all(), pk=pk)
    name = hook.name
    stranded = routes_left_without(hook)
    pin_routes_of(hook)
    hook.delete()
    messages.success(request, f"Deleted {name}.")
    _warn_stranded(request, stranded)
    return redirect("holdfast:webhooks")


@require_any(*MANAGERS)
@require_POST
def send_test(request, pk):
    """Post one sample to this webhook alone, pings included.

    The section settings pages test the routing; this tests the channel. It
    uses the same delivery step as a real alert, so what arrives is what an
    alert would look like there, and nothing is recorded as sent.
    """
    hook = get_object_or_404(Webhook.objects.all(), pk=pk)
    embed = _embed(
        title=f"Webhook test: {hook.name}",
        description=(
            "Holdfast can post to this channel. Which alerts arrive here is "
            "chosen on each section's settings page."
        ),
        color=COLOR_INFO,
        footer_note=TEST_FOOTER,
    )
    stranded = routes_left_without(hook)
    outcome = deliver(hook, embed)
    if outcome == SENT:
        messages.success(request, f"Posted a test to {hook.name}.")
    elif outcome == SWITCHED_OFF:
        # Discord says it is gone for good.
        messages.error(
            request,
            f"Could not post to {hook.name}. {hook.disabled_reason}, so it has "
            "been switched off. Put in a working URL and tick Enabled again.",
        )
        _warn_stranded(request, stranded)
    elif outcome == REFUSED:
        messages.error(
            request,
            f"Discord refused the post to {hook.name}. Check the URL; the log "
            "has Discord's answer.",
        )
    else:
        messages.error(
            request,
            f"Could not reach Discord to post to {hook.name}. The log says why.",
        )
    return redirect("holdfast:webhooks")
