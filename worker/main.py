import json
import math
import os
import socket
import time
from pathlib import Path
from random import Random
from uuid import UUID

import psycopg
import redis
from PIL import Image, ImageDraw, ImageOps

STREAM = "renderfarm:jobs"
GROUP = "render-workers"
CONSUMER = socket.gethostname()
OUTPUT_DIR = Path("/renders")
MAX_ATTEMPTS = 3
FRACTAL_STYLES = {
    "mandelbrot": ((-2.2, -1.3, 0.8, 1.3), "#080f24", "#52d6e8", 255),
    "seahorse": ((-0.806, 0.065, -0.681, 0.190), "#130c35", "#c9aaff", 80),
}


def log(event, **fields):
    print(json.dumps({"event": event, "worker": CONSUMER, **fields}))


def create_group(queue):
    try:
        # Start at 0 so the group receives jobs published before workers started.
        queue.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if not str(exc).startswith("BUSYGROUP"):
            raise


def draw_skyline(width, height, seed=None):
    random = Random(seed) if seed is not None else None
    image = Image.new("RGB", (640, 360))
    draw = ImageDraw.Draw(image)
    for y in range(360):
        draw.line((0, y, 639, y), fill=(36 + y // 2, 24 + y // 3, 87 + y // 6))
    draw.ellipse((242, 66, 398, 222), fill="#ffd38c")
    draw.polygon([(0, 265), (100, 182), (240, 268), (470, 177), (639, 257),
                  (639, 359), (0, 359)], fill="#684060")
    rooftops = (222, 191, 215, 145, 200, 163, 226, 185, 207, 244)
    edges = [0, *(i * 64 + (random.randint(-18, 18) if random else 0)
                  for i in range(1, 10)), 640]
    for index, top in enumerate(rooftops):
        left, right = edges[index:index + 2]
        if random:
            top = random.randint(115, 265)
        draw.rectangle((left + 2, top, right - 4, 359), fill="#1d203b")
        for column, x in enumerate(range(left + 10, right - 10, 16)):
            for row, y in enumerate(range(top + 14, 325, 20)):
                lights_on = random.randrange(4) != 0 if random else (index + column + row * 2) % 4 != 0
                if lights_on:
                    draw.rectangle((x, y, x + 7, y + 10), fill="#f9bb77")
    draw.rectangle((0, 340, 639, 359), fill="#14162b")
    return image.resize((width, height), Image.Resampling.LANCZOS)


def draw_geometric(width, height, viewport=None):
    base_size = 640
    background = "#102e36"
    image = Image.new("RGB", (base_size, base_size), background)
    draw = ImageDraw.Draw(image)
    colors = ("#f5c26b", "#e37455", "#64c4ae", "#faf0d2")
    for radius in range(288, 8, -16):
        points = []
        for vertex in range(8):
            angle = vertex * math.tau / 8 + radius / 95
            points.append((320 + radius * math.cos(angle), 320 + radius * math.sin(angle)))
        draw.polygon(points, outline=colors[(radius // 16) % len(colors)], width=4)
    if viewport:
        center_x = viewport["x"] * base_size
        center_y = viewport["y"] * base_size
        half = viewport["scale"] * base_size / 2
        return image.transform(
            (width, height), Image.Transform.EXTENT,
            (center_x - half, center_y - half, center_x + half, center_y + half),
            resample=Image.Resampling.BICUBIC, fillcolor=background,
        )
    return image.resize((width, height), Image.Resampling.LANCZOS)


def draw_fractal(width, height, iterations, style, viewport):
    (left, top, right, bottom), dark, light, whitepoint = FRACTAL_STYLES[style]
    if viewport:
        span_x, span_y = right - left, bottom - top
        center_x = left + span_x * viewport["x"]
        center_y = top + span_y * viewport["y"]
        half_x = span_x * viewport["scale"] / 2
        half_y = span_y * viewport["scale"] / 2
        left, right = center_x - half_x, center_x + half_x
        top, bottom = center_y - half_y, center_y + half_y
    if iterations == 1:
        # Both viewports are inside Pillow's escape radius after one iteration.
        fractal = Image.new("L", (width, height))
    else:
        # Sample the center on single-pixel axes and crop the duplicate sample.
        if width == 1:
            left = right = (left + right) / 2
        if height == 1:
            top = bottom = (top + bottom) / 2
        size = (max(width, 2), max(height, 2))
        fractal = Image.effect_mandelbrot(size, (left, top, right, bottom), iterations)
        if size != (width, height):
            fractal = fractal.crop((0, 0, width, height))
    return ImageOps.colorize(fractal, dark, light, whitepoint=whitepoint)


def render_png(job_id, width, height, iterations, style="mandelbrot", viewport=None, seed=None):
    output = OUTPUT_DIR / f"{job_id}.png"
    temporary = OUTPUT_DIR / f"{job_id}.{CONSUMER}.tmp"

    try:
        if style == "skyline":
            image = draw_skyline(width, height, seed)
        elif style == "geometric":
            image = draw_geometric(width, height, viewport)
        else:
            image = draw_fractal(width, height, iterations, style, viewport)
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
                worker_name = %s,
                started_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status IN ('queued', 'processing')
            RETURNING width, height, max_iterations, style, viewport, seed, attempts, COALESCE(trace_id, id)
            """,
            (CONSUMER, job_id),
        ).fetchone()
        if job is None:
            # The job is already finished or has been deleted.
            return True

        width, height, iterations, style, viewport, seed, attempt, trace_id = job
        context = {"job_id": str(job_id), "trace_id": str(trace_id)}
        log("render_started", **context, attempt=attempt, style=style)
        try:
            if attempt > MAX_ATTEMPTS:
                raise ValueError("Render interrupted too many times")
            path = render_png(job_id, width, height, iterations, style, viewport, seed)
        except (OSError, ValueError) as exc:
            if isinstance(exc, OSError) and attempt < MAX_ATTEMPTS:
                log("render_retry", **context, attempt=attempt, error=str(exc))
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
            log("render_failed", **context, component="postgres", error=str(exc))
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
            log("render_completed", **context, component="postgres", output_path=path)

        return True


def handle_message(queue, message_id, fields):
    job_id = UUID(fields["job_id"])
    if process_job(job_id):
        queue.xack(STREAM, GROUP, message_id)
        log("job_acknowledged", job_id=str(job_id), trace_id=fields.get("trace_id", str(job_id)),
            component="redis_stream", message_id=message_id)


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
