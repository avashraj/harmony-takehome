from __future__ import annotations

from app.models import (
    SchedulingProblem,
    ValidationIssue,
    ValidationResult,
)


def _issue(rule: str, message: str) -> ValidationIssue:
    return ValidationIssue(rule=rule, message=message)


# ---------------------------------------------------------------------------
# Tier 1 – Structural / non-empty
# ---------------------------------------------------------------------------


def _check_no_jobs(problem: SchedulingProblem) -> list[ValidationIssue]:
    if not problem.jobs:
        return [_issue("no_jobs", "Problem contains no jobs; nothing to schedule.")]
    return []


def _check_no_resources(problem: SchedulingProblem) -> list[ValidationIssue]:
    if problem.jobs and not problem.resources:
        return [_issue("no_resources", "Problem contains jobs but no resources; nowhere to schedule.")]
    return []


def _check_job_no_operations(problem: SchedulingProblem) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for job in problem.jobs:
        if not job.operations:
            issues.append(
                _issue(
                    "job_no_operations",
                    f"Job '{job.id}' has no operations.",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Tier 2 – Uniqueness
# ---------------------------------------------------------------------------


def _check_duplicate_job_ids(problem: SchedulingProblem) -> list[ValidationIssue]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for job in problem.jobs:
        if job.id in seen:
            duplicates.add(job.id)
        seen.add(job.id)
    return [
        _issue("duplicate_job_ids", f"Duplicate job id: '{jid}'.")
        for jid in sorted(duplicates)
    ]


def _check_duplicate_resource_ids(problem: SchedulingProblem) -> list[ValidationIssue]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for resource in problem.resources:
        if resource.id in seen:
            duplicates.add(resource.id)
        seen.add(resource.id)
    return [
        _issue("duplicate_resource_ids", f"Duplicate resource id: '{rid}'.")
        for rid in sorted(duplicates)
    ]


# ---------------------------------------------------------------------------
# Tier 3 – Capability coverage
# ---------------------------------------------------------------------------


def _check_orphan_capability(problem: SchedulingProblem) -> list[ValidationIssue]:
    provided: set[str] = {cap for r in problem.resources for cap in r.capabilities}
    required: set[str] = {op.capability for job in problem.jobs for op in job.operations}
    orphans = required - provided
    return [
        _issue(
            "orphan_capability",
            f"Capability '{cap}' is required by at least one operation but no resource provides it.",
        )
        for cap in sorted(orphans)
    ]


# ---------------------------------------------------------------------------
# Tier 4 – Temporal / calendar
# ---------------------------------------------------------------------------


def _check_resource_no_calendar(problem: SchedulingProblem) -> list[ValidationIssue]:
    """Flag resources that are needed by at least one job operation but have no
    calendar windows, making them permanently unavailable."""
    required_capabilities: set[str] = {
        op.capability for job in problem.jobs for op in job.operations
    }
    issues: list[ValidationIssue] = []
    for resource in problem.resources:
        if resource.capabilities & required_capabilities and not resource.calendar:
            issues.append(
                _issue(
                    "resource_no_calendar",
                    f"Resource '{resource.id}' is needed by at least one job operation "
                    f"but has an empty calendar and is never available.",
                )
            )
    return issues


def _check_operation_exceeds_windows(problem: SchedulingProblem) -> list[ValidationIssue]:
    """Flag any operation whose duration exceeds the longest available calendar
    window across all resources that provide the required capability.  Such an
    operation can never be assigned to a single contiguous slot."""
    capable_windows: dict[str, int] = {}
    for resource in problem.resources:
        for cap in resource.capabilities:
            for window in resource.calendar:
                capable_windows[cap] = max(
                    capable_windows.get(cap, 0), window.duration_minutes
                )

    issues: list[ValidationIssue] = []
    for job in problem.jobs:
        for idx, op in enumerate(job.operations):
            longest = capable_windows.get(op.capability, 0)
            if op.duration_minutes > longest:
                issues.append(
                    _issue(
                        "operation_exceeds_windows",
                        f"Job '{job.id}' operation {idx} (capability '{op.capability}', "
                        f"{op.duration_minutes} min) exceeds the longest available calendar "
                        f"window for that capability ({longest} min); it cannot be scheduled.",
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# Tier 5 – Changeover matrix
# ---------------------------------------------------------------------------


def _check_negative_changeover(problem: SchedulingProblem) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for from_family, targets in problem.changeover_matrix.entries.items():
        for to_family, minutes in targets.items():
            if minutes < 0:
                issues.append(
                    _issue(
                        "negative_changeover",
                        f"Changeover from '{from_family}' to '{to_family}' is {minutes} min "
                        f"(must be non-negative).",
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_RULES = [
    _check_no_jobs,
    _check_no_resources,
    _check_job_no_operations,
    _check_duplicate_job_ids,
    _check_duplicate_resource_ids,
    _check_orphan_capability,
    _check_resource_no_calendar,
    _check_operation_exceeds_windows,
    _check_negative_changeover,
]


def validate_problem(problem: SchedulingProblem) -> ValidationResult:
    """Run all validation rules against *problem* and return the aggregated result.

    All rules are always evaluated so callers receive a complete list of issues
    rather than only the first one encountered.
    """
    issues: list[ValidationIssue] = []
    for rule_fn in _RULES:
        issues.extend(rule_fn(problem))
    return ValidationResult(issues=issues)
