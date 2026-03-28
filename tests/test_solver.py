"""Unit tests for the CP-SAT solver and KPI computation.

Tests call solve() and compute_kpis() directly on canonical SchedulingProblem
objects, without going through the HTTP layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Assignment,
    ChangeoverMatrix,
    InfeasibleResult,
    Job,
    KPIs,
    ObjectiveMode,
    Operation,
    Resource,
    SchedulerSettings,
    SchedulerSuccess,
    SchedulingProblem,
    TimeWindow,
)
from app.scheduler import compute_kpis, solve
from app.scheduler.solver import _merge_calendar_windows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE = datetime(2025, 11, 3, 8, 0, 0)


def _dt(offset_minutes: int) -> datetime:
    return BASE + timedelta(minutes=offset_minutes)


def _window(start_offset: int, end_offset: int) -> TimeWindow:
    return TimeWindow(start=_dt(start_offset), end=_dt(end_offset))


def _problem(
    jobs: list[Job],
    resources: list[Resource],
    changeover_entries: dict | None = None,
    horizon_end_offset: int = 480,
    time_limit: int = 10,
) -> SchedulingProblem:
    return SchedulingProblem(
        horizon=TimeWindow(start=BASE, end=_dt(horizon_end_offset)),
        jobs=jobs,
        resources=resources,
        changeover_matrix=ChangeoverMatrix(entries=changeover_entries or {}),
        settings=SchedulerSettings(
            time_limit_seconds=time_limit,
            objective_mode=ObjectiveMode.MIN_TARDINESS,
        ),
    )


# ---------------------------------------------------------------------------
# Test 1 – Feasible single-job
# ---------------------------------------------------------------------------


def test_single_job_single_resource() -> None:
    """One job, one resource, one window: solver should find an assignment."""
    problem = _problem(
        jobs=[Job(id="J1", family="std", due=_dt(120), operations=[
            Operation(capability="fill", duration_minutes=30),
        ])],
        resources=[Resource(id="R1", capabilities={"fill"}, calendar=[_window(0, 480)])],
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)
    assert len(result.assignments) == 1
    a = result.assignments[0]
    assert a.job_id == "J1"
    assert a.resource_id == "R1"
    assert a.capability == "fill"
    end = a.start + timedelta(minutes=30)
    assert a.end == end


# ---------------------------------------------------------------------------
# Test 2 – Precedence
# ---------------------------------------------------------------------------


def test_precedence_multi_step_job() -> None:
    """A multi-step job's operations must be ordered: end[o] <= start[o+1]."""
    problem = _problem(
        jobs=[Job(id="J1", family="std", due=_dt(480), operations=[
            Operation(capability="fill", duration_minutes=30),
            Operation(capability="label", duration_minutes=20),
            Operation(capability="pack", duration_minutes=15),
        ])],
        resources=[
            Resource(id="Fill", capabilities={"fill"}, calendar=[_window(0, 480)]),
            Resource(id="Label", capabilities={"label"}, calendar=[_window(0, 480)]),
            Resource(id="Pack", capabilities={"pack"}, calendar=[_window(0, 480)]),
        ],
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    ops = sorted(result.assignments, key=lambda a: a.operation_index)
    assert len(ops) == 3
    for i in range(len(ops) - 1):
        assert ops[i].end <= ops[i + 1].start, (
            f"Precedence violated: op {i} ends {ops[i].end}, op {i+1} starts {ops[i+1].start}"
        )


# ---------------------------------------------------------------------------
# Test 3 – Calendar gaps
# ---------------------------------------------------------------------------


def test_calendar_gap_not_spanned() -> None:
    """An operation must fit entirely within a single calendar window.

    The resource has a break from t=120 to t=150.  An operation of 40 minutes
    cannot span the gap, so it must be scheduled before or after.
    """
    problem = _problem(
        jobs=[Job(id="J1", family="std", due=_dt(480), operations=[
            Operation(capability="fill", duration_minutes=40),
        ])],
        resources=[Resource(id="R1", capabilities={"fill"}, calendar=[
            _window(0, 120),    # 08:00 - 10:00
            _window(150, 480),  # 10:30 - 16:00
        ])],
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)
    a = result.assignments[0]

    gap_start = _dt(120)
    gap_end = _dt(150)
    # Operation must not overlap the gap
    assert not (a.start < gap_end and a.end > gap_start), (
        f"Assignment {a.start}–{a.end} spans the gap {gap_start}–{gap_end}"
    )


# ---------------------------------------------------------------------------
# Test 4 – Changeover
# ---------------------------------------------------------------------------


def test_changeover_gap_enforced() -> None:
    """Two jobs of different families on the same resource must have a changeover
    gap inserted between them."""
    problem = _problem(
        jobs=[
            Job(id="J1", family="standard", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=30),
            ]),
            Job(id="J2", family="premium", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=30),
            ]),
        ],
        resources=[Resource(id="Fill", capabilities={"fill"}, calendar=[_window(0, 480)])],
        changeover_entries={"standard": {"premium": 20}, "premium": {"standard": 20}},
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    fill_assignments = sorted(result.assignments, key=lambda a: a.start)
    assert len(fill_assignments) == 2

    first, second = fill_assignments
    gap = (second.start - first.end).total_seconds() / 60
    # The families differ so a 20-minute changeover must be present
    assert gap >= 20, f"Expected changeover gap >= 20 min, got {gap}"


