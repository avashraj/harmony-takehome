"""CP-SAT solver for the Job Shop Scheduling problem.

The solver is structured around a SolverContext dataclass that carries the
CP-SAT model and all variable registries.  Hard constraints and objectives are
registered as standalone functions in _CONSTRAINTS and _OBJECTIVES respectively,

Adding a new constraint: write _add_foo(ctx: SolverContext) -> None, append to
_CONSTRAINTS.

Adding a new objective: write _objective_foo(ctx: SolverContext) -> None, add
one entry to _OBJECTIVES, add one value to ObjectiveMode enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ortools.sat.python import cp_model

from app.models import (
    Assignment,
    InfeasibleResult,
    ObjectiveMode,
    SchedulerSuccess,
    SchedulingProblem,
    TimeWindow,
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _to_minutes(dt: datetime, origin: datetime) -> int:
    """Convert a datetime to integer minutes relative to origin."""
    origin_aware = origin if origin.tzinfo is not None else origin.replace(tzinfo=timezone.utc)
    dt_aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return int((dt_aware - origin_aware).total_seconds() // 60)


def _from_minutes(minutes: int, origin: datetime) -> datetime:
    """Convert integer minutes back to a datetime relative to origin."""
    return origin + timedelta(minutes=minutes)


def _merge_calendar_windows(windows: list[TimeWindow]) -> list[TimeWindow]:
    """Merge overlapping or adjacent calendar windows into disjoint intervals.

    Sorts by start time, then sweeps forward extending the current merged window
    whenever the next window starts at or before the current end.
    """
    if not windows:
        return []
    sorted_windows = sorted(windows, key=lambda w: w.start)
    merged: list[TimeWindow] = [sorted_windows[0]]
    for window in sorted_windows[1:]:
        if window.start <= merged[-1].end:
            if window.end > merged[-1].end:
                merged[-1] = TimeWindow(start=merged[-1].start, end=window.end)
        else:
            merged.append(window)
    return merged


# ---------------------------------------------------------------------------
# SolverContext
# ---------------------------------------------------------------------------


@dataclass
class SolverContext:
    """Carries the CP-SAT model and all variable registries.

    Constraint and objective functions receive this context so they can read
    existing variables and add new ones without needing to access global state.
    """

    problem: SchedulingProblem
    model: cp_model.CpModel
    origin: datetime
    horizon: int  # total horizon in minutes

    # (job_id, op_index) -> IntVar
    start: dict[tuple[str, int], cp_model.IntVar] = field(default_factory=dict)
    end: dict[tuple[str, int], cp_model.IntVar] = field(default_factory=dict)

    # (job_id, op_index, resource_id, window_index) -> BoolVar
    presence: dict[tuple[str, int, str, int], cp_model.IntVar] = field(default_factory=dict)

    # (job_id, op_index, resource_id) -> BoolVar
    assign: dict[tuple[str, int, str], cp_model.IntVar] = field(default_factory=dict)

    # resource_id -> list of OptionalIntervalVar
    intervals: dict[str, list] = field(default_factory=dict)

    # job_id -> family string
    family: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Variable construction
# ---------------------------------------------------------------------------


def _build_variables(problem: SchedulingProblem) -> SolverContext:
    """Create the CP-SAT model and all decision variables."""
    origin = problem.horizon.start
    horizon = _to_minutes(problem.horizon.end, origin)
    model = cp_model.CpModel()

    ctx = SolverContext(
        problem=problem,
        model=model,
        origin=origin,
        horizon=horizon,
    )

    # Pre-build resource lookup: capability -> list of (resource, window_index pairs)
    # Calendar windows are merged first so overlapping inputs don't corrupt the model.
    resource_by_cap: dict[str, list[tuple]] = {}
    for resource in problem.resources:
        merged_calendar = _merge_calendar_windows(resource.calendar)
        for cap in resource.capabilities:
            resource_by_cap.setdefault(cap, [])
            for w_idx, window in enumerate(merged_calendar):
                resource_by_cap[cap].append((resource, w_idx, window))
        ctx.intervals.setdefault(resource.id, [])

    # Populate family lookup
    for job in problem.jobs:
        ctx.family[job.id] = job.family

    for job in problem.jobs:
        for op_idx, op in enumerate(job.operations):
            key = (job.id, op_idx)
            duration = op.duration_minutes

            s = model.NewIntVar(0, horizon, f"start_{job.id}_{op_idx}")
            e = model.NewIntVar(0, horizon, f"end_{job.id}_{op_idx}")
            model.Add(e == s + duration)
            ctx.start[key] = s
            ctx.end[key] = e

            # Presence literals for each eligible (resource, window) pair
            all_presence: list[cp_model.IntVar] = []
            eligible_resources: dict[str, list[cp_model.IntVar]] = {}

            for resource, w_idx, window in resource_by_cap.get(op.capability, []):
                win_start = _to_minutes(window.start, origin)
                win_end = _to_minutes(window.end, origin)

                # Skip windows too short for this operation
                if win_end - win_start < duration:
                    continue

                p_key = (job.id, op_idx, resource.id, w_idx)
                p = model.NewBoolVar(f"presence_{job.id}_{op_idx}_{resource.id}_{w_idx}")
                ctx.presence[p_key] = p

                # Calendar bounds enforced when this presence literal is true
                model.Add(s >= win_start).OnlyEnforceIf(p)
                model.Add(e <= win_end).OnlyEnforceIf(p)

                opt_interval = model.NewOptionalIntervalVar(
                    s, duration, e, p,
                    f"interval_{job.id}_{op_idx}_{resource.id}_{w_idx}",
                )
                ctx.intervals[resource.id].append(opt_interval)

                all_presence.append(p)
                eligible_resources.setdefault(resource.id, []).append(p)

            # Exactly one (resource, window) pair selected per operation
            model.AddExactlyOne(all_presence)

            # Derive per-resource assignment booleans
            for resource_id, plist in eligible_resources.items():
                a = model.NewBoolVar(f"assign_{job.id}_{op_idx}_{resource_id}")
                ctx.assign[(job.id, op_idx, resource_id)] = a
                model.Add(sum(plist) == a)

    return ctx


# ---------------------------------------------------------------------------
# Constraint functions
# ---------------------------------------------------------------------------


def _add_precedence(ctx: SolverContext) -> None:
    """Each job's operations must execute in order: end[o] <= start[o+1]."""
    for job in ctx.problem.jobs:
        for op_idx in range(len(job.operations) - 1):
            ctx.model.Add(
                ctx.end[(job.id, op_idx)] <= ctx.start[(job.id, op_idx + 1)]
            )


