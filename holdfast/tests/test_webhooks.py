"""The Webhooks page.

A webhook could only be made in Django admin, and the people who run the
alliance's alerts are not Django admins. The page is gated on being a section
manager -- the same people who can already route alerts to a webhook -- and
its test button goes down the path a real alert takes, so a message arriving
proves the channel and not a shortcut.
"""

import itertools
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import Group, Permission, User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from ..core import alerts
from ..models import AlertCategory, AlertLog, AlertRoute, Webhook

URL = "https://discord.com/api/webhooks/1/x"

_character_ids = itertools.count(97_000_001)


def user_with(username, *codenames):
    user = User.objects.create_user(username)
    if codenames:
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="holdfast", codename__in=codenames
            )
        )
    # Auth turns a user without a main character away from every app page.
    character = EveCharacter.objects.create(
        character_id=next(_character_ids),
        character_name=f"Main of {username}",
        corporation_id=98000400,
        corporation_name="Webhook Corp",
        corporation_ticker="WHK",
    )
    CharacterOwnership.objects.create(
        user=user, character=character, owner_hash=f"main-{username}"
    )
    profile = user.profile
    profile.main_character = character
    profile.save()
    return User.objects.get(pk=user.pk)  # fresh: no permission cache


def notes_of(response):
    return [(m.level_tag, m.message) for m in get_messages(response.wsgi_request)]


class _Capture:
    """Stands in for dhooks_lite.Webhook and records what would be posted."""

    posts = []

    def __init__(self, url):
        self.url = url

    def execute(self, content=None, embeds=None, username=None):
        _Capture.posts.append((self.url, content, embeds))


class WebhookPageGateTests(TestCase):
    def test_any_section_manager_may_open_it(self):
        for codename in ("sov_manage", "skyhook_manage", "den_manage"):
            with self.subTest(codename=codename):
                self.client.force_login(user_with(f"manager_{codename}", codename))
                self.assertEqual(self.client.get(reverse("holdfast:webhooks")).status_code, 200)

    def test_nobody_else_may(self):
        """Not even the token manager: a webhook URL is a credential for a
        different thing, and routing alerts is a section manager's job."""
        for index, codenames in enumerate(
            (("basic_access",), ("den_member",), ("manage_owners",), ())
        ):
            with self.subTest(codenames=codenames):
                self.client.force_login(user_with(f"user_{index}", *codenames))
                self.assertEqual(self.client.get(reverse("holdfast:webhooks")).status_code, 403)