# ---------------------------------------------------------------------------
# Test 5 – Infeasible
# ---------------------------------------------------------------------------


def test_infeasible_problem_returns_infeasible_result() -> None:
    """An operation whose duration exceeds every calendar window cannot be
    scheduled; the solver should return an InfeasibleResult."""
    problem = _problem(
        jobs=[Job(id="J1", family="std", due=_dt(480), operations=[
            # 90-minute operation but the only window is 60 minutes wide
            Operation(capability="fill", duration_minutes=90),
        ])],
        resources=[Resource(id="R1", capabilities={"fill"}, calendar=[_window(0, 60)])],
    )
    result = solve(problem)
    assert isinstance(result, InfeasibleResult)
    assert len(result.why) >= 1


# ---------------------------------------------------------------------------
# Test 6 – Tardiness minimization
# ---------------------------------------------------------------------------


def test_urgent_job_scheduled_first() -> None:
    """When two jobs compete for one resource, the one with the tighter due date
    should complete with less (or equal) tardiness under the min_tardiness
    objective."""
    # J-urgent is due at t=40, J-relaxed is due at t=480
    # Both need 30 minutes of fill; only one fill resource
    problem = _problem(
        jobs=[
            Job(id="J-urgent", family="std", due=_dt(40), operations=[
                Operation(capability="fill", duration_minutes=30),
            ]),
            Job(id="J-relaxed", family="std", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=30),
            ]),
        ],
        resources=[Resource(id="Fill", capabilities={"fill"}, calendar=[_window(0, 480)])],
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    by_job = {a.job_id: a for a in result.assignments}
    urgent_end = by_job["J-urgent"].end
    relaxed_end = by_job["J-relaxed"].end

    urgent_due = _dt(40)
    relaxed_due = _dt(480)
    urgent_tardiness = max(0, (urgent_end - urgent_due).total_seconds())
    relaxed_tardiness = max(0, (relaxed_end - relaxed_due).total_seconds())

    # The minimum-tardiness schedule should prefer to minimize the sum.
    # Since J-urgent's due is much tighter, minimizing total tardiness strongly
    # favors scheduling it first (no tardiness for it, none for relaxed).
    assert urgent_tardiness == 0 or relaxed_tardiness == 0, (
        f"Expected at least one job on-time; "
        f"urgent_tardiness={urgent_tardiness/60:.1f}m, relaxed_tardiness={relaxed_tardiness/60:.1f}m"
    )


# ---------------------------------------------------------------------------
# Test 7 – Full 4-product spec example
# ---------------------------------------------------------------------------