def _add_no_overlap(ctx: SolverContext) -> None:
    """Each resource can run at most one operation at a time."""
    for resource in ctx.problem.resources:
        intervals = ctx.intervals.get(resource.id, [])
        if intervals:
            ctx.model.AddNoOverlap(intervals)


def _add_changeovers(ctx: SolverContext) -> None:
    """Sequence-dependent changeover times between different job families.

    For each resource, for each pair of operations from different families that
    could both land on that resource, introduce an ordering boolean and enforce
    the changeover gap in both directions.
    """
    matrix = ctx.problem.changeover_matrix

    for resource in ctx.problem.resources:
        # Collect all (job_id, op_idx) operations that can run on this resource
        eligible: list[tuple[str, int]] = [
            (jid, oidx)
            for (jid, oidx, rid) in ctx.assign
            if rid == resource.id
        ]

        for i in range(len(eligible)):
            for j in range(i + 1, len(eligible)):
                a_job, a_op = eligible[i]
                b_job, b_op = eligible[j]

                fam_a = ctx.family[a_job]
                fam_b = ctx.family[b_job]

                changeover_ab = matrix.get_minutes(fam_a, fam_b)
                changeover_ba = matrix.get_minutes(fam_b, fam_a)

                # If no changeover is ever needed between these two, skip
                if changeover_ab == 0 and changeover_ba == 0:
                    continue

                a_on_r = ctx.assign.get((a_job, a_op, resource.id))
                b_on_r = ctx.assign.get((b_job, b_op, resource.id))
                if a_on_r is None or b_on_r is None:
                    continue

                # a_before_b: true => a runs before b on this resource
                a_before_b = ctx.model.NewBoolVar(
                    f"order_{a_job}_{a_op}_{b_job}_{b_op}_{resource.id}"
                )

                # If a before b and both on r: end[a] + changeover(a->b) <= start[b]
                ctx.model.Add(
                    ctx.end[(a_job, a_op)] + changeover_ab <= ctx.start[(b_job, b_op)]
                ).OnlyEnforceIf([a_on_r, b_on_r, a_before_b])

                # If b before a and both on r: end[b] + changeover(b->a) <= start[a]
                ctx.model.Add(
                    ctx.end[(b_job, b_op)] + changeover_ba <= ctx.start[(a_job, a_op)]
                ).OnlyEnforceIf([a_on_r, b_on_r, a_before_b.Not()])


