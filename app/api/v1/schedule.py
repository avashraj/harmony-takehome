from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.adapters.client_a import ClientARequest, adapt, format_assignment
from app.models import InfeasibleResult
from app.scheduler import compute_kpis, solve
from app.validation import validate_problem

router = APIRouter()


@router.post("/schedule")
def schedule(request: ClientARequest):
    try:
        problem = adapt(request)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    validation = validate_problem(problem)
    if not validation.is_valid:
        return JSONResponse(
            status_code=422,
            content={"issues": [issue.model_dump() for issue in validation.issues]},
        )

    try:
        result = solve(problem)
    except RuntimeError as exc:
        return JSONResponse(status_code=500, content={"detail": f"Solver internal error: {exc}"})
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Unexpected server error."})

    if isinstance(result, InfeasibleResult):
        return JSONResponse(status_code=422, content=result.model_dump())

    kpis = compute_kpis(problem, result.assignments)
    return {
        "assignments": [format_assignment(a) for a in result.assignments],
        "kpis": kpis.model_dump(),
    }
