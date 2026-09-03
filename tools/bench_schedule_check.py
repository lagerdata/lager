#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Decide whether the nightly bench schedule is healthy, from its run history.

Split out of bench-watchdog.yml so the decision can be tested. The workflow
keeps the querying and the alerting; this file holds the arithmetic, which is
the part that was wrong.

WHAT WAS WRONG
--------------
The check asked "is the newest scheduled run more than 26h old?", evaluated
whenever the watchdog's own `41 */6` cron happened to fire. Nominal spacing is
24h, so that left two hours of headroom, sampled at four arbitrary offsets a
day -- and GitHub's scheduled-event queue spends it.

WHY LATENESS, NOT SPACING
-------------------------
Spacing looks like the obvious measure and is a poor one. Measured over the ten
scheduled runs to 2026-09-01, spacing barely moves while the schedule visibly
decays:

    interval spread   17.9h .. 33.6h, mean 24.47h   (nominal 24h)
    lateness vs cron  0.4h 0.5h 0.6h 0.6h  then  4.6h 4.8h 4.6h 7.3h 10.2h 10.9h

Spacing self-corrects: a night that lands 7h late shortens the *next* interval,
so a decaying schedule still averages ~24h. Lateness does not -- it is measured
against a fixed declared time and shows the regime change unmistakably, roughly
a 9x margin between healthy and degraded. It also fingers the right night: this
turned on 2026-08-27, four days before anyone noticed.

So spacing is kept only for the unambiguous case (a gap so large a night was
certainly skipped), and lateness carries the degradation signal.

WHAT IS CHECKED
---------------
  stale     The newest scheduled run is older than STALE_ALERT_HOURS. The
            backstop nothing else provides: if the cron is dead, no further run
            ever arrives, so no gap and no lateness sample is ever formed.
  gap       An interval above GAP_ALERT_HOURS within the recent lookback. Set
            well above nominal, because a merely late night is lateness, not a
            miss. Bounded by GAP_LOOKBACK_HOURS so a gap from last week stops
            alarming once the cadence recovers.
These are PROBLEMS: any one of them exits 1 and files a `bench-alert` issue.

WHAT IS WARNED ABOUT
--------------------
  lateness  Mean delay against the cron declared in nightly-bench.yml, above
            LATENESS_WARN_HOURS. This is the leading indicator: once lateness
            approaches half a day, "late" and "missed" are indistinguishable
            until the run either arrives or does not.

This is a WARNING: it is printed, written to warnings.txt, and carried in the
alert body whenever something else fires -- but it never exits 1 on its own.

WHY LATENESS IS REPORT-ONLY
---------------------------
The delay is GitHub's scheduled-event queue. Nothing in this repository can
bound it, and nightly-bench.yml already says so. A regime change on 2026-08-27
took the mean from ~0.5h to ~4.1h against a 3h threshold, and it has stayed
there.

Left as a problem, that is an issue filed every night, forever, for a condition
that will still be true tomorrow: `bench_alert.sh` searches only for an OPEN
issue, so the recovery job closing one on a green night guarantees the next
watchdog run files another. A `bench-alert` issue that is usually open for the
boring reason is one nobody reads on the night it is open for a real one --
which is the failure this file's own docstring set out to avoid.

Raising the threshold was the other option and is worse: it silences the signal
on exactly the nights it was built to catch, and re-mutes itself as the queue
degrades further. So the measurement is kept and the paging is dropped.

The cron is parsed from the workflow rather than duplicated here, so the two
cannot drift.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NIGHTLY = os.path.join(REPO_ROOT, ".github", "workflows", "nightly-bench.yml")

QUEUED_ALERT_HOURS = float(os.environ.get("QUEUED_ALERT_HOURS", "3"))
RUNNING_ALERT_HOURS = float(os.environ.get("RUNNING_ALERT_HOURS", "5"))
STALE_ALERT_HOURS = float(os.environ.get("SCHEDULE_STALE_ALERT_HOURS", "26"))
GAP_ALERT_HOURS = float(os.environ.get("SCHEDULE_GAP_ALERT_HOURS", "36"))
GAP_LOOKBACK_HOURS = float(os.environ.get("SCHEDULE_GAP_LOOKBACK_HOURS", "96"))
LATENESS_WARN_HOURS = float(os.environ.get("SCHEDULE_LATENESS_WARN_HOURS", "3"))

# Healthy nights land within an hour, so a mean over three or more samples is
# already a trend. Below that one queue hiccup would dominate.
MIN_RUNS_FOR_LATENESS = 3

_CRON_RE = re.compile(r'^\s*-\s*cron:\s*["\']?(\S+)\s+(\S+)\s+\S+\s+\S+\s+\S+["\']?',
                      re.MULTILINE)


def cron_hour_minute(path=NIGHTLY):
    """(hour, minute) UTC of the nightly cron, read from the workflow."""
    m = _CRON_RE.search(open(path).read())
    if not m:
        raise ValueError(f"no cron found in {path}")
    minute, hour = m.group(1), m.group(2)
    if not minute.isdigit() or not hour.isdigit():
        raise ValueError(f"cron is not a fixed daily time: {m.group(0).strip()!r}")
    return int(hour), int(minute)


