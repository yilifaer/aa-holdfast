"""The Webhooks page: where Discord channels are added, changed and tried out.

Until now a webhook could only be made in Django admin, which the people who
run the alliance's alerts do not have. The URL is still a credential, so the
page is open to section managers only -- the same people who can already
route alerts to a webhook, and nobody else.
"""

from urllib.parse import urlsplit

from django import forms
from django.contrib import messages
from django.contrib.auth.models import Group
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..core.alert_samples import TEST_FOOTER
from ..core.alerts import COLOR_INFO, _embed, post_to
from ..models import Webhook
from .common import require_any

MANAGERS = ("sov_manage", "skyhook_manage", "den_manage")

# Where Discord serves webhooks: the canonical host, the old one, and the
# canary / PTB builds of either, which hand out URLs on their own hostnames.
DISCORD_HOSTS = frozenset(
    f"{prefix}{domain}"
    for domain in ("discord.com", "discordapp.com")
    for prefix in ("", "canary.", "ptb.")
)


class WebhookForm(forms.ModelForm):
    class Meta:
        model = Webhook
        fields = ("name", "url", "is_enabled", "ping_type", "ping_groups", "notes")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "url": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://discord.com/api/webhooks/...",
                }
            ),
            "is_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "ping_type": forms.Select(attrs={"class": "form-select"}),
            "ping_groups": forms.SelectMultiple(attrs={"class": "form-select", "size": 6}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # By name: the picker is read by a person looking for "Skyhook Team".
        self.fields["ping_groups"].queryset = Group.objects.order_by("name")

    def clean_url(self):
        # The server posts every alert to whatever is saved here, and this
        # page opens the field to more people than Django admin did. Only
        # Discord's own webhook endpoint will do; a typo and a hostile host
        # look the same to requests.post, so both are refused up front.
        url = self.cleaned_data["url"].strip()
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in DISCORD_HOSTS
            or not parts.path.startswith("/api/webhooks/")
        ):
            raise forms.ValidationError(
                "Not a Discord webhook URL. It looks like "
                "https://discord.com/api/webhooks/<id>/<token>, "
                "from Channel Settings > Integrations > Webhooks."
            )
        return url


def _page(request, form, editing=None):
    return render(
        request,
        "holdfast/webhooks.html",
        {
            "webhooks": Webhook.objects.prefetch_related("ping_groups").order_by("name"),
            "form": form,
            "editing": editing,
        },
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
        f"Added {hook.name}. Until a category is routed to it on a section's "
        "settings page, every alert goes there along with every other enabled webhook.",
    )
    return redirect("holdfast:webhooks")


@require_any(*MANAGERS)
def edit(request, pk):
    # Webhooks belong to the install, not to an alliance, so the scoped
    # queryset here is all of them -- as on the Tokens page.
    hook = get_object_or_404(Webhook.objects.all(), pk=pk)
    form = WebhookForm(request.POST or None, instance=hook)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {hook.name}.")
        return redirect("holdfast:webhooks")
    return _page(request, form, editing=hook)


@require_any(*MANAGERS)
@require_POST
def delete(request, pk):
    hook = get_object_or_404(Webhook.objects.all(), pk=pk)
    name = hook.name
    hook.delete()
    messages.success(
        request,
        f"Deleted {name}. A category that was routed only to it now goes to "
        "every enabled webhook.",
    )
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
    if post_to(hook, embed):
        messages.success(request, f"Posted a test to {hook.name}.")
    else:
        messages.error(
            request,
            f"Could not post to {hook.name}. Check the URL; the log has Discord's answer.",
        )
    return redirect("holdfast:webhooks")