class WebhookFlowTests(TestCase):
    def setUp(self):
        self.client.force_login(user_with("manager", "sov_manage"))
        self.team = Group.objects.create(name="Skyhook Team")
        self.fc = Group.objects.create(name="FC")

    def test_adding_one(self):
        response = self.client.post(
            reverse("holdfast:webhook_add"),
            {
                "name": "ops",
                "url": URL,
                "is_enabled": "on",
                "ping_type": Webhook.PingType.HERE,
                "ping_groups": [self.team.pk, self.fc.pk],
                "notes": "pings the skyhook team",
            },
        )
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        hook = Webhook.objects.get(name="ops")
        self.assertEqual(hook.url, URL)
        self.assertTrue(hook.is_enabled)
        self.assertEqual(hook.ping_type, Webhook.PingType.HERE)
        self.assertCountEqual(hook.ping_groups.all(), [self.team, self.fc])
        self.assertEqual(notes_of(response)[0][0], "success")

    def test_a_bad_form_comes_back_with_its_errors_and_saves_nothing(self):
        response = self.client.post(reverse("holdfast:webhook_add"), {"name": "", "url": URL})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertFalse(Webhook.objects.exists())

    def test_a_duplicate_name_is_refused_on_the_form(self):
        Webhook.objects.create(name="ops", url=URL)
        response = self.client.post(reverse("holdfast:webhook_add"), {"name": "ops", "url": URL})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Webhook.objects.count(), 1)

    def test_the_groups_are_offered_by_name(self):
        response = self.client.get(reverse("holdfast:webhooks"))
        self.assertEqual(
            list(response.context["form"].fields["ping_groups"].queryset),
            [self.fc, self.team],
        )

    def test_editing_one_round_trips_the_groups(self):
        hook = Webhook.objects.create(name="ops", url=URL)
        hook.ping_groups.add(self.team)
        page = self.client.get(reverse("holdfast:webhook_edit", args=[hook.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'<option value="{self.team.pk}" selected>')
        self.assertNotContains(page, f'<option value="{self.fc.pk}" selected>')

        response = self.client.post(
            reverse("holdfast:webhook_edit", args=[hook.pk]),
            {
                "name": "ops renamed",
                "url": URL,
                "ping_type": Webhook.PingType.NONE,
                "ping_groups": [self.fc.pk],
                "notes": "",
            },
        )
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        hook.refresh_from_db()
        self.assertEqual(hook.name, "ops renamed")
        self.assertFalse(hook.is_enabled, "an unticked switch is off")
        self.assertEqual(list(hook.ping_groups.all()), [self.fc])

    def test_deleting_one(self):
        hook = Webhook.objects.create(name="ops", url=URL)
        response = self.client.post(reverse("holdfast:webhook_delete", args=[hook.pk]))
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        self.assertFalse(Webhook.objects.filter(pk=hook.pk).exists())

    def test_an_unknown_webhook_is_404_on_every_path(self):
        for name in ("webhook_edit", "webhook_delete", "webhook_test"):
            with self.subTest(name=name):
                response = self.client.post(reverse(f"holdfast:{name}", args=[9999]))
                self.assertEqual(response.status_code, 404)

    def test_the_mutating_paths_refuse_a_get(self):
        hook = Webhook.objects.create(name="ops", url=URL)
        for name, args in (
            ("webhook_add", []),
            ("webhook_delete", [hook.pk]),
            ("webhook_test", [hook.pk]),
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"holdfast:{name}", args=args))
                self.assertEqual(response.status_code, 405)

    def test_only_a_discord_webhook_url_is_accepted(self):
        """The page hands URL entry to section managers, and the server posts
        every alert -- system names, timers, pings -- to whatever is saved.
        Anything that is not Discord's webhook endpoint is refused, not
        routed: a typo and a hostile host look the same to requests.post."""
        for url in (
            "hello",
            "ftp://x/y",
            "javascript:alert(1)",
            "http://127.0.0.1:9/internal",
            "http://discord.com/api/webhooks/1/x",  # https only
            "https://discord.com.evil.example/api/webhooks/1/x",
            "https://discord.com/api/not-webhooks/1/x",
            "https://example.com/api/webhooks/1/x",
        ):
            with self.subTest(url=url):
                response = self.client.post(
                    reverse("holdfast:webhook_add"), {"name": "ops", "url": url}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("url", response.context["form"].errors)
                self.assertFalse(Webhook.objects.exists())

    def test_every_discord_host_is_accepted(self):
        for index, url in enumerate(
            (
                "https://discord.com/api/webhooks/1/x",
                "https://discordapp.com/api/webhooks/1/x",
                "https://canary.discord.com/api/webhooks/1/x",
                "https://ptb.discord.com/api/webhooks/1/x",
                "https://ptb.discordapp.com/api/webhooks/1/x",
                "  https://discord.com/api/webhooks/2/y  ",
            )
        ):
            with self.subTest(url=url):
                response = self.client.post(
                    reverse("holdfast:webhook_add"),
                    {"name": f"ops{index}", "url": url, "ping_type": Webhook.PingType.NONE},
                )
                self.assertRedirects(response, reverse("holdfast:webhooks"))
                self.assertEqual(Webhook.objects.get(name=f"ops{index}").url, url.strip())

    def test_editing_cannot_slip_a_bad_url_past_the_check(self):
        hook = Webhook.objects.create(name="ops", url=URL)
        response = self.client.post(
            reverse("holdfast:webhook_edit", args=[hook.pk]),
            {"name": "ops", "url": "http://127.0.0.1:9/internal"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("url", response.context["form"].errors)
        hook.refresh_from_db()
        self.assertEqual(hook.url, URL)

    def test_the_section_settings_pages_survive_deleting_a_routed_webhook(self):
        """Routing rows point at webhooks. Deleting one must drop it from the
        routes and leave every settings page renderable; where the category
        goes then is test_routing's business."""
        hook = Webhook.objects.create(name="ops", url=URL)
        route = AlertRoute.for_category(AlertCategory.SOV_FUEL)
        route.webhooks.set([hook])
        self.client.post(reverse("holdfast:webhook_delete", args=[hook.pk]))

        self.client.force_login(
            user_with("everything", "sov_manage", "skyhook_manage", "den_manage")
        )
        for name in ("sov_settings", "skyhook_settings", "den_settings"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"holdfast:{name}")).status_code, 200)
        self.assertEqual(route.webhooks.count(), 0)


class WebhookTestButtonTests(TestCase):
    def setUp(self):
        _Capture.posts = []
        self.client.force_login(user_with("manager", "den_manage"))
        self.hook = Webhook.objects.create(
            name="ops", url=URL, ping_type=Webhook.PingType.HERE
        )
        Webhook.objects.create(name="other", url="https://discord.com/api/webhooks/2/y")

    def _press(self):
        with mock.patch.object(alerts, "DiscordWebhook", _Capture):
            return self.client.post(reverse("holdfast:webhook_test", args=[self.hook.pk]))

    def test_it_posts_one_embed_to_that_webhook_alone_with_its_own_ping(self):
        response = self._press()
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        self.assertEqual(len(_Capture.posts), 1)
        url, content, embeds = _Capture.posts[0]
        self.assertEqual(url, URL)
        self.assertEqual(content, "@here")
        self.assertEqual(len(embeds), 1)
        self.assertEqual(notes_of(response)[0][0], "success")

    def test_it_writes_no_alert_log(self):
        """A test is not an alert: nothing may be recorded as sent, or the
        next real one could be swallowed as a repeat."""
        before = AlertLog.objects.count()
        self._press()
        self.assertEqual(AlertLog.objects.count(), before)

    def test_a_failing_post_is_reported_not_raised(self):
        class Broken:
            def __init__(self, url):
                pass

            def execute(self, **kwargs):
                raise RuntimeError("Discord said no")

        with (
            mock.patch.object(alerts, "DiscordWebhook", Broken),
            self.assertLogs(alerts.logger, level="ERROR"),
        ):
            response = self.client.post(reverse("holdfast:webhook_test", args=[self.hook.pk]))
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        self.assertEqual(notes_of(response)[0][0], "error")


SECRET_URL = (
    "https://discord.com/api/webhooks/123456789012345678/"
    "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-secretTOKENpart_wxyz"
)
TOKEN = SECRET_URL.rsplit("/", 1)[1]
MASKED = "https://discord.com/api/webhooks/1234…/…wxyz"


class WebhookUrlSecrecyTests(TestCase):
    """The URL is the webhook's password: whoever holds it can post to the
    channel as Holdfast. Any section manager manages webhooks, so a den
    manager could read the leadership channel's URL off the edit form. A
    saved URL now never goes back to a browser in full; leaving the field
    empty keeps it, and typing a new one replaces it."""

    def setUp(self):
        self.client.force_login(user_with("manager", "den_manage"))
        self.hook = Webhook.objects.create(name="leadership", url=SECRET_URL)

    def _edit(self, **data):
        return self.client.post(
            reverse("holdfast:webhook_edit", args=[self.hook.pk]),
            {"name": "leadership", "ping_type": Webhook.PingType.NONE, "is_enabled": "on", **data},
        )

    def test_the_mask_keeps_the_id_prefix_and_the_last_four_of_the_token(self):
        self.assertEqual(self.hook.masked_url, MASKED)

    def test_a_url_that_is_not_a_discord_webhook_shows_its_host_only(self):
        """One saved through Django admin before admin checked the URL."""
        self.assertEqual(
            Webhook(url="https://example.com/hooks/secret-path").masked_url,
            "https://example.com/…",
        )

    def test_a_short_token_shows_none_of_itself(self):
        self.assertEqual(
            Webhook(url="https://discord.com/api/webhooks/1/x").masked_url,
            "https://discord.com/api/webhooks/1…/…",
        )

    def test_neither_the_list_nor_the_edit_form_shows_the_token(self):
        for page in (
            reverse("holdfast:webhooks"),
            reverse("holdfast:webhook_edit", args=[self.hook.pk]),
        ):
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, TOKEN)
                self.assertNotContains(response, TOKEN[:-4])
                self.assertContains(response, MASKED)

    def test_saving_with_the_field_left_empty_keeps_the_stored_url(self):
        response = self._edit(url="", notes="renamed nothing, kept the URL")
        self.assertRedirects(response, reverse("holdfast:webhooks"))
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.url, SECRET_URL)
        self.assertEqual(self.hook.notes, "renamed nothing, kept the URL")

    def test_a_new_url_replaces_it(self):
        self._edit(url="https://discord.com/api/webhooks/2/new-token")
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.url, "https://discord.com/api/webhooks/2/new-token")

    def test_a_new_url_is_still_checked(self):
        response = self._edit(url="https://example.com/api/webhooks/2/y")
        self.assertIn("url", response.context["form"].errors)
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.url, SECRET_URL)

    def test_a_new_webhook_still_needs_one(self):
        response = self.client.post(reverse("holdfast:webhook_add"), {"name": "ops", "url": ""})
        self.assertIn("url", response.context["form"].errors)
        self.assertFalse(Webhook.objects.filter(name="ops").exists())


