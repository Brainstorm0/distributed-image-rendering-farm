import json
import os
import time

import psycopg
import redis

STREAM = "renderfarm:jobs"


def publish_one(db, queue):
    with db.transaction():
        row = db.execute(
            """
            SELECT outbox.job_id, COALESCE(jobs.trace_id, jobs.id)
            FROM render_outbox AS outbox
            JOIN render_jobs AS jobs ON jobs.id = outbox.job_id
            ORDER BY outbox.created_at, outbox.job_id
            LIMIT 1
            FOR UPDATE OF outbox SKIP LOCKED
            """
        ).fetchone()

        if row is None:
            return None

        job_id, trace_id = map(str, row)
        message_id = queue.xadd(STREAM, {"job_id": job_id, "trace_id": trace_id})
        db.execute("DELETE FROM render_outbox WHERE job_id = %s", (job_id,))

    return {
        "event": "job_published", "component": "redis_stream", "job_id": job_id,
        "trace_id": trace_id, "message_id": message_id,
    }


def main():
    queue = redis.Redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    retry_delay = 1

    while True:
        try:
            with psycopg.connect(connect_timeout=3, autocommit=True) as db:
                while True:
                    event = publish_one(db, queue)
                    retry_delay = 1
                    if event is None:
                        time.sleep(1)
                    else:
                        print(json.dumps(event))
        except (psycopg.OperationalError, redis.exceptions.RedisError) as exc:
            print(json.dumps({
                "event": "publish_retry",
                "error_type": type(exc).__name__,
                "retry_in_seconds": retry_delay,
            }))
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 8)


if __name__ == "__main__":
    main()
