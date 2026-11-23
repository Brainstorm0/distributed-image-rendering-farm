# Distributed Image Rendering Farm

A Distributed Systems assignment implementing an API Gateway and a pool of
workers that generate PNG images asynchronously. Clients will submit an
image request, receive a job ID, and use that ID to check progress and
download the completed image.

See the [architecture diagrams and data flows](docs/architecture.md) for
the Phase 1 mock deployment and planned rendering farm.

## Assignment milestones

| Phase | Deliverables |
| --- | --- |
| 1. Architecture, Docker, and Gateway | Architecture and data flow diagrams, Docker Compose, two mock nodes, gateway routing, token-bucket rate limiting with HTTP `429`, and per-node health reporting. |
| 2. Persistence, caching, and workers | PostgreSQL schema and migrations, Redis caching and queueing, asynchronous render workers, and an updated Compose stack. |
| 3. Resilience, load testing, and demo | Fault recovery, structured logs, a concurrent load-test script, a 2–3 page report or slide deck with measured results, and a 10-minute live demo and code walkthrough. |

## Phase 1 setup

The commands below describe how to run and verify Phase 1 once the Gateway,
mock service, and `docker-compose.yml` have been implemented.

Prerequisites are Git and Docker with Docker Compose. On Windows, start
Docker Desktop before running the project. From the repository root, run:

```cmd
docker compose up --build
```

In a second Command Prompt window, check health and routing:

```cmd
curl.exe -i http://localhost:8080/health
curl.exe -i http://localhost:8080/nodes/a/info
curl.exe -i http://localhost:8080/nodes/b/info
```

Submit a sample render request:

```cmd
curl.exe -i -H "Content-Type: application/json" -d "{\"width\":256,\"height\":256}" http://localhost:8080/render
```

With a token available, the request should return HTTP `202` and a
placeholder job ID. A burst that drains the token bucket should produce
HTTP `429` with a `Retry-After` header.

To verify per-node health reporting, run `docker compose stop mock-b`
from the repository root and check `/health` again. The response should
report `mock-a` as healthy and `mock-b` as unavailable. Restore the node
with `docker compose start mock-b`.