"""KPI computation from a solved schedule.

This is a pure function: it takes the original SchedulingProblem (for due dates,
calendar windows, and the changeover matrix) and the list of Assignments
produced by the solver, and returns a KPIs instance.

Keeping this separate from solver.py allows it to be unit-tested independently.
"""

from __future__ import annotations

from collections import defaultdict

from app.models import Assignment, KPIs, SchedulingProblem


def compute_kpis(problem: SchedulingProblem, assignments: list[Assignment]) -> KPIs:
    """Compute schedule quality KPIs from the solved assignments."""
    job_due = {job.id: job.due for job in problem.jobs}
    resource_calendars = {r.id: r.calendar for r in problem.resources}

    # --- Tardiness ---
    # For each job, find the latest completion time across all its assignments
    job_max_end: dict[str, object] = {}
    for a in assignments:
        current = job_max_end.get(a.job_id)
        if current is None or a.end > current:  # type: ignore[operator]
            job_max_end[a.job_id] = a.end

    tardiness_minutes = 0
    for job_id, completion in job_max_end.items():
        due = job_due.get(job_id)
        if due is not None and completion > due:  # type: ignore[operator]
            diff = (completion - due).total_seconds() / 60  # type: ignore[operator]
            tardiness_minutes += int(diff)

    # --- Changeovers ---
    # Group assignments by resource, sort by start time
    by_resource: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments:
        by_resource[a.resource_id].append(a)

    job_family = {job.id: job.family for job in problem.jobs}
    changeover_count = 0
    changeover_minutes = 0

    for resource_id, ops in by_resource.items():
        sorted_ops = sorted(ops, key=lambda a: a.start)
        for i in range(len(sorted_ops) - 1):
            curr = sorted_ops[i]
            nxt = sorted_ops[i + 1]
            fam_curr = job_family.get(curr.job_id, "")
            fam_nxt = job_family.get(nxt.job_id, "")
            minutes = problem.changeover_matrix.get_minutes(fam_curr, fam_nxt)
            if minutes > 0:
                changeover_count += 1
                changeover_minutes += minutes

    # --- Makespan ---
    if assignments:
        min_start = min(a.start for a in assignments)
        max_end = max(a.end for a in assignments)
        makespan_minutes = int((max_end - min_start).total_seconds() / 60)
    else:
        makespan_minutes = 0

    # --- Utilization ---
    # Per resource: busy minutes / total calendar minutes * 100
    resource_busy: dict[str, int] = defaultdict(int)
    for a in assignments:
        duration = int((a.end - a.start).total_seconds() / 60)
        resource_busy[a.resource_id] += duration

    utilization_pct: dict[str, int] = {}
    for resource in problem.resources:
        calendar_minutes = sum(w.duration_minutes for w in resource.calendar)
        if calendar_minutes > 0:
            busy = resource_busy.get(resource.id, 0)
            utilization_pct[resource.id] = round(busy * 100 / calendar_minutes)
        else:
            utilization_pct[resource.id] = 0

    return KPIs(
        tardiness_minutes=tardiness_minutes,
        changeover_count=changeover_count,
        changeover_minutes=changeover_minutes,
        makespan_minutes=makespan_minutes,
        utilization_pct=utilization_pct,
    )
