import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

NODES = {
    "a": "http://mock-a:8000",
    "b": "http://mock-b:8000",
}
RENDER_API = "http://render-api:8000"
HEALTH_TARGETS = {
    **{f"mock-{key}": url for key, url in NODES.items()},
    "render-api": RENDER_API,
}
logger = logging.getLogger("uvicorn.error")

BUCKET_CAPACITY = 5
REFILL_PER_SECOND = 1.0
buckets: dict[str, tuple[float, float]] = {}


class RenderRequest(BaseModel):
    width: int = Field(strict=True, ge=1, le=2048)
    height: int = Field(strict=True, ge=1, le=2048)
    max_iterations: int = Field(default=100, strict=True, ge=1, le=1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=5.0) as client:
        app.state.http = client
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_request(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    started = monotonic()
    status_code = 500
    trace_id = request_id
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        trace_id = response.headers.get("X-Trace-ID", request_id)
        response.headers["X-Trace-ID"] = trace_id
        status_code = response.status_code
        return response
    finally:
        logger.info(json.dumps({
            "event": "http_request",
            "request_id": request_id,
            "trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
            "status": status_code,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        }))


def consume_token(client_ip: str) -> int:
    """Return zero when allowed, or the retry delay in seconds."""
    now = monotonic()
    tokens, last_update = buckets.get(client_ip, (BUCKET_CAPACITY, now))
    tokens = min(BUCKET_CAPACITY, tokens + (now - last_update) * REFILL_PER_SECOND)

    if tokens < 1:
        buckets[client_ip] = (tokens, now)
        return math.ceil((1 - tokens) / REFILL_PER_SECOND)

    buckets[client_ip] = (tokens - 1, now)
    return 0


async def forward(request: Request, method: str, url: str, **kwargs) -> Response:
    try:
        upstream = await request.app.state.http.request(
            method, url, headers={"X-Request-ID": request.state.request_id}, **kwargs
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Upstream service timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "Upstream service is unavailable") from exc

    headers = {}
    for name in ("Content-Type", "Content-Disposition", "X-Cache", "X-Trace-ID"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


async def probe(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(f"{url}/health", timeout=3.0)
        response.raise_for_status()
        if response.json().get("status") == "healthy":
            return "healthy"
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return "unavailable"


@app.get("/health")
async def health(request: Request):
    results = await asyncio.gather(
        *(probe(request.app.state.http, url) for url in HEALTH_TARGETS.values())
    )
    nodes = dict(zip(HEALTH_TARGETS, results))
    healthy = all(status == "healthy" for status in results)
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "healthy" if healthy else "degraded", "nodes": nodes},
    )


@app.get("/nodes/{node}/info")
async def node_info(node: str, request: Request):
    if node not in NODES:
        raise HTTPException(404, "Unknown mock node")
    return await forward(request, "GET", f"{NODES[node]}/info")


@app.post("/render", status_code=202)
async def render(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    retry_after = consume_token(client_ip)
    if retry_after:
        raise HTTPException(
            429,
            "Too many render requests",
            headers={"Retry-After": str(retry_after)},
        )

    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        raise HTTPException(422, "Send the render request as application/json")

    try:
        settings = RenderRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(422, "Invalid render settings") from exc

    return await forward(
        request,
        "POST",
        f"{RENDER_API}/render",
        json=settings.model_dump(),
    )


@app.get("/jobs/{job_id}")
async def job_status(job_id: UUID, request: Request):
    return await forward(request, "GET", f"{RENDER_API}/jobs/{job_id}")


@app.get("/jobs/{job_id}/image")
async def job_image(job_id: UUID, request: Request):
    return await forward(request, "GET", f"{RENDER_API}/jobs/{job_id}/image")
