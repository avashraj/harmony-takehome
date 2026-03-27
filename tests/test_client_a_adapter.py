from datetime import datetime

import pytest

from app.adapters.client_a import ClientARequest, adapt
from app.models import SchedulingProblem, TimeWindow


def _sample_payload() -> dict:
    return {
        "horizon": {
            "start": "2025-11-03T08:00:00",
            "end": "2025-11-03T16:00:00",
        },
        "resources": [
            {
                "id": "Fill-1",
                "capabilities": ["fill"],
                "calendar": [
                    ["2025-11-03T08:00:00", "2025-11-03T12:00:00"],
                    ["2025-11-03T12:30:00", "2025-11-03T16:00:00"],
                ],
            },
            {
                "id": "Fill-2",
                "capabilities": ["fill"],
                "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
            },
            {
                "id": "Label-1",
                "capabilities": ["label"],
                "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
            },
            {
                "id": "Pack-1",
                "capabilities": ["pack"],
                "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
            },
        ],
        "changeover_matrix_minutes": {
            "values": {
                "standard->standard": 0,
                "standard->premium": 20,
                "premium->standard": 20,
                "premium->premium": 0,
            }
        },
        "products": [
            {
                "id": "P-100",
                "family": "standard",
                "due": "2025-11-03T12:30:00",
                "route": [
                    {"capability": "fill", "duration_minutes": 30},
                    {"capability": "label", "duration_minutes": 20},
                    {"capability": "pack", "duration_minutes": 15},
                ],
            },
            {
                "id": "P-101",
                "family": "premium",
                "due": "2025-11-03T15:00:00",
                "route": [
                    {"capability": "fill", "duration_minutes": 35},
                    {"capability": "label", "duration_minutes": 25},
                    {"capability": "pack", "duration_minutes": 15},
                ],
            },
            {
                "id": "P-102",
                "family": "standard",
                "due": "2025-11-03T13:30:00",
                "route": [
                    {"capability": "fill", "duration_minutes": 25},
                    {"capability": "label", "duration_minutes": 20},
                ],
            },
            {
                "id": "P-103",
                "family": "premium",
                "due": "2025-11-03T14:00:00",
                "route": [
                    {"capability": "fill", "duration_minutes": 30},
                    {"capability": "label", "duration_minutes": 20},
                    {"capability": "pack", "duration_minutes": 15},
                ],
            },
        ],
        "settings": {
            "time_limit_seconds": 30,
            "objective_mode": "min_tardiness",
        },
    }


def test_parse_full_request() -> None:
    request = ClientARequest.model_validate(_sample_payload())
    assert len(request.products) == 4
    assert len(request.resources) == 4


def test_adapt_produces_correct_problem() -> None:
    request = ClientARequest.model_validate(_sample_payload())
    problem = adapt(request)

    assert isinstance(problem, SchedulingProblem)
    assert len(problem.jobs) == 4
    assert len(problem.resources) == 4
    assert problem.horizon.start == datetime.fromisoformat("2025-11-03T08:00:00")
    assert problem.horizon.end == datetime.fromisoformat("2025-11-03T16:00:00")


def test_changeover_arrow_keys_parsed() -> None:
    request = ClientARequest.model_validate(_sample_payload())
    problem = adapt(request)

    assert problem.changeover_matrix.get_minutes("standard", "premium") == 20
    assert problem.changeover_matrix.get_minutes("unknown", "premium") == 0


def test_calendar_tuples_become_time_windows() -> None:
    request = ClientARequest.model_validate(_sample_payload())
    problem = adapt(request)

    fill_1 = next(resource for resource in problem.resources if resource.id == "Fill-1")
    assert len(fill_1.calendar) == 2
    assert all(isinstance(window, TimeWindow) for window in fill_1.calendar)
    assert fill_1.calendar[0].duration_minutes == 240
    assert fill_1.calendar[1].duration_minutes == 210


def test_product_to_job_mapping() -> None:
    request = ClientARequest.model_validate(_sample_payload())
    problem = adapt(request)

    job = next(j for j in problem.jobs if j.id == "P-101")
    assert job.family == "premium"
    assert job.due == datetime.fromisoformat("2025-11-03T15:00:00")
    assert [operation.capability for operation in job.operations] == [
        "fill",
        "label",
        "pack",
    ]
    assert [operation.duration_minutes for operation in job.operations] == [35, 25, 15]


def test_invalid_changeover_key_raises() -> None:
    payload = _sample_payload()
    payload["changeover_matrix_minutes"]["values"] = {"standard_premium": 20}
    request = ClientARequest.model_validate(payload)

    with pytest.raises(ValueError, match="invalid changeover key"):
        adapt(request)
