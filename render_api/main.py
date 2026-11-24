import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import redis
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

app = FastAPI(title="Render API")

CACHE_ENABLED = os.getenv("STATUS_CACHE_ENABLED", "1") == "1"
cache = redis.Redis.from_url(
    os.environ["REDIS_URL"],
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,
)


class RenderRequest(BaseModel):
    width: int = Field(strict=True, ge=1, le=2048)
    height: int = Field(strict=True, ge=1, le=2048)
    max_iterations: int = Field(default=100, strict=True, ge=1, le=1000)


@app.get("/health")
def health():
    try:
        with psycopg.connect(connect_timeout=2) as db:
            db.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "healthy"}


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
    cache_key = f"renderfarm:status:{job_id}"

    if CACHE_ENABLED:
        try:
            cached = cache.get(cache_key)
            if cached is not None:
                return JSONResponse(json.loads(cached), headers={"X-Cache": "HIT"})
        except (redis.exceptions.RedisError, json.JSONDecodeError):
            pass

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

    payload = jsonable_encoder(job)
    if CACHE_ENABLED and job["status"] == "completed":
        try:
            cache.set(cache_key, json.dumps(payload), ex=300)
        except redis.exceptions.RedisError:
            pass

    return JSONResponse(payload, headers={"X-Cache": "MISS" if CACHE_ENABLED else "BYPASS"})


@app.get("/jobs/{job_id}/image")
def download_image(job_id: UUID):
    try:
        with psycopg.connect(connect_timeout=3) as db:
            job = db.execute(
                "SELECT status FROM render_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc

    if job is None:
        raise HTTPException(404, "Render job not found")
    if job[0] != "completed":
        raise HTTPException(409, "Render job has not completed")

    image_path = Path("/renders") / f"{job_id}.png"
    if not image_path.is_file():
        raise HTTPException(404, "Rendered image not found")

    return FileResponse(image_path, media_type="image/png", filename=image_path.name)
