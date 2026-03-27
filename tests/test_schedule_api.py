"""Integration tests for POST /api/v1/schedule.

Three scenarios are covered:
1. Structurally bad payload  → 422 from FastAPI's own Pydantic parsing layer.
2. Semantic validation failure → 422 with our custom {"issues": [...]} body.
3. Fully valid payload        → 200 stub acceptance response.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _valid_payload() -> dict:
    return {
        "horizon": {
            "start": "2025-11-03T08:00:00",
            "end": "2025-11-03T16:00:00",
        },
        "resources": [
            {
                "id": "Fill-1",
                "capabilities": ["fill"],
                "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]],
            },
            {
                "id": "Label-1",
                "capabilities": ["label"],
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
                "due": "2025-11-03T12:00:00",
                "route": [
                    {"capability": "fill", "duration_minutes": 30},
                    {"capability": "label", "duration_minutes": 20},
                ],
            },
        ],
        "settings": {
            "time_limit_seconds": 30,
            "objective_mode": "min_tardiness",
        },
    }


# ---------------------------------------------------------------------------
# Test 1 – structurally bad payload
# ---------------------------------------------------------------------------


def test_bad_payload_returns_422() -> None:
    """An empty JSON body is missing all required fields; FastAPI rejects it."""
    response = client.post("/api/v1/schedule", json={})

    assert response.status_code == 422
    # FastAPI's default error shape includes a "detail" key, not our "issues" key.
    body = response.json()
    assert "detail" in body


# ---------------------------------------------------------------------------
# Test 2 – semantic validation failure
# ---------------------------------------------------------------------------


def test_orphan_capability_returns_422_with_issues() -> None:
    """A well-formed request whose operation requires a capability ('weld') that
    no resource provides triggers our semantic validator and returns a 422 with
    a structured issues list."""
    payload = _valid_payload()
    # Add an operation that requires a capability no resource can handle.
    payload["products"][0]["route"].append(
        {"capability": "weld", "duration_minutes": 10}
    )

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert "issues" in body
    rules = [issue["rule"] for issue in body["issues"]]
    assert "orphan_capability" in rules


def test_multiple_semantic_issues_all_returned() -> None:
    """All validation issues are collected in one pass (non-fail-fast).
    Here we trigger both orphan_capability and resource_no_calendar."""
    payload = _valid_payload()
    # Resource with no calendar windows.
    payload["resources"].append(
        {"id": "Pack-1", "capabilities": ["pack"], "calendar": []}
    )
    # Operation requiring the resource with no calendar AND an unprovidable capability.
    payload["products"][0]["route"].append(
        {"capability": "pack", "duration_minutes": 15}
    )
    payload["products"][0]["route"].append(
        {"capability": "weld", "duration_minutes": 10}
    )

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    rules = {issue["rule"] for issue in body["issues"]}
    assert "resource_no_calendar" in rules
    assert "orphan_capability" in rules


# ---------------------------------------------------------------------------
# Test 3 – valid payload
# ---------------------------------------------------------------------------


def test_valid_payload_returns_200_stub() -> None:
    """A fully valid request passes parsing and semantic validation and receives
    the stub acceptance response."""
    response = client.post("/api/v1/schedule", json=_valid_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
