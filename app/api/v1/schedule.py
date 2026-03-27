from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.adapters.client_a import ClientARequest, adapt, format_assignment
from app.models import InfeasibleResult
from app.scheduler import compute_kpis, solve
from app.validation import validate_problem

router = APIRouter()


@router.post("/schedule")
async def schedule(request: ClientARequest):
    problem = adapt(request)
    validation = validate_problem(problem)
    if not validation.is_valid:
        return JSONResponse(
            status_code=422,
            content={"issues": [issue.model_dump() for issue in validation.issues]},
        )

    result = solve(problem)

    if isinstance(result, InfeasibleResult):
        return result.model_dump()

    kpis = compute_kpis(problem, result.assignments)
    return {
        "assignments": [format_assignment(a) for a in result.assignments],
        "kpis": kpis.model_dump(),
    }
