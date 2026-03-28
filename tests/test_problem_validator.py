"""Tests for app/validation/problem_validator.py.

Each test targets exactly one validation rule (or the happy path).
The _build_problem() helper returns a minimal but fully-valid
SchedulingProblem; individual tests then mutate it to trigger a rule.
"""

from __future__ import annotations

from datetime import datetime

from app.models import (
    ChangeoverMatrix,
    Job,
    Operation,
    Resource,
    SchedulerSettings,
    SchedulingProblem,
    TimeWindow,
    ValidationResult,
)
from app.validation import validate_problem


# ---------------------------------------------------------------------------
# Shared fixture builder
# ---------------------------------------------------------------------------

_HORIZON_START = datetime.fromisoformat("2025-11-03T08:00:00")
_HORIZON_END = datetime.fromisoformat("2025-11-03T16:00:00")
_HORIZON = TimeWindow(start=_HORIZON_START, end=_HORIZON_END)

# Single calendar window that spans the whole horizon (480 min)
_FULL_WINDOW = TimeWindow(start=_HORIZON_START, end=_HORIZON_END)


def _build_problem(**overrides: object) -> SchedulingProblem:
    """Return a minimal valid SchedulingProblem.

    Keyword overrides replace top-level fields so individual tests can
    inject invalid data without constructing an entire problem from scratch.
    """
    defaults: dict[str, object] = dict(
        horizon=_HORIZON,
        resources=[
            Resource(
                id="Fill-1",
                capabilities={"fill"},
                calendar=[_FULL_WINDOW],
            )
        ],
        jobs=[
            Job(
                id="P-100",
                family="standard",
                due=datetime.fromisoformat("2025-11-03T12:00:00"),
                operations=[Operation(capability="fill", duration_minutes=30)],
            )
        ],
        changeover_matrix=ChangeoverMatrix(
            entries={"standard": {"standard": 0}}
        ),
        settings=SchedulerSettings(),
    )
    defaults.update(overrides)
    return SchedulingProblem(**defaults)  # type: ignore[arg-type]


def _rules(result: ValidationResult) -> set[str]:
    return {issue.rule for issue in result.issues}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_problem_passes() -> None:
    result = validate_problem(_build_problem())
    assert result.is_valid
    assert result.issues == []


# ---------------------------------------------------------------------------
# Tier 1 – Structural / non-empty
# ---------------------------------------------------------------------------


def test_no_jobs() -> None:
    result = validate_problem(_build_problem(jobs=[]))
    assert "no_jobs" in _rules(result)


def test_no_resources() -> None:
    result = validate_problem(_build_problem(resources=[]))
    assert "no_resources" in _rules(result)


def test_job_no_operations() -> None:
    job = Job(
        id="P-100",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[],
    )
    result = validate_problem(_build_problem(jobs=[job]))
    assert "job_no_operations" in _rules(result)


# ---------------------------------------------------------------------------
# Tier 2 – Uniqueness
# ---------------------------------------------------------------------------


def test_duplicate_job_ids() -> None:
    job = Job(
        id="P-100",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[Operation(capability="fill", duration_minutes=30)],
    )
    result = validate_problem(_build_problem(jobs=[job, job]))
    assert "duplicate_job_ids" in _rules(result)


def test_duplicate_resource_ids() -> None:
    resource = Resource(id="Fill-1", capabilities={"fill"}, calendar=[_FULL_WINDOW])
    result = validate_problem(_build_problem(resources=[resource, resource]))
    assert "duplicate_resource_ids" in _rules(result)


# ---------------------------------------------------------------------------
# Tier 3 – Capability coverage
# ---------------------------------------------------------------------------


def test_orphan_capability() -> None:
    job = Job(
        id="P-100",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[Operation(capability="weld", duration_minutes=30)],
    )
    result = validate_problem(_build_problem(jobs=[job]))
    assert "orphan_capability" in _rules(result)
    assert any("weld" in issue.message for issue in result.issues)


# ---------------------------------------------------------------------------
# Tier 4 – Temporal / calendar
# ---------------------------------------------------------------------------


