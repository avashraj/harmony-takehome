import uvicorn
from fastapi import FastAPI

from app.api.v1.schedule import router

app = FastAPI(title="Harmony Scheduling API")
app.include_router(router, prefix="/api/v1")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
