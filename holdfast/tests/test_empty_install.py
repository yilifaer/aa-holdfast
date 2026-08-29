"""Every page has to render on an install that has never synced anything.

This app was developed against a live alliance, where the database filled up
before most of the pages were written. That order hides a whole class of bug:
a page that reads ``skyhooks[0]``, a template that assumes a config row exists,
a tile that divides by a count. None of it shows up once there is data, and all
of it shows up on somebody else's first afternoon.

The test database starts empty and these tests add nothing to it beyond a user
to look at the pages with. If a page needs an owner, a config, a threshold or a
sync to have happened, this is where that shows.
"""

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.urls import NoReverseMatch, get_resolver, reverse

# A GET on a POST-only endpoint is a correct 405, not a failure.
ACCEPTABLE = (200, 302, 405)


class EmptyInstallTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("fresh", "fresh@example.invalid", "x")
        character = EveCharacter.objects.create(
            character_id=95000001,
            character_name="Fresh Install",
            corporation_id=98000099,
            corporation_name="Example Corporation",
            corporation_ticker="EXMP",
        )
        CharacterOwnership.objects.create(
            user=self.user, character=character, owner_hash="fresh-install-hash"
        )
        # Auth gates most of its pages on the user having a main character.
        profile = self.user.profile
        profile.main_character = character
        profile.save()
        self.factory = RequestFactory()

    def _urls(self):
        """Every holdfast page that can be reached without arguments."""
        resolver = get_resolver()
        namespace = resolver.namespace_dict["holdfast"][1]
        for name in sorted(
            key for key in namespace.reverse_dict.keys() if isinstance(key, str)
        ):
            try:
                yield name, reverse(f"holdfast:{name}")
            except NoReverseMatch:
                continue  # takes arguments; covered by the views' own tests

    def _render(self, url):
        resolver = get_resolver()
        match = resolver.resolve(url)
        request = self.factory.get(url)
        request.user = self.user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        response = match.func(request, *match.args, **match.kwargs)
        if hasattr(response, "render"):
            response.render()
        return response

    def test_every_page_renders_with_no_data_at_all(self):
        checked = 0
        for name, url in self._urls():
            with self.subTest(page=name, url=url):
                response = self._render(url)
                self.assertIn(
                    response.status_code,
                    ACCEPTABLE,
                    f"{name} ({url}) returned {response.status_code} on an empty install",
                )
                checked += 1
        self.assertGreater(checked, 10, "the URL walk found suspiciously few pages")

    def test_the_config_singleton_creates_itself(self):
        """Nothing seeds it, so every page that reads it must be able to."""
        from ..models import HoldfastConfig

        self.assertEqual(HoldfastConfig.objects.count(), 0)
        config = HoldfastConfig.get_solo()
        self.assertIsNotNone(config.pk)
        self.assertEqual(HoldfastConfig.get_solo().pk, config.pk)

    def test_alert_routes_appear_for_every_category(self):
        """A settings page listing only what somebody already touched is empty."""
        from ..models import AlertCategory, AlertRoute
        from ..views.common import routes_for_section

        self.assertEqual(AlertRoute.objects.count(), 0)
        sections = {"sov", "skyhook", "den"}
        seen = set()
        for section in sections:
            for route in routes_for_section(section):
                seen.add(route.category)
        self.assertEqual(seen, set(AlertCategory))

    def test_the_alert_checks_run_against_nothing(self):
        """The beat schedule starts firing before the first sync finishes."""
        from ..core.alerts import run_all_checks
        from ..core.den_alerts import run_den_checks

        self.assertIsInstance(run_all_checks(), dict)
        self.assertIsInstance(run_den_checks(), dict)

    def test_the_attention_counts_are_zero_not_broken(self):
        from ..core.attention import den_count, skyhook_count, sov_count

        for counter in (sov_count, skyhook_count, den_count):
            with self.subTest(counter=counter.__name__):
                self.assertEqual(counter(self.user), 0)
