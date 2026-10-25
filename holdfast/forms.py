"""The webhook form, shared by the Webhooks page and Django admin.

One set of rules for both. Admin used to save any URL at all, which made the
page's Discord-only check a suggestion, and both showed a saved URL in full.
"""

from urllib.parse import urlsplit

from django import forms
from django.contrib.auth.models import Group

from .models import Webhook

# Where Discord serves webhooks: the canonical host, the old one, and the
# canary / PTB builds of either, which hand out URLs on their own hostnames.
DISCORD_HOSTS = frozenset(
    f"{prefix}{domain}"
    for domain in ("discord.com", "discordapp.com")
    for prefix in ("", "canary.", "ptb.")
)

NOT_DISCORD = (
    "Not a Discord webhook URL. It looks like "
    "https://discord.com/api/webhooks/<id>/<token>, "
    "from Channel Settings > Integrations > Webhooks."
)


def is_discord_webhook_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname in DISCORD_HOSTS
        and parts.path.startswith("/api/webhooks/")
    )


class WebhookUrlForm(forms.ModelForm):
    """The URL field's rules, for any form that saves a webhook.

    A saved URL is never sent back to the browser: the URL is the webhook's
    password, and every section manager manages every webhook, so the den
    manager would otherwise read the leadership channel's off its edit form.
    The field starts empty with the masked URL beside it; left empty, the
    stored URL stays, and a new one replaces it after the same check as a
    first one.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["url"]
        if self.instance.pk:
            self.initial["url"] = ""
            field.required = False
            field.widget.attrs["placeholder"] = self.instance.masked_url
            field.help_text = (
                f"Saved: {self.instance.masked_url}. Leave this empty to keep it, "
                "or paste a new URL to replace it."
            )

    def clean_url(self):
        # The server posts every alert to whatever is saved here. Only
        # Discord's own webhook endpoint will do; a typo and a hostile host
        # look the same to requests.post, so both are refused up front.
        url = (self.cleaned_data.get("url") or "").strip()
        if not url and self.instance.pk:
            return self.instance.url
        if not is_discord_webhook_url(url):
            raise forms.ValidationError(NOT_DISCORD)
        return url


class WebhookForm(WebhookUrlForm):
    """The Webhooks page's form."""

    class Meta:
        model = Webhook
        fields = ("name", "url", "is_enabled", "ping_type", "ping_groups", "notes")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "url": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://discord.com/api/webhooks/...",
                    "autocomplete": "off",
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
