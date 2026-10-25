"""holdfast_update is "sync now", so it runs what the schedule runs.

It used to stop at owners, the sov map, the raidable list and alerts. Den
operators, slots, workforce tracking, planet names, cost indices and
campaigns waited for beat, so the command documented as the first-run and
debugging sync left the den pages and the siphon checks empty.
"""

import io
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from .. import tasks
from ..core import alerts, den_sync, esi_sync
from ..management.commands import holdfast_update
from .test_operations import OperationFixture

ORDER = [
    "owner",
    "den character",
    "den slots",
    "workforce",
    "planets",
    "system costs",
    "campaigns",
    "sov map",
    "raidable",
    "alerts",
]

OWNER_STATS = {
    "sov_hubs": 1,
    "skyhooks": 2,
    "hub_details": 1,
    "skyhook_details": 2,
    "complete": True,
    "rate_limited": False,
}


class UpdateCommandTests(OperationFixture):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.owner = mock.Mock(pk=1)
        self.owner.__str__ = lambda _self: "Test Corp"

    def _record(self, name, value):
        def step(*_args, **_kwargs):
            self.calls.append(name)
            return value

        return step

    def _run(self, *args, den_step=None):
        owners = mock.Mock()
        owners.objects.active.return_value = [self.owner]
        owners.objects.filter.return_value = [self.owner]
        steps = [
            mock.patch.object(holdfast_update, "Owner", owners),
            mock.patch.object(holdfast_update, "inactive_reason", return_value=None),
            mock.patch.object(esi_sync, "update_owner", self._record("owner", OWNER_STATS)),
            mock.patch.object(
                den_sync,
                "update_den_character",
                den_step
                or self._record(
                    "den character",
                    {"dens": 1, "den_details": 1, "operations": 0, "new_events": 0},
                ),
            ),
            mock.patch.object(
                den_sync,
                "sync_den_slots",
                self._record("den slots", {"created": 0, "removed": 0, "total": 0}),
            ),
            mock.patch.object(
                den_sync,
                "track_workforce_high_water",
                self._record(
                    "workforce", {"peaks_raised": 0, "newly_dropped": 0, "recovered": 0}
                ),
            ),
            mock.patch.object(
                esi_sync,
                "resolve_pending_planets",
                self._record("planets", {"resolved": 0, "remaining": 0}),
            ),
            mock.patch.object(esi_sync, "update_system_costs", self._record("system costs", 0)),
            mock.patch.object(esi_sync, "update_campaigns", self._record("campaigns", 0)),
            mock.patch.object(esi_sync, "update_sov_systems", self._record("sov map", 0)),
            mock.patch.object(esi_sync, "refresh_raidable_skyhooks", self._record("raidable", [])),
            mock.patch.object(alerts, "run_all_checks", self._record("alerts", {})),
        ]
        out, err = io.StringIO(), io.StringIO()
        for step in steps:
            step.start()
        try:
            call_command("holdfast_update", *args, stdout=out, stderr=err)
        finally:
            for step in reversed(steps):
                step.stop()
        return out.getvalue(), err.getvalue()

    def test_it_runs_every_scheduled_job_in_the_schedules_order(self):
        self._run()
        self.assertEqual(self.calls, ORDER)

    def test_an_idle_den_character_is_synced_too(self):
        """The idle back-off spares the beat fan-out; "sync now" means now."""
        character = self.den.den_character
        character.idle_until = timezone.now() + timedelta(hours=3)
        character.save(update_fields=["idle_until"])
        self._run()
        self.assertIn("den character", self.calls)

    def test_a_switched_off_den_character_is_not(self):
        character = self.den.den_character
        character.is_enabled = False
        character.save(update_fields=["is_enabled"])
        self._run()
        self.assertNotIn("den character", self.calls)

    def test_the_flags_still_skip_their_parts(self):
        self._run("--skip-public", "--skip-alerts", "--skip-dens")
        self.assertEqual(self.calls, ["owner", "den slots", "workforce", "planets"])

    def test_a_failing_den_character_does_not_stop_the_rest(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("esi down")

        with self.assertLogs(tasks.logger, level="ERROR"):
            out, err = self._run(den_step=boom)
        self.assertIn("esi down", err)
        self.assertEqual(self.calls, [step for step in ORDER if step != "den character"])