class WebhookAdminTests(TestCase):
    """Django admin used a plain form: no Discord check, and the URL in
    full. It uses the same rules as the Webhooks page now."""

    def setUp(self):
        from django.contrib import admin
        from django.test import RequestFactory

        self.root = User.objects.create_superuser("root", "root@example.invalid", "x")
        self.model_admin = admin.site._registry[Webhook]
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.root
        self.hook = Webhook.objects.create(name="leadership", url=SECRET_URL)

    def _form(self, instance=None, **data):
        form_class = self.model_admin.get_form(self.request, instance)
        if not data:
            return form_class(instance=instance)
        base = {"name": "ops", "ping_type": Webhook.PingType.NONE, "is_enabled": "on", "notes": ""}
        return form_class(data={**base, **data}, instance=instance)

    def test_admin_refuses_a_url_that_is_not_discord_s(self):
        form = self._form(url="http://127.0.0.1:9/internal")
        self.assertFalse(form.is_valid())
        self.assertIn("url", form.errors)

    def test_admin_accepts_a_discord_webhook(self):
        form = self._form(url="https://discord.com/api/webhooks/2/y")
        self.assertTrue(form.is_valid(), form.errors)

    def test_admin_never_renders_the_saved_url(self):
        rendered = str(self._form(instance=self.hook))
        self.assertNotIn(TOKEN, rendered)
        self.assertIn(MASKED, rendered)

    def test_admin_keeps_the_stored_url_when_the_field_is_left_empty(self):
        form = self._form(instance=self.hook, name="leadership", url="")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.url, SECRET_URL)