def parse_created(run):
    """`createdAt` (gh run list) or `created_at` (REST) -> aware datetime."""
    raw = run.get("createdAt") or run.get("created_at")
    if not raw:
        raise ValueError(f"run has no creation timestamp: {run!r}")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def scheduled_runs(runs):
    """Scheduled runs only, newest first, ignoring manual dispatches."""
    return sorted(
        (r for r in runs if r.get("event") == "schedule"),
        key=parse_created,
        reverse=True,
    )


def lateness_hours(when, hour, minute):
    """Hours between the cron instant that should have fired this run and it."""
    fired = when.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if fired > when:
        fired -= timedelta(days=1)
    return (when - fired).total_seconds() / 3600


def intervals_hours(runs_desc):
    """(interval_hours, newer_run_datetime) per consecutive pair."""
    times = [parse_created(r) for r in runs_desc]
    return [
        ((times[i] - times[i + 1]).total_seconds() / 3600, times[i])
        for i in range(len(times) - 1)
    ]


def check_run_health(runs, now=None):
    """Runs stuck queued or running past every plausible budget."""
    now = now or datetime.now(timezone.utc)
    problems = []
    for r in runs:
        age = (now - parse_created(r)).total_seconds() / 3600
        if r.get("status") == "queued" and age > QUEUED_ALERT_HOURS:
            problems.append(
                f"run {r.get('databaseId')} has been QUEUED for {age:.1f}h - "
                f"is the bench runner offline?"
            )
        if r.get("status") == "in_progress" and age > RUNNING_ALERT_HOURS:
            problems.append(
                f"run {r.get('databaseId')} has been RUNNING for {age:.1f}h - "
                f"stuck past every job timeout"
            )
    return problems


def check_schedule(runs, now=None, cron=None):
    """Return a list of human-readable problems. Empty means healthy."""
    now = now or datetime.now(timezone.utc)
    hour, minute = cron if cron else cron_hour_minute()
    problems = []

    sched = scheduled_runs(runs)
    if not sched:
        return [
            f"no scheduled nightly run in the {len(runs)} run(s) examined - "
            f"is the schedule disabled?"
        ]

    newest = parse_created(sched[0])
    newest_age = (now - newest).total_seconds() / 3600
    if newest_age > STALE_ALERT_HOURS:
        problems.append(
            f"newest scheduled nightly is {newest_age:.1f}h old "
            f"(cron is {hour:02d}:{minute:02d} UTC daily) - is the schedule "
            f"disabled?"
        )

    recent = [
        (gap, newer) for gap, newer in intervals_hours(sched)
        if (now - newer).total_seconds() / 3600 <= GAP_LOOKBACK_HOURS
    ]
    if recent:
        worst, when = max(recent, key=lambda pair: pair[0])
        if worst > GAP_ALERT_HOURS:
            problems.append(
                f"a {worst:.1f}h gap between consecutive scheduled nightlies, "
                f"ending {when:%Y-%m-%d %H:%M} UTC (nominal is 24h) - a night "
                f"did not run"
            )

    return problems


def check_lateness(runs, now=None, cron=None):
    """Return warnings about schedule lateness. Never a problem on its own.

    Separate from :func:`check_schedule` because the response differs, not
    because the signal is weaker. Lateness is the earliest evidence the
    schedule is decaying, and it is worth having in front of whoever reads an
    alert -- "the night is 5h late" is what makes a missed night ambiguous.
    But it is a condition to know about, not an incident to page on, because
    no change here can affect it. See WHY LATENESS IS REPORT-ONLY above.
    """
    hour, minute = cron if cron else cron_hour_minute()
    sched = scheduled_runs(runs)
    if len(sched) < MIN_RUNS_FOR_LATENESS:
        return []

    delays = [lateness_hours(parse_created(r), hour, minute) for r in sched]
    mean_late = sum(delays) / len(delays)
    if mean_late <= LATENESS_WARN_HOURS:
        return []
    return [
        f"scheduled nightlies are starting {mean_late:.1f}h after the "
        f"{hour:02d}:{minute:02d} UTC cron on average over the last "
        f"{len(delays)} run(s), worst {max(delays):.1f}h - the schedule "
        f"is degrading, and at this delay a late night is "
        f"indistinguishable from a missed one"
    ]


def main():
    # Two queries, because they answer different questions. Run health needs
    # recent runs of every event; cadence needs scheduled runs only, filtered
    # server-side so a burst of manual dispatches cannot evict the history the
    # cadence check depends on.
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <all-runs.json> <scheduled-runs.json>",
              file=sys.stderr)
        return 2

    all_runs = json.loads(open(sys.argv[1]).read())
    scheduled = json.loads(open(sys.argv[2]).read())

    problems = check_run_health(all_runs) + check_schedule(scheduled)
    warnings = check_lateness(scheduled)

    # Written whether or not anything is wrong: the workflow puts this in the
    # run summary every time, and folds it into the alert body when a problem
    # does fire. A trend nobody is paged for still has to be visible somewhere.
    if warnings:
        print("WARNINGS (reported, not alerted):")
        for item in warnings:
            print(f"- {item}")
        with open("warnings.txt", "w") as f:
            f.write("\n".join(f"- {item}" for item in warnings))

    if not problems:
        print("Nightly bench runs are flowing normally.")
        return 0

    print("PROBLEMS:")
    for item in problems:
        print(f"- {item}")
    with open("problems.txt", "w") as f:
        f.write("\n".join(f"- {item}" for item in problems))
    return 1


if __name__ == "__main__":
    sys.exit(main())
