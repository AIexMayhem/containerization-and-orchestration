"""Bounded in-cluster traffic, invoked by verify_stack.py as a temporary Job."""

import concurrent.futures
import http.client
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://lab2-api.labs.svc.cluster.local:80"


def hit(kind, path):
    retries = 0
    for attempt in range(3):
        try:
            try:
                response = urllib.request.urlopen(BASE + path, timeout=8)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                response.read()
                status = response.code
            assert status == (500 if kind == "fail" else 200), (kind, status)
            return kind, retries
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            retries += 1
            if attempt == 2:
                raise
            time.sleep(0.2)
    raise AssertionError("Unreachable retry state")


def main():
    duration = int(sys.argv[1])
    assert 1 <= duration <= 600
    counts = {"success": 0, "fail": 0, "slow": 0}
    retries = 0
    started = time.monotonic()
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for second in range(duration):
            for kind, path in [
                ("success", "/health"),
                ("fail", "/fail"),
                ("slow", "/slow?seconds=2"),
            ]:
                futures.append(pool.submit(hit, kind, path))
            time.sleep(max(0, started + second + 1 - time.monotonic()))
        for future in futures:
            kind, attempts = future.result()
            counts[kind] += 1
            retries += attempts
    print(json.dumps({"traffic": counts, "transport_retries": retries}), flush=True)


if __name__ == "__main__":
    main()
