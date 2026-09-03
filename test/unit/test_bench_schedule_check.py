# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The bench watchdog must notice a schedule decaying, not just one that stopped.

`bench-watchdog.yml` exists to catch the failure the nightly's own notify jobs
structurally cannot: the night that never runs. It asked one question -- "is the
newest scheduled run more than 26h old?" -- sampled whenever its own `41 */6`
cron fired.

That question has two hours of headroom against a nominal 24h cadence, and
GitHub's scheduled-event queue spends it. The nightly workflow's own comment
records a `:00` cron firing 78 minutes late, which is why it moved off the hour.
Runs then began landing 4.5-5h after the cron, roughly 25h apart -- inside 26h
at most sampling moments. So the watchdog reported green while the cadence
decayed, and on 2026-08-31, when no scheduled run fired at all, it ran at 22:59
and still reported green.

The quantity that actually degrades is the interval between consecutive runs,
and it does not depend on when the watchdog looks. These tests pin the three
signals apart from each other, because they mean different things and a
watchdog that conflates them is how the last one went quiet:

  gap    one missed night (a ~48h interval)
  stale  nothing is arriving at all -- the backstop, because if the cron is
         dead no second run ever arrives to form a gap
  lateness  the cadence slipping later against the declared cron each night,
            which is the leading indicator the old check could not see

