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


class ScheduleTests(TestCase):
    """The shipped schedule has to name tasks that exist.

    A schedule entry pointing at a task path that has been renamed does not
    fail at startup; Celery logs it once, at a level nobody is reading, and the
    feature is simply absent. This is the cheapest place to catch that.
    """

    def test_every_scheduled_task_can_be_imported(self):
        import importlib

        from ..schedule import CELERYBEAT_SCHEDULE

        for name, entry in CELERYBEAT_SCHEDULE.items():
            with self.subTest(entry=name):
                module_path, _, function = entry["task"].rpartition(".")
                module = importlib.import_module(module_path)
                self.assertTrue(
                    hasattr(module, function),
                    f"{name} points at {entry['task']}, which does not exist",
                )

    # Fanned out one row at a time by the "_all_" task above them, so they are
    # deliberately absent from the schedule. Listed here rather than inferred,
    # so that adding a task and forgetting to schedule it is a failure and not
    # a shrug.
    DISPATCHED_NOT_SCHEDULED = {"update_owner", "update_den_character"}

    def test_the_schedule_covers_every_task_the_app_defines(self):
        """A task nobody schedules is a feature nobody gets."""
        from .. import tasks
        from ..schedule import CELERYBEAT_SCHEDULE

        scheduled = {
            entry["task"].rpartition(".")[2] for entry in CELERYBEAT_SCHEDULE.values()
        }
        defined = {
            name
            for name in dir(tasks)
            if not name.startswith("_")
            and hasattr(getattr(tasks, name), "delay")  # a Celery task
        }
        self.assertEqual(
            defined - scheduled - self.DISPATCHED_NOT_SCHEDULED,
            set(),
            "these tasks exist but nothing in the shipped schedule runs them",
        )

    def test_the_dispatched_tasks_really_are_dispatched(self):
        """Otherwise the exemption above turns into a place to hide things."""
        import inspect

        from .. import tasks

        source = inspect.getsource(tasks)
        for name in self.DISPATCHED_NOT_SCHEDULED:
            with self.subTest(task=name):
                # Either dispatch form counts. These go out through
                # apply_async so each row can carry its own countdown.
                dispatched = f"{name}.delay(" in source or f"{name}.apply_async(" in source
                self.assertTrue(dispatched, f"{name} is exempt but never dispatched")

    def test_entry_names_are_prefixed_so_they_cannot_collide(self):
        from ..schedule import CELERYBEAT_SCHEDULE

        for name in CELERYBEAT_SCHEDULE:
            self.assertTrue(name.startswith("holdfast_"), name)