def test_resource_no_calendar_needed_by_job() -> None:
    # The resource provides "fill" which the job needs -- must be flagged.
    resource = Resource(id="Fill-1", capabilities={"fill"}, calendar=[])
    result = validate_problem(_build_problem(resources=[resource]))
    assert "resource_no_calendar" in _rules(result)


def test_resource_no_calendar_not_needed_by_any_job() -> None:
    # The resource provides "weld" but no job needs it -- must NOT be flagged.
    unneeded = Resource(id="Weld-1", capabilities={"weld"}, calendar=[])
    fill = Resource(id="Fill-1", capabilities={"fill"}, calendar=[_FULL_WINDOW])
    result = validate_problem(_build_problem(resources=[fill, unneeded]))
    assert "resource_no_calendar" not in _rules(result)


def test_operation_exceeds_windows() -> None:
    # Longest window is 60 min but the operation needs 120 min
    short_window = TimeWindow(
        start=_HORIZON_START,
        end=datetime.fromisoformat("2025-11-03T09:00:00"),
    )
    resource = Resource(id="Fill-1", capabilities={"fill"}, calendar=[short_window])
    job = Job(
        id="P-100",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[Operation(capability="fill", duration_minutes=120)],
    )
    result = validate_problem(_build_problem(resources=[resource], jobs=[job]))
    assert "operation_exceeds_windows" in _rules(result)


# ---------------------------------------------------------------------------
# Tier 5 – Changeover matrix
# ---------------------------------------------------------------------------


def test_negative_changeover() -> None:
    matrix = ChangeoverMatrix(entries={"standard": {"premium": -5}})
    result = validate_problem(_build_problem(changeover_matrix=matrix))
    assert "negative_changeover" in _rules(result)
    assert any("-5" in issue.message for issue in result.issues)


# ---------------------------------------------------------------------------
# Multiple issues collected in a single pass
# ---------------------------------------------------------------------------


def test_multiple_issues_all_returned() -> None:
    """A problem with several violations must surface all of them, not just the first."""
    job_no_ops = Job(
        id="P-100",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[],
    )
    # orphan capability: resource only has "fill", job needs "weld"
    job_orphan = Job(
        id="P-101",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[Operation(capability="weld", duration_minutes=30)],
    )
    matrix = ChangeoverMatrix(entries={"standard": {"standard": -1}})
    resource = Resource(id="Fill-1", capabilities={"fill"}, calendar=[_FULL_WINDOW])

    result = validate_problem(
        _build_problem(
            jobs=[job_no_ops, job_orphan],
            resources=[resource],
            changeover_matrix=matrix,
        )
    )

    found_rules = _rules(result)
    assert "job_no_operations" in found_rules
    assert "orphan_capability" in found_rules
    assert "negative_changeover" in found_rules
    assert len(result.issues) >= 3


# ---------------------------------------------------------------------------
# Tier 1 – Blank IDs
# ---------------------------------------------------------------------------


def test_empty_job_id_flagged() -> None:
    job = Job(
        id="",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[Operation(capability="fill", duration_minutes=30)],
    )
    result = validate_problem(_build_problem(jobs=[job]))
    assert "blank_job_id" in _rules(result)


def test_whitespace_only_job_id_flagged() -> None:
    job = Job(
        id="   ",
        family="standard",
        due=datetime.fromisoformat("2025-11-03T12:00:00"),
        operations=[Operation(capability="fill", duration_minutes=30)],
    )
    result = validate_problem(_build_problem(jobs=[job]))
    assert "blank_job_id" in _rules(result)


def test_empty_resource_id_flagged() -> None:
    resource = Resource(id="", capabilities={"fill"}, calendar=[_FULL_WINDOW])
    result = validate_problem(_build_problem(resources=[resource]))
    assert "blank_resource_id" in _rules(result)


def test_valid_ids_not_flagged() -> None:
    result = validate_problem(_build_problem())
    assert "blank_job_id" not in _rules(result)
    assert "blank_resource_id" not in _rules(result)
