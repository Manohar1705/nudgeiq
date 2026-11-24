"""Smoke check of a running server:  python -m app.smoke [base_url] [--write]

Default (read-only): /health, /campaigns, /decisions, /insights/uplift.
--write also seeds 5 DEMO customers and runs one decision, so it adds fake data to the
database it points at. Do not use it on real data.

Exit code 0 when every check passes, 1 otherwise. Inside Docker:
  docker compose exec backend python -m app.smoke --write
"""

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

AS_OF = "2026-11-02T10:00:00Z"  # a Monday morning, inside the allowed sending hours

# A caller takes (method, path, json_body) and returns (status_code, parsed_json).
Caller = Callable[[str, str, Any], tuple[int, Any]]
Check = tuple[str, bool, str]


def run_smoke(call: Caller, write: bool = False) -> list[Check]:
    results: list[Check] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    status, body = call("GET", "/health", None)
    check("health answers 200", status == 200, f"status {status}")
    check(
        "a campaign is live",
        isinstance(body, dict) and body.get("status") == "ok",
        f"health said {body.get('status') if isinstance(body, dict) else body!r}",
    )

    status, body = call("GET", "/campaigns", None)
    check("campaigns list", status == 200 and isinstance(body, list), f"status {status}")

    status, body = call("GET", "/decisions?limit=1", None)
    check("decisions list", status == 200 and isinstance(body, list), f"status {status}")

    status, body = call("GET", "/insights/uplift", None)
    check("uplift summary", status == 200 and isinstance(body, dict), f"status {status}")

    if write:
        status, body = call("POST", "/demo/seed", {"count": 5, "seed": 1})
        seeded = status == 200 and isinstance(body, dict) and "first_id" in body
        check("seed 5 demo customers", seeded, f"status {status}")
        if seeded:
            status, body = call(
                "POST", "/decide", {"customer_id": body["first_id"], "as_of": AS_OF}
            )
            check(
                "decide for one customer",
                status == 200 and isinstance(body, dict) and "decision_id" in body,
                f"status {status}",
            )
    return results


def http_caller(base_url: str) -> Caller:
    base = base_url.rstrip("/")

    def call(method: str, path: str, body: Any) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, TimeoutError) as exc:
            return 0, str(exc)

    return call


def main(argv: list[str]) -> int:
    write = "--write" in argv
    urls = [a for a in argv if not a.startswith("--")]
    base = urls[0] if urls else "http://127.0.0.1:8000"
    results = run_smoke(http_caller(base), write=write)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} checks passed against {base}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