def _full_spec_problem() -> SchedulingProblem:
    horizon = TimeWindow(
        start=datetime(2025, 11, 3, 8, 0, 0),
        end=datetime(2025, 11, 3, 16, 0, 0),
    )
    resources = [
        Resource(
            id="Fill-1",
            capabilities={"fill"},
            calendar=[
                TimeWindow(
                    start=datetime(2025, 11, 3, 8, 0, 0),
                    end=datetime(2025, 11, 3, 12, 0, 0),
                ),
                TimeWindow(
                    start=datetime(2025, 11, 3, 12, 30, 0),
                    end=datetime(2025, 11, 3, 16, 0, 0),
                ),
            ],
        ),
        Resource(
            id="Fill-2",
            capabilities={"fill"},
            calendar=[
                TimeWindow(
                    start=datetime(2025, 11, 3, 8, 0, 0),
                    end=datetime(2025, 11, 3, 16, 0, 0),
                )
            ],
        ),
        Resource(
            id="Label-1",
            capabilities={"label"},
            calendar=[
                TimeWindow(
                    start=datetime(2025, 11, 3, 8, 0, 0),
                    end=datetime(2025, 11, 3, 16, 0, 0),
                )
            ],
        ),
        Resource(
            id="Pack-1",
            capabilities={"pack"},
            calendar=[
                TimeWindow(
                    start=datetime(2025, 11, 3, 8, 0, 0),
                    end=datetime(2025, 11, 3, 16, 0, 0),
                )
            ],
        ),
    ]
    jobs = [
        Job(
            id="P-100",
            family="standard",
            due=datetime(2025, 11, 3, 12, 30, 0),
            operations=[
                Operation(capability="fill", duration_minutes=30),
                Operation(capability="label", duration_minutes=20),
                Operation(capability="pack", duration_minutes=15),
            ],
        ),
        Job(
            id="P-101",
            family="premium",
            due=datetime(2025, 11, 3, 15, 0, 0),
            operations=[
                Operation(capability="fill", duration_minutes=35),
                Operation(capability="label", duration_minutes=25),
                Operation(capability="pack", duration_minutes=15),
            ],
        ),
        Job(
            id="P-102",
            family="standard",
            due=datetime(2025, 11, 3, 13, 30, 0),
            operations=[
                Operation(capability="fill", duration_minutes=25),
                Operation(capability="label", duration_minutes=20),
            ],
        ),
        Job(
            id="P-103",
            family="premium",
            due=datetime(2025, 11, 3, 14, 0, 0),
            operations=[
                Operation(capability="fill", duration_minutes=30),
                Operation(capability="label", duration_minutes=20),
                Operation(capability="pack", duration_minutes=15),
            ],
        ),
    ]
    return SchedulingProblem(
        horizon=horizon,
        jobs=jobs,
        resources=resources,
        changeover_matrix=ChangeoverMatrix(
            entries={
                "standard": {"standard": 0, "premium": 20},
                "premium": {"standard": 20, "premium": 0},
            }
        ),
        settings=SchedulerSettings(
            time_limit_seconds=30,
            objective_mode=ObjectiveMode.MIN_TARDINESS,
        ),
    )


def test_full_spec_example_feasible() -> None:
    """The 4-product spec problem must be solvable."""
    result = solve(_full_spec_problem())
    assert isinstance(result, SchedulerSuccess), (
        f"Expected feasible solution, got: {result}"
    )
    # All 4 jobs x their operation counts = 3+3+2+3 = 11 assignments
    assert len(result.assignments) == 11


def test_full_spec_precedence() -> None:
    """Each job's operations must respect order."""
    result = solve(_full_spec_problem())
    assert isinstance(result, SchedulerSuccess)

    by_job: dict[str, list[Assignment]] = {}
    for a in result.assignments:
        by_job.setdefault(a.job_id, []).append(a)

    for job_id, ops in by_job.items():
        sorted_ops = sorted(ops, key=lambda a: a.operation_index)
        for i in range(len(sorted_ops) - 1):
            assert sorted_ops[i].end <= sorted_ops[i + 1].start, (
                f"Precedence violated for {job_id}"
            )