The healthy cases matter as much as the alerting ones. A watchdog that cries
wolf gets muted, and a muted watchdog is the state this one was already in.
"""

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "bench_schedule_check.py"

_spec = importlib.util.spec_from_file_location("bench_schedule_check", TOOL)
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def run(hours_ago, event="schedule", status="completed"):
    return {
        "databaseId": int(hours_ago * 100),
        "status": status,
        "event": event,
        "createdAt": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
    }


def cadence(spacing_h, count=6, first_age_h=1.0):
    """`count` scheduled runs, `spacing_h` apart, newest `first_age_h` old."""
    return [run(first_age_h + i * spacing_h) for i in range(count)]


CRON = (10, 17)


def punctual(count=6, late_h=0.5):
    """Runs exactly 24h apart, each `late_h` after the cron. Newest first."""
    newest = NOW.replace(hour=CRON[0], minute=CRON[1], second=0, microsecond=0)
    newest += timedelta(hours=late_h)
    if newest > NOW:
        newest -= timedelta(days=1)
    return [
        {"databaseId": i, "status": "completed", "event": "schedule",
         "createdAt": (newest - timedelta(days=i)).isoformat().replace("+00:00", "Z")}
        for i in range(count)
    ]


class TestHealthy:
    def test_a_steady_24h_cadence_is_silent(self):
        assert bsc.check_schedule(punctual(), now=NOW, cron=CRON) == []

    def test_a_single_late_night_does_not_alarm(self):
        """One night in the queue is noise; the mean is what must move."""
        runs = punctual(count=6, late_h=0.5)
        # Push one night 4h past the cron, leaving the rest punctual.
        late = bsc.parse_created(runs[2]) + timedelta(hours=3.5)
        runs[2] = dict(runs[2],
                       createdAt=late.isoformat().replace("+00:00", "Z"))
        assert bsc.check_schedule(runs, now=NOW, cron=CRON) == []

    def test_manual_dispatches_are_ignored(self):
        """Dispatches share the workflow but say nothing about the cron."""
        runs = cadence(24.0) + [run(h, event="workflow_dispatch") for h in (2, 3, 4)]
        assert bsc.check_schedule(runs, now=NOW, cron=CRON) == []


class TestMissedNight:
    def test_a_skipped_night_is_caught_by_the_gap(self):
        runs = [run(1.0), run(1.0 + 48.0), run(1.0 + 72.0), run(1.0 + 96.0)]
        problems = bsc.check_schedule(runs, now=NOW, cron=CRON)
        assert any("48.0h gap" in p for p in problems), problems

    def test_the_gap_is_caught_even_when_the_newest_run_is_fresh(self):
        """The old check could not do this: a fresh newest run hid the gap."""
        runs = [run(0.5), run(0.5 + 48.0), run(0.5 + 72.0), run(0.5 + 96.0)]
        problems = bsc.check_schedule(runs, now=NOW, cron=CRON)
        assert problems, "a 48h gap must alarm regardless of sampling moment"
        assert not any("old" in p for p in problems), (
            "the newest run is 0.5h old; this must be reported as a gap, not "
            "as staleness"
        )


class TestStale:
    def test_nothing_arriving_at_all_is_caught_by_staleness(self):
        """A dead cron produces no new run, so gaps alone stay silent forever."""
        runs = cadence(24.0, count=6, first_age_h=40.0)
        problems = bsc.check_schedule(runs, now=NOW, cron=CRON)
        assert any("40.0h old" in p for p in problems), problems

    def test_no_scheduled_runs_at_all_is_reported(self):
        runs = [run(h, event="workflow_dispatch") for h in (1, 2, 3)]
        problems = bsc.check_schedule(runs, now=NOW, cron=CRON)
        assert len(problems) == 1
        assert "no scheduled nightly run" in problems[0]


class TestLateness:
    """The signal that carries the degradation, and the reason it is not spacing.

    Reported, never alerted: see WHY LATENESS IS REPORT-ONLY in the tool. The
    tests here assert the measurement still works; TestLatenessIsReportOnly
    below asserts it cannot page on its own.
    """

    def test_a_sustained_late_cadence_is_reported(self):
        """Runs landing ~5h after the cron, on time relative to each other."""
        runs = [run(1.0 + i * 24.0 - 4.6) for i in range(6)]
        warnings = bsc.check_lateness(runs, now=NOW, cron=CRON)
        assert any("degrading" in w for w in warnings), warnings

    def test_punctual_runs_are_silent(self):
        """Healthy nights land within about half an hour of the cron."""
        runs = punctual(count=6, late_h=0.5)
        assert bsc.check_schedule(runs, now=NOW, cron=CRON) == []
        assert bsc.check_lateness(runs, now=NOW, cron=CRON) == []

    def test_spacing_alone_would_have_missed_it(self):
        """The reason this check is lateness and not interval arithmetic.

        A schedule that slips a fixed amount and then holds is perfectly
        spaced -- every interval is exactly 24h -- while every run is hours
        late. An interval-based check is blind to this by construction.
        """
        runs = punctual(count=6, late_h=6.0)
        gaps = [g for g, _ in bsc.intervals_hours(bsc.scheduled_runs(runs))]
        assert all(abs(g - 24.0) < 0.01 for g in gaps), gaps
        warnings = bsc.check_lateness(runs, now=NOW, cron=CRON)
        assert any("degrading" in w for w in warnings), warnings

    def test_too_few_runs_to_average_reports_nothing(self):
        runs = punctual(count=2, late_h=6.0)
        assert bsc.check_lateness(runs, now=NOW, cron=CRON) == []


class TestLatenessIsReportOnly:
    """Lateness must never be the sole reason an issue gets filed.

    The regression this guards: lateness lived in the same `problems` list as
    gap and stale, and `main()` exits 1 on any non-empty `problems`. Since the
    queue delay is permanent and unfixable from this repository, that filed a
    new `bench-alert` issue roughly every night -- `bench_alert.sh` only ever
    searches for an OPEN issue, so the recovery job closing one guaranteed the
    next watchdog run created another.
    """

    def test_a_badly_late_but_otherwise_healthy_schedule_is_not_a_problem(self):
        """Every night present and evenly spaced, all of them hours late."""
        runs = punctual(count=6, late_h=9.0)
        assert bsc.check_schedule(runs, now=NOW, cron=CRON) == [], (
            "lateness alone must not reach the problems list -- that is what "
            "filed an issue every night"
        )
        assert bsc.check_lateness(runs, now=NOW, cron=CRON), (
            "...but it must still be measured and reported"
        )

    def test_lateness_does_not_suppress_a_real_problem(self):
        """A missed night still alarms while the schedule is also late."""
        runs = [run(1.0 + 9.0), run(1.0 + 9.0 + 48.0),
                run(1.0 + 9.0 + 72.0), run(1.0 + 9.0 + 96.0)]
        problems = bsc.check_schedule(runs, now=NOW, cron=CRON)
        assert any("gap" in p for p in problems), problems

    def test_the_cron_is_read_from_the_workflow_not_hardcoded(self):
        """A cron change must move this check with it, not silently diverge."""
        assert bsc.cron_hour_minute() == (10, 17)


class TestParsing:
    def test_the_rest_api_timestamp_field_is_accepted(self):
        """gh run list emits createdAt; the REST API emits created_at."""
        r = run(1.0)
        r["created_at"] = r.pop("createdAt")
        assert bsc.parse_created(r) == NOW - timedelta(hours=1.0)

    def test_a_run_with_no_timestamp_raises_rather_than_scoring_zero(self):
        with pytest.raises(ValueError):
            bsc.parse_created({"event": "schedule"})

    def test_runs_are_ordered_by_time_not_by_list_position(self):
        """Never assume the query returned them sorted."""
        runs = [run(49.0), run(1.0), run(25.0)]
        ages = [bsc.parse_created(r) for r in bsc.scheduled_runs(runs)]
        assert ages == sorted(ages, reverse=True)


class TestRunHealth:
    """The two checks that were already right; kept, and now covered."""

    def test_a_long_queued_run_means_the_runner_is_offline(self):
        runs = [run(4.0, status="queued")]
        problems = bsc.check_run_health(runs, now=NOW)
        assert any("QUEUED for 4.0h" in p for p in problems), problems

    def test_a_run_past_every_job_timeout_is_reported(self):
        runs = [run(6.0, status="in_progress")]
        problems = bsc.check_run_health(runs, now=NOW)
        assert any("RUNNING for 6.0h" in p for p in problems), problems

    def test_a_briefly_queued_run_is_normal(self):
        assert bsc.check_run_health([run(0.5, status="queued")], now=NOW) == []

    def test_completed_runs_are_never_reported_however_old(self):
        runs = [run(500.0, status="completed")]
        assert bsc.check_run_health(runs, now=NOW) == []

    def test_a_dispatched_run_can_still_be_stuck(self):
        """Run health is about the runner, so the trigger is irrelevant."""
        runs = [run(4.0, event="workflow_dispatch", status="queued")]
        assert bsc.check_run_health(runs, now=NOW) != []
