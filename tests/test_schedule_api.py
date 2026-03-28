"""Integration tests for POST /api/v1/schedule.

Four scenarios are covered:
1. Structurally bad payload      → 422 from FastAPI's own Pydantic parsing layer.
2. Semantic validation failure   → 422 with our custom {"issues": [...]} body.
3. Multiple semantic issues      → all issues returned in one pass (non-fail-fast).
4. Fully valid payload (minimal) → 200 with assignments and kpis.
5. Full 4-product example        → 200, hard constraints verified in response.
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


def _full_payload() -> dict:
    """The canonical 4-product, 4-resource example from the spec."""
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


# ---------------------------------------------------------------------------
# Test 1 – structurally bad payload
# ---------------------------------------------------------------------------


def test_bad_payload_returns_422() -> None:
    """An empty JSON body is missing all required fields; FastAPI rejects it."""
    response = client.post("/api/v1/schedule", json={})

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


# ---------------------------------------------------------------------------
# Test 2 – semantic validation failure
# ---------------------------------------------------------------------------


def test_orphan_capability_returns_422_with_issues() -> None:
    """A well-formed request whose operation requires a capability ('weld') that
    no resource provides triggers our semantic validator and returns a 422 with
    the infeasible error body."""
    payload = _valid_payload()
    payload["products"][0]["route"].append(
        {"capability": "weld", "duration_minutes": 10}
    )

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body.get("error") == "infeasible"
    why = body.get("why", [])
    assert isinstance(why, list) and len(why) >= 1
    assert any("weld" in msg for msg in why)


def test_multiple_semantic_issues_all_returned() -> None:
    """All validation issues are collected in one pass (non-fail-fast)."""
    payload = _valid_payload()
    payload["resources"].append(
        {"id": "Pack-1", "capabilities": ["pack"], "calendar": []}
    )
    payload["products"][0]["route"].append(
        {"capability": "pack", "duration_minutes": 15}
    )
    payload["products"][0]["route"].append(
        {"capability": "weld", "duration_minutes": 10}
    )

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body.get("error") == "infeasible"
    why = body.get("why", [])
    assert isinstance(why, list) and len(why) >= 2
    combined = " ".join(why)
    assert "Pack-1" in combined or "never available" in combined
    assert "weld" in combined


# ---------------------------------------------------------------------------
# Test 4 – infeasible problem returns 422 with structured error body
# ---------------------------------------------------------------------------


def test_infeasible_problem_returns_422_with_error_body() -> None:
    """A problem that passes semantic validation but is unsolvable at the solver
    level must return 422 with the structured {'error': 'infeasible', 'why': [...]}
    body.

    The trigger: two jobs each requiring 300 minutes of fill, but the only fill
    resource has a single 400-minute window.  Each operation individually fits
    (300 < 400) so the validator passes, but combined they need 600 minutes which
    exceeds the available 400 minutes -- the solver returns infeasible.
    """
    payload = {
        "horizon": {
            "start": "2025-11-03T08:00:00",
            "end": "2025-11-03T14:40:00",  # 400-minute horizon
        },
        "resources": [
            {
                "id": "Fill-1",
                "capabilities": ["fill"],
                "calendar": [["2025-11-03T08:00:00", "2025-11-03T14:40:00"]],
            }
        ],
        "changeover_matrix_minutes": {"values": {}},
        "products": [
            {
                "id": "P-A",
                "family": "standard",
                "due": "2025-11-03T14:40:00",
                "route": [{"capability": "fill", "duration_minutes": 300}],
            },
            {
                "id": "P-B",
                "family": "standard",
                "due": "2025-11-03T14:40:00",
                "route": [{"capability": "fill", "duration_minutes": 300}],
            },
        ],
        "settings": {"time_limit_seconds": 5, "objective_mode": "min_tardiness"},
    }

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body.get("error") == "infeasible"
    assert isinstance(body.get("why"), list)
    assert len(body["why"]) >= 1


# ---------------------------------------------------------------------------
# Test 5 – valid minimal payload returns real schedule
# ---------------------------------------------------------------------------


def test_valid_payload_returns_assignments_and_kpis() -> None:
    """A fully valid request returns a solved schedule with assignments and KPIs."""
    response = client.post("/api/v1/schedule", json=_valid_payload())

    assert response.status_code == 200
    body = response.json()
    assert "assignments" in body
    assert "kpis" in body
    assert len(body["assignments"]) > 0

    # Verify Client A output field names
    first = body["assignments"][0]
    assert "product" in first
    assert "step_index" in first
    assert "resource" in first
    assert "capability" in first
    assert "start" in first
    assert "end" in first
    # step_index is 1-based
    assert first["step_index"] >= 1

    kpis = body["kpis"]
    assert "tardiness_minutes" in kpis
    assert "changeover_count" in kpis
    assert "changeover_minutes" in kpis
    assert "makespan_minutes" in kpis
    assert "utilization_pct" in kpis


# ---------------------------------------------------------------------------
# Test 5 – full 4-product example: verify hard constraints
# ---------------------------------------------------------------------------


def test_full_example_satisfies_hard_constraints() -> None:
    """The 4-product, 4-resource spec example is feasible and the returned
    schedule satisfies all hard constraints."""
    from datetime import datetime

    response = client.post("/api/v1/schedule", json=_full_payload())

    assert response.status_code == 200
    body = response.json()
    assert "assignments" in body, body
    assignments = body["assignments"]

    # Build lookup structures using Client A field names
    by_product: dict[str, list[dict]] = {}
    for a in assignments:
        by_product.setdefault(a["product"], []).append(a)

    def parse_dt(s: str) -> datetime:
        return datetime.fromisoformat(s)

    horizon_start = parse_dt("2025-11-03T08:00:00")
    horizon_end = parse_dt("2025-11-03T16:00:00")

    # 1. Horizon bounds: all assignments within [horizon_start, horizon_end]
    for a in assignments:
        assert parse_dt(a["start"]) >= horizon_start, f"start before horizon: {a}"
        assert parse_dt(a["end"]) <= horizon_end, f"end after horizon: {a}"

    # 2. Precedence: within each product, steps are ordered by step_index (1-based)
    for product_id, ops in by_product.items():
        sorted_ops = sorted(ops, key=lambda x: x["step_index"])
        for i in range(len(sorted_ops) - 1):
            curr_end = parse_dt(sorted_ops[i]["end"])
            next_start = parse_dt(sorted_ops[i + 1]["start"])
            assert curr_end <= next_start, (
                f"Precedence violated for {product_id}: step {sorted_ops[i]['step_index']} "
                f"ends at {curr_end}, step {sorted_ops[i+1]['step_index']} starts at {next_start}"
            )

    # 3. No overlap: on each resource, no two operations overlap
    by_resource: dict[str, list[dict]] = {}
    for a in assignments:
        by_resource.setdefault(a["resource"], []).append(a)

    for resource_id, ops in by_resource.items():
        sorted_ops = sorted(ops, key=lambda x: parse_dt(x["start"]))
        for i in range(len(sorted_ops) - 1):
            curr_end = parse_dt(sorted_ops[i]["end"])
            next_start = parse_dt(sorted_ops[i + 1]["start"])
            assert curr_end <= next_start, (
                f"Overlap on {resource_id}: {sorted_ops[i]} overlaps {sorted_ops[i+1]}"
            )

    # 4. Calendar compliance: Fill-1 has a break 12:00-12:30
    fill1_ops = by_resource.get("Fill-1", [])
    break_start = parse_dt("2025-11-03T12:00:00")
    break_end = parse_dt("2025-11-03T12:30:00")
    for a in fill1_ops:
        op_start = parse_dt(a["start"])
        op_end = parse_dt(a["end"])
        # Operation must not span the break
        assert not (op_start < break_end and op_end > break_start), (
            f"Fill-1 op spans break window: {a}"
        )

    # 5. KPIs are present and non-negative
    kpis = body["kpis"]
    assert kpis["tardiness_minutes"] >= 0
    assert kpis["changeover_count"] >= 0
    assert kpis["changeover_minutes"] >= 0
    assert kpis["makespan_minutes"] > 0
    for resource_id, pct in kpis["utilization_pct"].items():
        assert 0 <= pct <= 100, f"utilization out of range for {resource_id}: {pct}"


# ---------------------------------------------------------------------------
# Error handling: adapt() ValueError paths
# ---------------------------------------------------------------------------


def test_invalid_objective_mode_returns_422_with_detail() -> None:
    """An unrecognised objective_mode string raises ValueError inside adapt();
    the endpoint must catch it and return 422 with a 'detail' key."""
    payload = _valid_payload()
    payload["settings"]["objective_mode"] = "invalid_mode"

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body


def test_malformed_changeover_key_returns_422_with_detail() -> None:
    """A changeover key missing the '->' separator raises ValueError inside adapt();
    the endpoint must catch it and return 422 with a 'detail' key."""
    payload = _valid_payload()
    payload["changeover_matrix_minutes"]["values"] = {"standard_premium": 20}

    response = client.post("/api/v1/schedule", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
