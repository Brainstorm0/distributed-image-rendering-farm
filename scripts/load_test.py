"""Submit concurrent render jobs and measure polling and cache behavior."""

import argparse
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(url, payload=None):
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    data = json.dumps(payload).encode() if payload is not None else None
    started = time.monotonic()
    try:
        with urlopen(Request(url, data, headers), timeout=10) as response:
            return (
                response.status, response.headers, response.read(),
                time.monotonic() - started,
            )
    except HTTPError as response:
        with response:
            return response.code, response.headers, response.read(), time.monotonic() - started


def latency(samples):
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        f"p{percent}_ms": round(1000 * ordered[math.ceil(percent / 100 * len(ordered)) - 1], 2)
        for percent in (50, 95)
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--poll-timeout", type=float, default=90)
    args = parser.parse_args()
    if min(args.requests, args.concurrency, args.poll_timeout) <= 0:
        parser.error("requests, concurrency, and poll timeout must be positive")

    base = args.url.rstrip("/")
    settings = {"width": 256, "height": 256}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        submissions = list(pool.map(
            lambda _: request(f"{base}/render", settings), range(args.requests)
        ))
        submit_seconds = time.monotonic() - started
        codes = Counter(code for code, _, _, _ in submissions)
        urls = [f"{base}/jobs/{json.loads(body)['job_id']}"
                for code, _, body, _ in submissions if code == 202]
        pending = urls.copy()
        completed = []
        failed = []
        deadline = time.monotonic() + args.poll_timeout
        while pending and time.monotonic() < deadline:
            remaining = []
            for url, (code, _, body, _) in zip(pending, pool.map(request, pending)):
                status = json.loads(body)["status"] if code == 200 else str(code)
                if status == "completed":
                    completed.append(url)
                elif status in ("queued", "processing", "502", "503", "504"):
                    remaining.append(url)
                else:
                    failed.append({"url": url, "status": status})
            pending = remaining
            if pending:
                time.sleep(0.25)
        completion_seconds = time.monotonic() - started

        reads = list(pool.map(request, completed * 5))
        read_codes = Counter(code for code, _, _, _ in reads)
        cache = Counter(headers.get("X-Cache", "unknown") for _, headers, _, _ in reads)
        bad_images = []
        for url in completed:
            code, _, image, _ = request(f"{url}/image")
            if code != 200 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                bad_images.append(url)

    print(json.dumps({
        "submission_codes": codes,
        "submission_latency": latency([elapsed for _, _, _, elapsed in submissions]),
        "accepted_per_second": round(len(urls) / submit_seconds, 2),
        "completed": len(completed),
        "completed_per_second": round(len(completed) / completion_seconds, 2),
        "failed": failed,
        "timed_out": pending,
        "status_read_codes": read_codes,
        "status_read_latency": latency([elapsed for _, _, _, elapsed in reads]),
        "status_cache": cache,
        "invalid_pngs": bad_images,
    }, indent=2))
    return int(bool(not urls or (set(codes) - {202, 429}) or failed or pending
                    or (set(read_codes) - {200}) or bad_images))


if __name__ == "__main__":
    raise SystemExit(main())
