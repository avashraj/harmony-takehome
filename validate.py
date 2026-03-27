"""Acceptance check script for the Harmony Production Scheduler.

Runs 3 checks against a live server and reports pass/fail.

Usage:
    uv run python validate.py                         # default: http://localhost:8000
    uv run python validate.py --url http://host:8000

Exit code: 0 = all passed, 1 = any failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

FEASIBLE_PAYLOAD = {
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
    "settings": {"time_limit_seconds": 30, "objective_mode": "min_tardiness"},
}

# Two 300-minute fill jobs on a single 400-minute window: each fits individually
# (passes semantic validation) but cannot coexist -- solver returns infeasible.
INFEASIBLE_PAYLOAD = {
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


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def post(url: str, payload: dict) -> tuple[int, dict]:
    """POST JSON to url, return (status_code, response_body)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60


# ---------------------------------------------------------------------------
# Check 1 -- Invariants: no overlap + precedence
# ---------------------------------------------------------------------------


def check_invariants(url: str) -> None:
    """No resource overlap and per-product step ordering on the 4-product response."""
    status, body = post(url, FEASIBLE_PAYLOAD)
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert "assignments" in body, f"No 'assignments' key in response: {body}"

    assignments = body["assignments"]

    # --- No overlap ---
    by_resource: dict[str, list[dict]] = defaultdict(list)
    for a in assignments:
        by_resource[a["resource"]].append(a)

    for resource_id, ops in by_resource.items():
        sorted_ops = sorted(ops, key=lambda a: _dt(a["start"]))
        for i in range(len(sorted_ops) - 1):
            end_i = _dt(sorted_ops[i]["end"])
            start_next = _dt(sorted_ops[i + 1]["start"])
            assert end_i <= start_next, (
                f"Overlap on {resource_id}: "
                f"{sorted_ops[i]['product']} ends {end_i}, "
                f"{sorted_ops[i+1]['product']} starts {start_next}"
            )

    # --- Precedence ---
    by_product: dict[str, list[dict]] = defaultdict(list)
    for a in assignments:
        by_product[a["product"]].append(a)

    for product_id, steps in by_product.items():
        sorted_steps = sorted(steps, key=lambda a: a["step_index"])
        for i in range(len(sorted_steps) - 1):
            end_i = _dt(sorted_steps[i]["end"])
            start_next = _dt(sorted_steps[i + 1]["start"])
            assert end_i <= start_next, (
                f"Precedence violated for {product_id}: "
                f"step {sorted_steps[i]['step_index']} ends {end_i}, "
                f"step {sorted_steps[i+1]['step_index']} starts {start_next}"
            )


# ---------------------------------------------------------------------------
# Check 2 -- KPI reproducibility
# ---------------------------------------------------------------------------