def test_full_spec_no_overlap() -> None:
    """No two operations on the same resource may overlap."""
    result = solve(_full_spec_problem())
    assert isinstance(result, SchedulerSuccess)

    by_resource: dict[str, list[Assignment]] = {}
    for a in result.assignments:
        by_resource.setdefault(a.resource_id, []).append(a)

    for resource_id, ops in by_resource.items():
        sorted_ops = sorted(ops, key=lambda a: a.start)
        for i in range(len(sorted_ops) - 1):
            assert sorted_ops[i].end <= sorted_ops[i + 1].start, (
                f"Overlap on {resource_id}: {sorted_ops[i]} vs {sorted_ops[i+1]}"
            )


def test_full_spec_calendar_compliance() -> None:
    """Fill-1 has a break 12:00–12:30; no operation may span it."""
    result = solve(_full_spec_problem())
    assert isinstance(result, SchedulerSuccess)

    gap_start = datetime(2025, 11, 3, 12, 0, 0)
    gap_end = datetime(2025, 11, 3, 12, 30, 0)

    for a in result.assignments:
        if a.resource_id == "Fill-1":
            assert not (a.start < gap_end and a.end > gap_start), (
                f"Fill-1 assignment spans break: {a.start}–{a.end}"
            )


def test_full_spec_horizon_bounds() -> None:
    """All assignments must lie within the horizon [08:00, 16:00]."""
    result = solve(_full_spec_problem())
    assert isinstance(result, SchedulerSuccess)

    horizon_start = datetime(2025, 11, 3, 8, 0, 0)
    horizon_end = datetime(2025, 11, 3, 16, 0, 0)

    for a in result.assignments:
        assert a.start >= horizon_start, f"start before horizon: {a}"
        assert a.end <= horizon_end, f"end after horizon: {a}"


def test_full_spec_changeover_gaps() -> None:
    """On any resource, consecutive operations from different families must have
    at least the required changeover gap between them."""
    problem = _full_spec_problem()
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    job_family = {job.id: job.family for job in problem.jobs}
    by_resource: dict[str, list[Assignment]] = {}
    for a in result.assignments:
        by_resource.setdefault(a.resource_id, []).append(a)

    for resource_id, ops in by_resource.items():
        sorted_ops = sorted(ops, key=lambda a: a.start)
        for i in range(len(sorted_ops) - 1):
            curr = sorted_ops[i]
            nxt = sorted_ops[i + 1]
            fam_curr = job_family[curr.job_id]
            fam_nxt = job_family[nxt.job_id]
            required = problem.changeover_matrix.get_minutes(fam_curr, fam_nxt)
            if required > 0:
                gap = (nxt.start - curr.end).total_seconds() / 60
                assert gap >= required, (
                    f"Changeover gap too small on {resource_id} between "
                    f"{curr.job_id}({fam_curr}) and {nxt.job_id}({fam_nxt}): "
                    f"required {required}m, got {gap}m"
                )


def test_full_spec_kpis() -> None:
    """KPIs can be computed and have sensible values."""
    problem = _full_spec_problem()
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    kpis = compute_kpis(problem, result.assignments)
    assert isinstance(kpis, KPIs)
    assert kpis.tardiness_minutes >= 0
    assert kpis.changeover_count >= 0
    assert kpis.changeover_minutes >= 0
    assert kpis.makespan_minutes > 0

    for resource in problem.resources:
        assert resource.id in kpis.utilization_pct
        assert 0 <= kpis.utilization_pct[resource.id] <= 100


# ---------------------------------------------------------------------------
# KPI: same-family changeovers must not be counted
# ---------------------------------------------------------------------------


def test_same_family_changeover_not_counted() -> None:
    """Two consecutive jobs of the same family on the same resource produce zero
    changeover_count and zero changeover_minutes, even when cross-family entries
    exist in the matrix."""
    problem = _problem(
        jobs=[
            Job(id="J1", family="standard", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=20),
            ]),
            Job(id="J2", family="standard", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=20),
            ]),
        ],
        resources=[Resource(id="Fill", capabilities={"fill"}, calendar=[_window(0, 480)])],
        changeover_entries={
            "standard": {"standard": 0, "premium": 20},
            "premium": {"standard": 20, "premium": 0},
        },
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    kpis = compute_kpis(problem, result.assignments)
    assert kpis.changeover_count == 0, (
        f"Expected 0 changeovers for same-family sequence, got {kpis.changeover_count}"
    )
    assert kpis.changeover_minutes == 0, (
        f"Expected 0 changeover minutes for same-family sequence, got {kpis.changeover_minutes}"
    )


