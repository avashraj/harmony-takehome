# Harmony Production Scheduler

A constraint-based production scheduling API built with FastAPI and OR-Tools CP-SAT.
Accepts a production planning problem and returns an optimized, feasible schedule with KPIs.

---

## Running the service

**Prerequisites:** Python 3.13+, [`uv`](https://docs.astral.sh/uv/)

```bash
# Install dependencies
uv sync

# Start the server (http://localhost:8000)
uv run python main.py
```

The API is available at `http://localhost:8000/api/v1/schedule`.

Interactive docs: `http://localhost:8000/docs`

### Example request

```bash
curl -X POST http://localhost:8000/api/v1/schedule \
  -H "Content-Type: application/json" \
  -d '{
    "horizon": {"start": "2025-11-03T08:00:00", "end": "2025-11-03T16:00:00"},
    "resources": [
      {"id": "Fill-1", "capabilities": ["fill"], "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]]},
      {"id": "Label-1", "capabilities": ["label"], "calendar": [["2025-11-03T08:00:00", "2025-11-03T16:00:00"]]}
    ],
    "changeover_matrix_minutes": {"values": {"standard->standard": 0, "standard->premium": 20, "premium->standard": 20, "premium->premium": 0}},
    "products": [
      {"id": "P-100", "family": "standard", "due": "2025-11-03T12:00:00",
       "route": [{"capability": "fill", "duration_minutes": 30}, {"capability": "label", "duration_minutes": 20}]}
    ],
    "settings": {"time_limit_seconds": 30, "objective_mode": "min_tardiness"}
  }'
```

---

## Running tests

```bash
uv run pytest tests/ -v
```

All 60 tests should pass. The suite covers canonical models, the Client A adapter,
the semantic validator, the CP-SAT solver, and the HTTP endpoint end-to-end.

### Acceptance checks (requires running server)

```bash
uv run python validate.py
```

Runs 3 checks against the live API: constraint invariants (no overlap + precedence),
KPI reproducibility, and the infeasible case. Exit code 0 = all passed.

---

## Approach

### Solver: OR-Tools CP-SAT

The scheduling problem is modelled as a variant of **Flexible Job Shop Scheduling**:

- Each product has an ordered sequence of operations (a route).
- Each operation requires a specific capability and must fit inside a single
  contiguous calendar window of the assigned resource.
- Resources can only run one operation at a time.
- Switching between job families on the same resource requires a sequence-dependent
  changeover gap.
- Objective: minimize total tardiness (sum of `max(0, completion − due)` across all products).

CP-SAT was chosen because it handles all of the above natively:
`OptionalIntervalVar` for calendar-gated placements, `AddNoOverlap` for resource
exclusivity, conditional `Add(...).OnlyEnforceIf(...)` for changeovers, and
`AddMaxEquality` for the tardiness linearization.

### Changeover modelling

Changeovers are encoded as **pairwise ordering constraints** rather than circuit
constraints. For every pair of operations `(a, b)` that could both land on the same
resource, an ordering boolean `a_before_b` is introduced. The required setup time
is enforced conditionally on both operations being co-located and the ordering
variable being set. This is simpler to extend than a full circuit formulation and
sufficient for the expected problem sizes.

---

## Assumptions and tradeoffs

| Decision | Rationale |
|---|---|
| HTTP 422 for infeasible results | Infeasibility is a valid scheduling outcome but the request cannot be fulfilled, so a 422 with a structured `{"error": "infeasible", "why": [...]}` body is returned. |
| Semantic validation before solving | Catches obviously unsolvable inputs (orphan capabilities, zero-window resources) cheaply without invoking the solver. |
| Pairwise changeover constraints | Simpler than circuit constraints, O(n²) per resource. Suitable for the sizes seen in production scheduling problems at this scale. |
| One `start`/`end` variable per operation | Shared across all resource/window options. `OptionalIntervalVar` presence literals handle resource and window selection. This keeps precedence constraints simple (`end[o] <= start[o+1]`). |
| `time_limit_seconds` passed directly to CP-SAT | The solver returns the best solution found within the limit. If time expires before proving optimality, a `FEASIBLE` (not `OPTIMAL`) solution is returned — still valid. |

---

## Design note

### Request flow

```
POST /api/v1/schedule
        │
        ▼
ClientARequest          ← Pydantic parsing (FastAPI)
        │  adapt()
        ▼
SchedulingProblem       ← canonical internal model
        │  validate_problem()
        ▼
ValidationResult ──── invalid ──→ 422 {"error": "infeasible", "why": [...]}
        │ valid
        ▼
solve(problem)          ← CP-SAT solver
        │
  ┌─────┴──────┐
  │            │
feasible    infeasible
  │            │
  ▼            ▼
compute_kpis   422 {"error": "infeasible", "why": [...]}
  │
  ▼
200 {"assignments": [...], "kpis": {...}}
```
