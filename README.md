# Distributed Image Rendering Farm

A distributed systems assignment that generates Mandelbrot PNGs asynchronously.
The FastAPI gateway accepts requests and rate-limits submissions. The Render API
stores jobs and outbox entries in PostgreSQL. A publisher sends the job IDs to a
Redis Stream, and workers render images into a shared Docker volume.

## Run locally

Start Docker Desktop on Windows, then run this from the repository root.

```powershell
docker compose up -d --build --scale worker=2 --wait
docker compose ps -a
curl.exe -i http://localhost:8080/health
```

The `migrate` service exits with code 0 after applying the idempotent SQL files
in `db/migrations`. The Render API, publisher, and workers start after
migrations succeed. Compose retains the PostgreSQL, Redis, and image volumes.

## Submit, poll, and download

```powershell
$base = 'http://localhost:8080'
$settings = @{ width = 256; height = 256; max_iterations = 100 } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri "$base/render" -ContentType 'application/json' -Body $settings
$job

Invoke-RestMethod "$base/jobs/$($job.job_id)"
# Repeat the status request until it reports completed, then download the image.
curl.exe -f "$base/jobs/$($job.job_id)/image" -o render.png
curl.exe -i "$base/jobs/$($job.job_id)"
```

`POST /render` returns HTTP 202. Dimensions range from 1 through 2048, and
`max_iterations` ranges from 1 through 1000 with a default of 100. The per-client
token bucket holds five tokens and refills at one token per second. Rate-limited
submissions receive HTTP 429 with `Retry-After`.

Completed status responses are cached for five minutes. A repeated status
request shows `X-Cache: HIT`. The gateway reports the Render API and both Phase 1
mock nodes at `/health`. The mock routes are `/nodes/a/info` and `/nodes/b/info`.

## Load test and recovery demo

Run the dependency-free load test with Python 3, or use the gateway container's
Python as shown here. The script reports HTTP response counts, submission
latency, completed jobs per second, status read latency, and cache outcomes.
It also checks the PNG signature of each completed render.

```powershell
Get-Content -Raw scripts/load_test.py | docker compose exec -T gateway python -
```

To compare repeated status reads with caching disabled, recreate the Render API
with the setting below. Allow the submission bucket to refill between runs.

```powershell
$env:STATUS_CACHE_ENABLED = '0'
docker compose up -d --wait render-api
Get-Content -Raw scripts/load_test.py | docker compose exec -T gateway python -
Remove-Item Env:STATUS_CACHE_ENABLED
docker compose up -d --wait render-api
```

For an outbox recovery demo, stop `publisher`, submit a job, and confirm that
its status is `queued`. Start `publisher` and poll the same job until it
completes. Inspect the correlated job IDs in the publisher and worker logs.

```powershell
docker compose stop publisher
# Submit a job using the example above and check its status.
docker compose start publisher
docker compose logs --tail 50 gateway publisher worker
```

Workers reclaim unacknowledged messages after 60 seconds. PostgreSQL advisory
locks serialize duplicate deliveries for the same job, and workers acknowledge
messages after writing the terminal job status. `docker compose down` stops
the stack while retaining the volumes.

## Local verification

Two workers shared a burst of 20 submissions with concurrency set to 10
on Docker Desktop. Each request asked for a 256 × 256 image with
100 iterations. Both runs returned five accepted submissions and 15
rate-limit responses. All accepted jobs completed and returned PNGs,
and both worker containers logged completed jobs.

| Measurement | Cache enabled | Cache disabled |
| --- | ---: | ---: |
| Submission latency, p50 / p95 | 14.33 / 148.98 ms | 26.05 / 175.95 ms |
| Accepted submissions per second | 31.99 | 27.36 |
| Completed renders per second | 4.63 | 6.11 |
| Repeated status read latency, p50 / p95 | 28.73 / 43.91 ms | 70.87 / 116.77 ms |
| Repeated status cache results | 25 hits | 25 bypasses |

Submission latency includes both HTTP response codes. Throughput reflects a
short burst from a single client IP. Completion throughput includes polling
time and can vary with the publisher's one-second polling interval.

An outage check confirmed that a submission persisted in PostgreSQL while the
publisher was stopped. After restart, an interrupted test consumer left the
message pending. A worker reclaimed it, completed the 1 × 1 image in one
render attempt, and the gateway served the PNG and a cached status response.
A separate check confirmed that stopping mock B produced HTTP 503 with
individual node statuses, and that restarting it restored health.

Docker's package downloads timed out during the clean build attempt. The
application checks used a cached Python image and checksum-verified wheels
for the project's pinned dependencies.

See [architecture and data flows](docs/architecture.md) for the Phase 1 and
current deployments.

## Ten-minute live demo

The [load test report](output/pdf/load-test-report.pdf) contains the measured results.
Run the following from the repository root in PowerShell.

| Time | Demonstrate |
| --- | --- |
| 0:00–1:30 | Run `docker compose up -d --build --scale worker=2 --wait`, show `docker compose ps -a`, and open `/health`. |
| 1:30–3:00 | Submit and poll the job below. Explain validation and the token bucket in `gateway/main.py`. |
| 3:00–5:00 | Filter the logs by trace ID. Walk through the API transaction, the outbox publisher, Redis Streams, the worker's database update, and the cache hit. |
| 5:00–7:00 | Run the concurrent load test. Explain the 202 and 429 counts, throughput, and p95 status read latency. |
| 7:00–8:30 | Stop the publisher, submit a job, show its queued status, then restart the publisher and show completion. |
| 8:30–10:00 | Download the PNG, discuss the report's burst-test limits, and take questions. |

```powershell
$base = 'http://localhost:8080'
$body = @{ width = 256; height = 256 } | ConvertTo-Json
$submitted = Invoke-WebRequest -Method Post -Uri "$base/render" -ContentType 'application/json' -Body $body
$trace = [string]$submitted.Headers['X-Trace-ID']
$id = ($submitted.Content | ConvertFrom-Json).job_id
for ($i = 0; $i -lt 30; $i++) {
    $job = Invoke-RestMethod "$base/jobs/$id"
    if ($job.status -in 'completed', 'failed') { break }
    Start-Sleep -Seconds 1
}
if ($job.status -ne 'completed') { throw "Render status is $($job.status)" }
$cached = Invoke-WebRequest "$base/jobs/$id"
$cached.Headers['X-Cache']
docker compose logs --no-color gateway render-api publisher worker | Select-String -SimpleMatch $trace
curl.exe -f "$base/jobs/$id/image" -o render.png
Start-Sleep -Seconds 5
Get-Content -Raw scripts/load_test.py | docker compose exec -T gateway python -
```

The submission's `X-Trace-ID` follows the job through PostgreSQL, Redis
Streams, the worker, and the status cache. Polls and downloads have their
own `X-Request-ID` and return the same job trace ID. Jobs created before
migration 003 use their job ID when no submission trace was stored.
