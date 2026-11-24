import json
import os
import socket
import time
from pathlib import Path
from uuid import UUID

import psycopg
import redis
from PIL import Image, ImageOps

STREAM = "renderfarm:jobs"
GROUP = "render-workers"
CONSUMER = socket.gethostname()
OUTPUT_DIR = Path("/renders")
MAX_ATTEMPTS = 3


def log(event, **fields):
    print(json.dumps({"event": event, "worker": CONSUMER, **fields}))


def create_group(queue):
    try:
        # Start at 0 so the group receives jobs published before workers started.
        queue.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if not str(exc).startswith("BUSYGROUP"):
            raise


def render_png(job_id, width, height, iterations):
    output = OUTPUT_DIR / f"{job_id}.png"
    temporary = OUTPUT_DIR / f"{job_id}.{CONSUMER}.tmp"

    try:
        if iterations == 1:
            # The viewport is inside Pillow's escape radius after one iteration.
            fractal = Image.new("L", (width, height))
        else:
            # Pillow divides by dimension minus one. Sample the viewport center
            # on any single-pixel axis and crop off its duplicate sample.
            left, right = (-0.7, -0.7) if width == 1 else (-2.2, 0.8)
            top, bottom = (0.0, 0.0) if height == 1 else (-1.3, 1.3)
            size = (max(width, 2), max(height, 2))
            fractal = Image.effect_mandelbrot(size, (left, top, right, bottom), iterations)
            if size != (width, height):
                fractal = fractal.crop((0, 0, width, height))
        image = ImageOps.colorize(fractal, "#080f24", "#52d6e8")
        image.save(temporary, format="PNG")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)

    return str(output)


def process_job(job_id):
    with psycopg.connect(connect_timeout=3, autocommit=True) as db:
        # Hold a per-job lock while rendering so duplicate messages cannot
        # cause two workers to render the same job at the same time.
        locked = db.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            (str(job_id),),
        ).fetchone()[0]
        if not locked:
            return False

        job = db.execute(
            """
            UPDATE render_jobs
            SET status = 'processing',
                attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status IN ('queued', 'processing')
            RETURNING width, height, max_iterations, attempts
            """,
            (job_id,),
        ).fetchone()
        if job is None:
            # The job is already finished or has been deleted.
            return True

        width, height, iterations, attempt = job
        log("render_started", job_id=str(job_id), attempt=attempt)
        try:
            if attempt > MAX_ATTEMPTS:
                raise ValueError("Render interrupted too many times")
            path = render_png(job_id, width, height, iterations)
        except (OSError, ValueError) as exc:
            if isinstance(exc, OSError) and attempt < MAX_ATTEMPTS:
                log("render_retry", job_id=str(job_id), attempt=attempt, error=str(exc))
                return False
            db.execute(
                """
                UPDATE render_jobs
                SET status = 'failed', error_message = %s,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (str(exc), job_id),
            )
            log("render_failed", job_id=str(job_id), error=str(exc))
        else:
            db.execute(
                """
                UPDATE render_jobs
                SET status = 'completed', output_path = %s,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (path, job_id),
            )
            log("render_completed", job_id=str(job_id), output_path=path)

        return True


def handle_message(queue, message_id, fields):
    job_id = UUID(fields["job_id"])
    if process_job(job_id):
        queue.xack(STREAM, GROUP, message_id)


def main():
    queue = redis.Redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cursor = "0-0"
    retry_delay = 1

    while True:
        try:
            create_group(queue)

            # Reclaim messages left unacknowledged by interrupted workers.
            cursor, pending, _ = queue.xautoclaim(
                STREAM, GROUP, CONSUMER,
                min_idle_time=60_000, start_id=cursor, count=1,
            )
            for message_id, fields in pending:
                handle_message(queue, message_id, fields)

            batches = queue.xreadgroup(
                GROUP, CONSUMER, {STREAM: ">"}, count=1, block=2000
            )
            for _, messages in batches:
                for message_id, fields in messages:
                    handle_message(queue, message_id, fields)
            retry_delay = 1
        except (psycopg.OperationalError, redis.exceptions.RedisError) as exc:
            log("worker_retry", error_type=type(exc).__name__,
                retry_in_seconds=retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 8)


if __name__ == "__main__":
    main()
