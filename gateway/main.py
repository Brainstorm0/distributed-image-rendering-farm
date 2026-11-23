import asyncio
import math
from contextlib import asynccontextmanager
from time import monotonic

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

NODES = {
    "a": "http://mock-a:8000",
    "b": "http://mock-b:8000",
}

BUCKET_CAPACITY = 5
REFILL_PER_SECOND = 1.0
buckets: dict[str, tuple[float, float]] = {}


class RenderRequest(BaseModel):
    width: int = Field(strict=True, ge=1)
    height: int = Field(strict=True, ge=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=2.0) as client:
        app.state.http = client
        yield


app = FastAPI(lifespan=lifespan)


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
        upstream = await request.app.state.http.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Mock service timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "Mock service is unavailable") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def probe(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(f"{url}/health")
        response.raise_for_status()
        if response.json().get("status") == "healthy":
            return "healthy"
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return "unavailable"


@app.get("/health")
async def health(request: Request):
    results = await asyncio.gather(
        *(probe(request.app.state.http, url) for url in NODES.values())
    )
    nodes = {f"mock-{key}": status for key, status in zip(NODES, results)}
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
        raise HTTPException(422, "Width and height must be positive integers") from exc

    return await forward(
        request,
        "POST",
        f"{NODES['a']}/render",
        json=settings.model_dump(),
    )