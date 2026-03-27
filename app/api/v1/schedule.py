from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.adapters.client_a import ClientARequest, adapt
from app.validation import validate_problem

router = APIRouter()


@router.post("/schedule")
async def schedule(request: ClientARequest):
    problem = adapt(request)
    result = validate_problem(problem)
    if not result.is_valid:
        return JSONResponse(
            status_code=422,
            content={"issues": [issue.model_dump() for issue in result.issues]},
        )
    return {"status": "accepted", "message": "Scheduler not yet implemented."}
