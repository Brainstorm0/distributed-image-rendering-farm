import os
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
node_name = os.environ.get("NODE_NAME", "mock")


class RenderRequest(BaseModel):
    width: int
    height: int


@app.get("/health")
def health():
    return {"node": node_name, "status": "healthy"}


@app.get("/info")
def info():
    return {"node": node_name}


@app.post("/render", status_code=202)
def render(request: RenderRequest):
    return {"job_id": str(uuid4()), "status": "mock"}