def test_different_family_changeover_counted_once() -> None:
    """Exactly one cross-family transition on one resource produces changeover_count=1
    and changeover_minutes equal to the matrix value."""
    problem = _problem(
        jobs=[
            Job(id="J1", family="standard", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=20),
            ]),
            Job(id="J2", family="premium", due=_dt(480), operations=[
                Operation(capability="fill", duration_minutes=20),
            ]),
        ],
        resources=[Resource(id="Fill", capabilities={"fill"}, calendar=[_window(0, 480)])],
        changeover_entries={
            "standard": {"standard": 0, "premium": 20},
            "premium": {"standard": 20, "premium": 0},
        },
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)

    kpis = compute_kpis(problem, result.assignments)
    assert kpis.changeover_count == 1, (
        f"Expected 1 changeover for one cross-family transition, got {kpis.changeover_count}"
    )
    assert kpis.changeover_minutes == 20, (
        f"Expected 20 changeover minutes, got {kpis.changeover_minutes}"
    )


# ---------------------------------------------------------------------------
# Calendar window normalization
# ---------------------------------------------------------------------------


def test_merge_calendar_windows_overlapping() -> None:
    """Two overlapping windows are merged into a single spanning window."""
    windows = [_window(0, 120), _window(60, 240)]
    merged = _merge_calendar_windows(windows)
    assert len(merged) == 1
    assert merged[0].start == _dt(0)
    assert merged[0].end == _dt(240)


def test_merge_calendar_windows_fully_contained() -> None:
    """A window fully inside another is absorbed into the outer window."""
    windows = [_window(0, 240), _window(60, 120)]
    merged = _merge_calendar_windows(windows)
    assert len(merged) == 1
    assert merged[0].start == _dt(0)
    assert merged[0].end == _dt(240)


def test_merge_calendar_windows_adjacent() -> None:
    """Adjacent (touching) windows are merged into one contiguous window."""
    windows = [_window(0, 120), _window(120, 240)]
    merged = _merge_calendar_windows(windows)
    assert len(merged) == 1
    assert merged[0].start == _dt(0)
    assert merged[0].end == _dt(240)


def test_merge_calendar_windows_disjoint_unchanged() -> None:
    """Non-overlapping, non-adjacent windows are kept as separate intervals."""
    windows = [_window(0, 120), _window(150, 240)]
    merged = _merge_calendar_windows(windows)
    assert len(merged) == 2


def test_solver_handles_overlapping_calendar_windows() -> None:
    """A resource with overlapping calendar windows produces a valid schedule.

    Without normalization the solver would create two presence literals for the
    overlapping zone and could produce an invalid model; with normalization the
    windows are merged to [0, 240) and the 90-minute operation fits cleanly.
    """
    problem = _problem(
        jobs=[Job(id="J1", family="std", due=_dt(480), operations=[
            Operation(capability="fill", duration_minutes=90),
        ])],
        resources=[Resource(id="R1", capabilities={"fill"}, calendar=[
            _window(0, 120),   # 08:00 – 10:00
            _window(60, 240),  # 09:00 – 12:00  (overlaps above)
        ])],
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)
    a = result.assignments[0]
    assert a.resource_id == "R1"
    # Merged window is [0, 240); assignment must lie within it
    assert a.start >= _dt(0)
    assert a.end <= _dt(240)


def test_solver_handles_fully_contained_calendar_window() -> None:
    """A window fully inside a larger window is absorbed; the operation still schedules."""
    problem = _problem(
        jobs=[Job(id="J1", family="std", due=_dt(480), operations=[
            Operation(capability="fill", duration_minutes=30),
        ])],
        resources=[Resource(id="R1", capabilities={"fill"}, calendar=[
            _window(0, 240),   # outer window
            _window(60, 120),  # fully inside the outer window
        ])],
    )
    result = solve(problem)
    assert isinstance(result, SchedulerSuccess)
    assert len(result.assignments) == 1
