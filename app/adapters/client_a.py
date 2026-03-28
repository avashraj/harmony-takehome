from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import (
    Assignment,
    ChangeoverMatrix,
    Job,
    ObjectiveMode,
    Operation,
    Resource,
    SchedulerSettings,
    SchedulingProblem,
    TimeWindow,
)


class ClientARouteStep(BaseModel):
    capability: str
    duration_minutes: int = Field(gt=0)


class ClientAProduct(BaseModel):
    id: str
    family: str
    due: datetime
    route: list[ClientARouteStep]


class ClientAResource(BaseModel):
    id: str
    capabilities: list[str]
    calendar: list[tuple[datetime, datetime]]


class ClientAChangeoverMatrixMinutes(BaseModel):
    values: dict[str, int]


class ClientASettings(BaseModel):
    time_limit_seconds: int = Field(default=30, gt=0)
    objective_mode: str = "min_tardiness"


class ClientARequest(BaseModel):
    horizon: TimeWindow
    resources: list[ClientAResource]
    changeover_matrix_minutes: ClientAChangeoverMatrixMinutes
    products: list[ClientAProduct]
    settings: ClientASettings


def format_assignment(assignment: Assignment) -> dict:
    """Format a canonical Assignment into the Client A response shape."""
    return {
        "product": assignment.job_id,
        "step_index": assignment.operation_index + 1,  # canonical is 0-based; Client A is 1-based
        "capability": assignment.capability,
        "resource": assignment.resource_id,
        "start": assignment.start.isoformat(),
        "end": assignment.end.isoformat(),
    }


def _parse_changeover_keys(flat: dict[str, int]) -> dict[str, dict[str, int]]:
    entries: dict[str, dict[str, int]] = {}

    for key, minutes in flat.items():
        parts = key.split("->")
        if len(parts) != 2:
            raise ValueError(f"invalid changeover key: {key!r}")

        from_family, to_family = parts[0].strip(), parts[1].strip()
        if not from_family or not to_family:
            raise ValueError(f"invalid changeover key: {key!r}")

        entries.setdefault(from_family, {})[to_family] = minutes

    return entries


def adapt(request: ClientARequest) -> SchedulingProblem:
    resources = [
        Resource(
            id=resource.id,
            capabilities=resource.capabilities,
            calendar=[
                TimeWindow(start=start, end=end) for start, end in resource.calendar
            ],
        )
        for resource in request.resources
    ]

    jobs = [
        Job(
            id=product.id,
            family=product.family,
            due=product.due,
            operations=[
                Operation(
                    capability=route_step.capability,
                    duration_minutes=route_step.duration_minutes,
                )
                for route_step in product.route
            ],
        )
        for product in request.products
    ]

    return SchedulingProblem(
        horizon=request.horizon,
        resources=resources,
        jobs=jobs,
        changeover_matrix=ChangeoverMatrix(
            entries=_parse_changeover_keys(request.changeover_matrix_minutes.values)
        ),
        settings=SchedulerSettings(
            time_limit_seconds=request.settings.time_limit_seconds,
            objective_mode=ObjectiveMode(request.settings.objective_mode),
        ),
    )
