# Distributed Image Rendering Farm

A distributed systems project that generates fractal art, sunset skylines, and
geometric patterns as PNGs. Submit jobs from the browser or API, track their
progress, and download the finished images.

The FastAPI gateway validates and rate-limits submissions. The Render API
writes each job and its outbox entry in one PostgreSQL transaction, then returns
the job ID. A publisher sends the job to Redis Streams, and an available worker
writes the PNG to a shared Docker volume. Redis also caches completed job
statuses. See the [architecture and data flows](docs/architecture.md).

## Run locally

Install Git and Docker with Docker Compose. Start Docker Desktop on Windows,
then clone the repository and run the stack from the project folder. Use
`--build` on a fresh checkout or after changing the source.

```powershell
git clone https://github.com/Brainstorm0/distributed-image-rendering-farm.git
cd distributed-image-rendering-farm
docker compose up -d --build --scale worker=2 --wait
docker compose ps -a
curl.exe -i http://localhost:8080/health
Start-Process 'http://localhost:8080/'
```

If the build hits a package-download timeout and your machine already has the
images for this version, start them with:

```powershell
docker compose up -d --no-build --scale worker=2 --wait
```

The `migrate` service exits with code 0 after applying the idempotent SQL files
in `db/migrations`. The Render API, publisher, and workers start after
migrations succeed. Compose retains the PostgreSQL, Redis, and image volumes.
Run `docker compose down` from the repository root when you finish.

On macOS or Linux, use the same Git and Compose commands. Open
[the frontend](http://localhost:8080/) in your browser, and use
`curl -i http://localhost:8080/health` to check the gateway.

## Submit, poll, and download

```powershell
$base = 'http://localhost:8080'
$settings = @{ width = 640; height = 360; style = 'skyline'; seed = 42 } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri "$base/render" -ContentType 'application/json' -Body $settings
$job

$url = "$base/jobs/$($job.job_id)"
for ($i = 0; $i -lt 30; $i++) {
    $status = Invoke-RestMethod $url
    if ($status.status -in 'completed', 'failed') { break }
    Start-Sleep -Seconds 1
}
if ($status.status -ne 'completed') { throw "Render status is $($status.status)" }
curl.exe -f "$url/image" -o skyline.png
curl.exe -i $url
```

`POST /render` returns HTTP 202. Dimensions range from 1 through 2048, and
`max_iterations` ranges from 1 through 1000 with a default of 100. The per-client
token bucket holds five tokens and refills at one token per second. Rate-limited
submissions receive HTTP 429 with `Retry-After`.

Completed status responses are cached for five minutes. A repeated status
request shows `X-Cache: HIT`. The gateway reports the Render API and both Phase 1
mock nodes at `/health`. The mock routes are `/nodes/a/info` and `/nodes/b/info`.

## Browser frontend

After starting Docker Compose, open [Image Rendering Farm](http://localhost:8080/).
Choose a style and dimensions, then select **Render image**. The result panel
shows the job and trace IDs, live status, and a PNG preview and download.

Click a completed Mandelbrot, Seahorse Valley, or Geometric rosette image to
render a 2× zoom around that point. The result
panel keeps the previous view alongside the new PNG. **Zoom at center** gives
the same action using the keyboard.

For skyline renders, choose seed 42 or 99 to select a reproducible layout. The
same seed and dimensions produce the same PNG. **Compare skyline seeds** submits
one job per seed so you can compare the two outputs and worker
IDs. API clients can send a `seed` integer from 0 to 2147483647 with
`"style":"skyline"`. Omitting it uses the original skyline.

## Art styles

| Browser label | API `style` | Output |
| --- | --- | --- |
| Sunset skyline | `skyline` | A city at sunset. The optional `seed` controls building heights, widths, and lit windows. |
| Mandelbrot | `mandelbrot` | The original blue fractal and the default API style. |
| Seahorse Valley | `seahorse` | A purple close-up of the Mandelbrot boundary. |
| Geometric rosette | `geometric` | A pattern made from rotating polygons. |

The fractal styles accept `max_iterations`. The fractal and geometric styles
accept an optional `viewport` object.
The viewport uses normalized `x` and `y` coordinates from 0 to 1 and a
`scale` from 1/1024 to 1. For example, `{"x":0.62,"y":0.39,"scale":0.5}`
requests a 2× zoom centered near that point of the style's original view.

## Load test and recovery demo

The browser's **Job activity** panel shows the selected render's accepted
response, worker timestamps, PNG preview, and repeated status cache result.
Use the trace ID in the service logs to see internal events such as publication
to the Redis Stream. In the browser, select **Run load test** to send 20
requests and see response counts, completed renders, latency, and cache hits.

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

In the baseline benchmark, two workers shared a burst of 20 submissions with concurrency set to 10
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

The [Compose smoke test](.github/workflows/smoke.yml) builds the stack on
GitHub Actions, checks the gateway and frontend, then runs the API load test
against two workers and verifies the resulting PNGs.

See [architecture and data flows](docs/architecture.md) for the Phase 1 and
current deployments.

## Ten-minute live demo

The [load test report](output/pdf/load-test-report.pdf) contains the measured results.
Run the following from the repository root in PowerShell.

| Time | Demonstrate |
| --- | --- |
| 0:00–1:30 | Start the stack with the local run command, show `docker compose ps -a`, and open `/health`. |
| 1:30–3:00 | Open the browser frontend and select **Compare skyline seeds**. Show the two skyline layouts and their worker IDs. |
| 3:00–5:00 | Filter the logs by trace ID. Walk through the API transaction, the outbox publisher, Redis Streams, the worker's database update, and the cache hit. |
| 5:00–7:00 | Run the concurrent load test. Explain the 202 and 429 counts, throughput, and p95 status read latency. |
| 7:00–8:30 | Stop the publisher, submit a job, show its queued status, then restart the publisher and show completion. |
| 8:30–10:00 | Render a Mandelbrot image, click its boundary to zoom, and download the result. Discuss the report's burst-test limits. |

```powershell
$base = 'http://localhost:8080'
$body = @{ width = 256; height = 256 } | ConvertTo-Json
$submitted = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$base/render" -ContentType 'application/json' -Body $body
$trace = [string]$submitted.Headers['X-Trace-ID']
$id = ($submitted.Content | ConvertFrom-Json).job_id
for ($i = 0; $i -lt 30; $i++) {
    $job = Invoke-RestMethod "$base/jobs/$id"
    if ($job.status -in 'completed', 'failed') { break }
    Start-Sleep -Seconds 1
}
if ($job.status -ne 'completed') { throw "Render status is $($job.status)" }
$cached = Invoke-WebRequest -UseBasicParsing "$base/jobs/$id"
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
