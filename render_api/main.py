import json
import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

app = FastAPI(title="Render API")
logger = logging.getLogger("uvicorn.error")

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


def request_id(request: Request) -> UUID:
    try:
        return UUID(request.headers.get("X-Request-ID", ""))
    except ValueError:
        return uuid4()


def log(event, job_id, trace_id, request, **fields):
    logger.info(json.dumps({
        "event": event,
        "job_id": str(job_id),
        "trace_id": str(trace_id),
        "request_id": str(request),
        **fields,
    }))


@app.get("/health")
def health():
    try:
        with psycopg.connect(connect_timeout=2) as db:
            db.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "healthy"}


@app.post("/render", status_code=202)
def create_job(settings: RenderRequest, request: Request, response: Response):
    job_id = uuid4()
    trace_id = request_id(request)

    try:
        with psycopg.connect(connect_timeout=3) as db:
            db.execute(
                """
                INSERT INTO render_jobs (id, width, height, max_iterations, trace_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (job_id, settings.width, settings.height, settings.max_iterations, trace_id),
            )
            db.execute(
                "INSERT INTO render_outbox (job_id) VALUES (%s)",
                (job_id,),
            )
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc

    log("job_committed", job_id, trace_id, trace_id, component="postgres")
    response.headers["X-Trace-ID"] = str(trace_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: UUID, request: Request):
    poll_id = request_id(request)
    cache_key = f"renderfarm:status:v2:{job_id}"
    cache_status = "BYPASS"

    if CACHE_ENABLED:
        cache_status = "MISS"
        try:
            cached = cache.get(cache_key)
            if cached is not None:
                payload = json.loads(cached)
                trace_id = payload.pop("trace_id")
                log("status_cache", job_id, trace_id, poll_id, component="redis", result="HIT")
                return JSONResponse(payload, headers={"X-Cache": "HIT", "X-Trace-ID": trace_id})
        except (redis.exceptions.RedisError, json.JSONDecodeError, AttributeError, KeyError):
            pass

    try:
        with psycopg.connect(connect_timeout=3, row_factory=dict_row) as db:
            job = db.execute(
                """
                SELECT id AS job_id, width, height, max_iterations, status,
                       attempts, created_at, started_at, finished_at,
                       COALESCE(trace_id, id) AS trace_id
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
    trace_id = payload["trace_id"]
    log("job_status_read", job_id, trace_id, poll_id, component="postgres")
    if CACHE_ENABLED and job["status"] == "completed":
        try:
            cache.set(cache_key, json.dumps(payload), ex=300)
            log("status_cached", job_id, trace_id, poll_id, component="redis")
        except redis.exceptions.RedisError:
            pass

    log("status_cache", job_id, trace_id, poll_id, component="redis", result=cache_status)
    payload.pop("trace_id")
    return JSONResponse(payload, headers={"X-Cache": cache_status, "X-Trace-ID": trace_id})


@app.get("/jobs/{job_id}/image")
def download_image(job_id: UUID, request: Request):
    try:
        with psycopg.connect(connect_timeout=3) as db:
            job = db.execute(
                "SELECT status, COALESCE(trace_id, id) FROM render_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "Database unavailable") from exc

    if job is None:
        raise HTTPException(404, "Render job not found")
    if job[0] != "completed":
        raise HTTPException(
            409, "Render job has not completed", headers={"X-Trace-ID": str(job[1])}
        )

    image_path = Path("/renders") / f"{job_id}.png"
    if not image_path.is_file():
        raise HTTPException(404, "Rendered image not found")

    log("image_read", job_id, job[1], request_id(request), component="postgres")
    return FileResponse(
        image_path, media_type="image/png", filename=image_path.name,
        headers={"X-Trace-ID": str(job[1])},
    )