_CONSTRAINTS: list = [
    _add_precedence,
    _add_no_overlap,
    _add_changeovers,
]


# ---------------------------------------------------------------------------
# Objective functions
# ---------------------------------------------------------------------------


def _objective_min_tardiness(ctx: SolverContext) -> None:
    """Minimize the sum of job tardiness (max(0, completion - due))."""
    tardiness_vars = []
    for job in ctx.problem.jobs:
        due_min = _to_minutes(job.due, ctx.origin)
        last_op_idx = len(job.operations) - 1
        last_end = ctx.end[(job.id, last_op_idx)]

        t = ctx.model.NewIntVar(0, ctx.horizon, f"tardiness_{job.id}")
        # tardiness = max(0, last_end - due)
        ctx.model.AddMaxEquality(t, [ctx.model.NewConstant(0), last_end - due_min])
        tardiness_vars.append(t)

    ctx.model.Minimize(sum(tardiness_vars))


_OBJECTIVES: dict = {
    ObjectiveMode.MIN_TARDINESS: _objective_min_tardiness,
}


def _set_objective(ctx: SolverContext) -> None:
    _OBJECTIVES[ctx.problem.settings.objective_mode](ctx)


# ---------------------------------------------------------------------------
# Solution extraction
# ---------------------------------------------------------------------------


def _extract_solution(
    ctx: SolverContext,
    solver: cp_model.CpSolver,
    status: int,
) -> SchedulerSuccess | InfeasibleResult:
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments: list[Assignment] = []
        for job in ctx.problem.jobs:
            for op_idx, op in enumerate(job.operations):
                s_val = solver.Value(ctx.start[(job.id, op_idx)])
                e_val = solver.Value(ctx.end[(job.id, op_idx)])

                # Find which resource was assigned
                resource_id = _find_assigned_resource(ctx, solver, job.id, op_idx)

                assignments.append(
                    Assignment(
                        job_id=job.id,
                        operation_index=op_idx,
                        capability=op.capability,
                        resource_id=resource_id,
                        start=_from_minutes(s_val, ctx.origin),
                        end=_from_minutes(e_val, ctx.origin),
                    )
                )
        return SchedulerSuccess(assignments=assignments)

    if status == cp_model.INFEASIBLE:
        return InfeasibleResult(
            why=["No feasible schedule exists within the given constraints."]
        )

    status_name = solver.StatusName(status)
    return InfeasibleResult(why=[f"Solver terminated without a solution: {status_name}."])


def _find_assigned_resource(
    ctx: SolverContext,
    solver: cp_model.CpSolver,
    job_id: str,
    op_idx: int,
) -> str:
    for resource in ctx.problem.resources:
        a_var = ctx.assign.get((job_id, op_idx, resource.id))
        if a_var is not None and solver.Value(a_var) == 1:
            return resource.id
    # Fallback: check presence literals directly
    for (jid, oidx, rid, _widx), p_var in ctx.presence.items():
        if jid == job_id and oidx == op_idx and solver.Value(p_var) == 1:
            return rid
    raise RuntimeError(f"No resource found for job={job_id} op={op_idx} in solution")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def solve(problem: SchedulingProblem) -> SchedulerSuccess | InfeasibleResult:
    """Build and solve the CP-SAT model for the given scheduling problem."""
    ctx = _build_variables(problem)

    for add_constraint in _CONSTRAINTS:
        add_constraint(ctx)

    _set_objective(ctx)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(problem.settings.time_limit_seconds)
    status = solver.Solve(ctx.model)

    return _extract_solution(ctx, solver, status)