def check_kpi_reproducibility(url: str) -> None:
    """Recompute all 5 KPIs from assignments and diff against reported values."""
    status, body = post(url, FEASIBLE_PAYLOAD)
    assert status == 200, f"Expected 200, got {status}: {body}"

    assignments = body["assignments"]
    reported = body["kpis"]

    # Build lookup tables from the payload directly (no app/ imports)
    payload = FEASIBLE_PAYLOAD
    product_due = {p["id"]: _dt(p["due"]) for p in payload["products"]}
    product_family = {p["id"]: p["family"] for p in payload["products"]}
    resource_calendar = {
        r["id"]: [(_dt(w[0]), _dt(w[1])) for w in r["calendar"]]
        for r in payload["resources"]
    }
    changeover_flat: dict[str, int] = payload["changeover_matrix_minutes"]["values"]

    def get_changeover(fam_a: str, fam_b: str) -> int:
        return changeover_flat.get(f"{fam_a}->{fam_b}", 0)

    # tardiness_minutes
    product_max_end: dict[str, datetime] = {}
    for a in assignments:
        pid = a["product"]
        t = _dt(a["end"])
        if pid not in product_max_end or t > product_max_end[pid]:
            product_max_end[pid] = t

    tardiness = 0
    for pid, completion in product_max_end.items():
        due = product_due[pid]
        if completion > due:
            tardiness += int(_minutes(due, completion))

    # changeover_count + changeover_minutes
    by_resource: dict[str, list[dict]] = defaultdict(list)
    for a in assignments:
        by_resource[a["resource"]].append(a)

    co_count = 0
    co_minutes = 0
    for ops in by_resource.values():
        sorted_ops = sorted(ops, key=lambda a: _dt(a["start"]))
        for i in range(len(sorted_ops) - 1):
            fam_curr = product_family[sorted_ops[i]["product"]]
            fam_nxt = product_family[sorted_ops[i + 1]["product"]]
            m = get_changeover(fam_curr, fam_nxt)
            if m > 0:
                co_count += 1
                co_minutes += m

    # makespan_minutes
    all_starts = [_dt(a["start"]) for a in assignments]
    all_ends = [_dt(a["end"]) for a in assignments]
    makespan = int(_minutes(min(all_starts), max(all_ends)))

    # utilization_pct
    resource_busy: dict[str, int] = defaultdict(int)
    for a in assignments:
        resource_busy[a["resource"]] += int(
            _minutes(_dt(a["start"]), _dt(a["end"]))
        )

    utilization: dict[str, int] = {}
    for r in payload["resources"]:
        cal_minutes = sum(
            int(_minutes(ws, we)) for ws, we in resource_calendar[r["id"]]
        )
        if cal_minutes > 0:
            utilization[r["id"]] = round(resource_busy[r["id"]] * 100 / cal_minutes)
        else:
            utilization[r["id"]] = 0

    # Assert within 1-minute tolerance
    TOLERANCE = 1

    assert abs(tardiness - reported["tardiness_minutes"]) <= TOLERANCE, (
        f"tardiness_minutes: computed {tardiness}, reported {reported['tardiness_minutes']}"
    )
    assert abs(co_count - reported["changeover_count"]) <= TOLERANCE, (
        f"changeover_count: computed {co_count}, reported {reported['changeover_count']}"
    )
    assert abs(co_minutes - reported["changeover_minutes"]) <= TOLERANCE, (
        f"changeover_minutes: computed {co_minutes}, reported {reported['changeover_minutes']}"
    )
    assert abs(makespan - reported["makespan_minutes"]) <= TOLERANCE, (
        f"makespan_minutes: computed {makespan}, reported {reported['makespan_minutes']}"
    )
    for rid, pct in utilization.items():
        reported_pct = reported["utilization_pct"].get(rid, 0)
        assert abs(pct - reported_pct) <= TOLERANCE, (
            f"utilization_pct[{rid}]: computed {pct}, reported {reported_pct}"
        )


# ---------------------------------------------------------------------------
# Check 3 -- Infeasible case
# ---------------------------------------------------------------------------


def check_infeasible_case(url: str) -> None:
    """Known-infeasible payload returns 422 + structured error body."""
    status, body = post(url, INFEASIBLE_PAYLOAD)
    assert status == 422, f"Expected 422, got {status}: {body}"
    assert body.get("error") == "infeasible", (
        f"Expected error='infeasible', got: {body}"
    )
    why = body.get("why", [])
    assert isinstance(why, list) and len(why) >= 1, (
        f"Expected non-empty 'why' list, got: {why}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECKS = [check_invariants, check_kpi_reproducibility, check_infeasible_case]


def main() -> None:
    parser = argparse.ArgumentParser(description="Harmony acceptance checks")
    parser.add_argument(
        "--url",
        default="http://localhost:8000/api/v1/schedule",
        help="Full URL for the schedule endpoint",
    )
    args = parser.parse_args()
    url: str = args.url

    print(f"Running acceptance checks against {url}\n")

    failures: list[str] = []
    for check in CHECKS:
        try:
            check(url)
            print(f"  PASS  {check.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {check.__name__}: {e}")
            failures.append(check.__name__)
        except Exception as e:
            print(f"  ERROR {check.__name__}: {e}")
            failures.append(check.__name__)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
