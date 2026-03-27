from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TypeAlias

from pydantic import BaseModel, Field, model_validator


class TimeWindow(BaseModel):
    """A contiguous time interval [start, end)."""

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_start_before_end(self) -> "TimeWindow":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class ObjectiveMode(str, Enum):
    MIN_TARDINESS = "min_tardiness"


class Operation(BaseModel):
    capability: str
    duration_minutes: int = Field(gt=0)


class Job(BaseModel):
    id: str
    family: str
    due: datetime
    operations: list[Operation]


class Resource(BaseModel):
    id: str
    capabilities: set[str]
    calendar: list[TimeWindow]


class ChangeoverMatrix(BaseModel):
    entries: dict[str, dict[str, int]]

    def get_minutes(self, from_family: str, to_family: str) -> int:
        return self.entries.get(from_family, {}).get(to_family, 0)


class SchedulerSettings(BaseModel):
    time_limit_seconds: int = Field(default=30, gt=0)
    objective_mode: ObjectiveMode = ObjectiveMode.MIN_TARDINESS


class SchedulingProblem(BaseModel):
    horizon: TimeWindow
    resources: list[Resource]
    jobs: list[Job]
    changeover_matrix: ChangeoverMatrix
    settings: SchedulerSettings


class Assignment(BaseModel):
    job_id: str
    operation_index: int = Field(ge=0)
    capability: str
    resource_id: str
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_start_before_end(self) -> "Assignment":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class SchedulerSuccess(BaseModel):
    assignments: list[Assignment]


class InfeasibleResult(BaseModel):
    error: str = "infeasible"
    why: list[str] = Field(min_length=1)


SchedulerResult: TypeAlias = SchedulerSuccess | InfeasibleResult


class KPIs(BaseModel):
    tardiness_minutes: int = Field(ge=0)
    changeover_count: int = Field(ge=0)
    changeover_minutes: int = Field(ge=0)
    makespan_minutes: int = Field(ge=0)
    utilization_pct: dict[str, int]
