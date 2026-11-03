"""Check that a worker is alive:  python -m app.worker.check [--timeout 10]

Sends the ping task through Redis and waits for the answer. Exit code 0 = a worker answered.
It fails fast (about a second) with a plain message when Redis is down.
"""

import sys

import redis

from app.worker.celery_app import celery_app
from app.worker.tasks import ping


def run_check(timeout: float = 10.0) -> tuple[bool, str]:
    url = str(celery_app.conf.broker_url)
    try:  # a direct ping first: Celery's own reconnect loop can spend ~20s on a dead Redis
        redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2).ping()
    except Exception as exc:
        return False, f"could not reach Redis at {url}: {exc}"
    try:
        result = ping.apply_async(args=["hello"], retry=False)
    except Exception as exc:
        return False, f"could not send the task to {url}: {exc}"
    try:
        answer = result.get(timeout=timeout)
    except Exception as exc:  # no worker, or the task failed
        return False, f"no answer within {timeout:g}s (is a worker running?): {exc!r}"
    if answer.get("echo") != "hello":
        return False, f"unexpected answer: {answer}"
    return True, f"worker {answer['worker']} answered"


def main(argv: list[str]) -> int:
    timeout = float(argv[argv.index("--timeout") + 1]) if "--timeout" in argv else 10.0
    ok, message = run_check(timeout)
    print(("PASS  " if ok else "FAIL  ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
