from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

app = FastAPI(title="Render API")


class RenderRequest(BaseModel):
    width: int = Field(strict=True, ge=1, le=2048)
    height: int = Field(strict=True, ge=1, le=2048)
    max_iterations: int = Field(default=100, strict=True, ge=1, le=1000)


@app.post("/render", status_code=202)
def create_job(settings: RenderRequest):
    job_id = uuid4()

    try:
        with psycopg.connect(connect_timeout=3) as db:
            db.execute(
                """
                INSERT INTO render_jobs (id, width, height, max_iterations)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, settings.width, settings.height, settings.max_iterations),
            )
            db.execute(
                "INSERT INTO render_outbox (job_id) VALUES (%s)",
                (job_id,),
            )
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: UUID):
    try:
        with psycopg.connect(connect_timeout=3, row_factory=dict_row) as db:
            job = db.execute(
                """
                SELECT id AS job_id, width, height, max_iterations, status,
                       attempts, created_at, started_at, finished_at
                FROM render_jobs
                WHERE id = %s
                """,
                (job_id,),
            ).fetchone()
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc

    if job is None:
        raise HTTPException(404, "Render job not found")

    return job