# NudgeIQ

Decides which nudge (channel, timing, offer, message) to send each customer, learns from the
outcomes, and measures uplift against a holdout group. Guardrails are hard rules the learned policy
can never override, and no message is sent until a person approves it.

    nudgeiq/
    ├── backend/    FastAPI service (decisions, guardrails, learning, analytics, campaigns)
    └── frontend/   React + TypeScript + Vite UI

## Requirements

- Python 3.12 or newer (use the same version on every machine)
- Node.js 18 or newer, with npm
- Docker with Compose v2, for the one-command setup or a local PostgreSQL

## Run everything with Docker (one command)

    cp .env.example .env          # optional; every setting has a default
    docker compose up --build

Then open:
- UI: http://localhost:8080
- API and docs: http://localhost:8000/docs

This starts PostgreSQL, Redis, the backend, a background worker, a scheduler (`beat`) and the frontend (served by nginx,
which forwards `/api` to the backend). Data is kept in the `nudgeiq_pg` Docker volume, so it survives
restarts.

The worker (Celery, with Redis) delivers scheduled nudges when they are due. Prove it is alive:

    docker compose exec worker python -m app.worker.check     # PASS  worker celery@... answered

    docker compose exec backend curl -s localhost:8000/scheduled-sends   # what is scheduled / sent
    docker compose logs -f worker       # watch nudges go out (lines starting SEND)
    docker compose logs -f backend      # watch the backend
    docker compose down                 # stop, keep data
    docker compose down -v              # stop and DELETE the database

Load demo data (the first start already creates the live `target_cart` campaign):

    curl -X POST http://localhost:8000/demo/seed -H "Content-Type: application/json" -d "{\"count\": 100, \"seed\": 0}"

Notes:
- To use a real free model, set `NUDGEIQ_LLM=chain` (or `groq`, `gemini`, `openrouter`), the key, and the model name in the root `.env`.
- Change the ports with `BACKEND_PORT` and `FRONTEND_PORT` in `.env`.
- Rebuild after code changes: `docker compose up --build`.
- The database has no published port by default. Uncomment `ports` under `db` in
  `docker-compose.yml` if you want to connect with `psql`.
- `backend/docker-compose.yml` (database only, for running uvicorn by hand) uses port 5432 and
  can clash with the full stack's database if you publish it. Run one or the other.
- Run one backend (uvicorn) process only. The repository shares one database connection (a known
  limit). The Celery `worker` and `beat` are separate services; the worker has its own database
  connection. Run exactly one `beat`.
- Change how many tasks the worker runs at once with `WORKER_CONCURRENCY` in `.env` (default 2).
- Set a real `POSTGRES_PASSWORD` before exposing this to anyone else.

## Quick start without Docker (about 5 minutes, no database needed)

Open two terminals.

**Terminal 1: backend**

macOS / Linux:

    cd backend
    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    uvicorn app.api.main:app --reload --env-file .env

Windows (PowerShell):

    cd backend
    py -3.12 -m venv venv
    venv\Scripts\activate
    pip install -r requirements.txt
    uvicorn app.api.main:app --reload --env-file .env

Check: http://127.0.0.1:8000/health should answer with `"status": "ok"`; http://127.0.0.1:8000/docs lists
every endpoint.

With no settings, the backend keeps data **in memory**: restarting it forgets customers, decisions and
drafts. See "Keeping your data" below.

**Terminal 2: frontend**

    cd frontend
    npm install
    npm run dev

Open http://localhost:5173. The dev server forwards `/api/*` to the backend, so no CORS setup is
needed. If the backend is down the UI shows "Backend not reachable".

**Load demo data** so every page has something to show:

    curl -X POST http://127.0.0.1:8000/demo/seed -H "Content-Type: application/json" -d "{\"count\": 100, \"seed\": 0}"

(or run `POST /demo/seed` from `/docs`). The first start also creates the `target_cart` campaign as
the live one, from `backend/app/config/use_cases/target_cart.json`.

## Keeping your data

| Want | Set before starting uvicorn |
|---|---|
| Nothing saved (default) | nothing |
| A JSON file | `NUDGEIQ_DATA_FILE=data.json` |
| PostgreSQL | `docker compose up -d`, then `NUDGEIQ_DATABASE_URL=postgresql://nudgeiq:nudgeiq@localhost:5432/nudgeiq` and `python -m app.repository.init_db` once |

If both are set, PostgreSQL wins. Run the commands from `backend/`.

## Environment variables

Copy `backend/.env.example` to `backend/.env`, edit it, and start the server with
`uvicorn app.api.main:app --reload --env-file .env` (uvicorn loads the file; a missing file is
ignored). Variables already set in your shell take priority over the file. You can also just export
them, as in the examples below.

| Variable | Meaning | Default |
|---|---|---|
| `NUDGEIQ_DATABASE_URL` | PostgreSQL address | unset |
| `NUDGEIQ_DATA_FILE` | JSON file for storage when no database is set | unset (memory) |
| `NUDGEIQ_LLM` | `mock`, `groq`, `gemini`, `openrouter` or `chain` | `mock` |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | Free keys for the provider you pick | unset |
| `NUDGEIQ_GROQ_MODEL` (and `_2`), `NUDGEIQ_GEMINI_MODEL`, `NUDGEIQ_OPENROUTER_MODEL` | Model names, copied from each console | unset |
| `NUDGEIQ_LLM_TIMEOUT` | Seconds to wait for the model | `60` |
| `NUDGEIQ_ENGINE` | `classic` (plain Python) or `graph` (LangGraph). Same decisions | `classic` |
| `NUDGEIQ_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `NUDGEIQ_CORS_ORIGINS` | Comma-separated browser origins allowed to call the API directly | unset (no CORS) |
| `NUDGEIQ_REDIS_URL` | Redis for the background worker. `redis://` or `rediss://` only; anything else stops start-up | `redis://localhost:6379/0` |
| `NUDGEIQ_SCHEDULED_SENDS` | `1` = store nudges and let the worker deliver them at their send time. Needs PostgreSQL | off (on in docker compose) |
| `NUDGEIQ_DISPATCH_INTERVAL_SECONDS` | How often `beat` asks for due sends (5 or more) | `30` |
| `NUDGEIQ_SEND_MAX_LATE_MINUTES` | A send found later than this is skipped, not delivered | `120` |
| `NUDGEIQ_TEST_REDIS` | Spare Redis database for the one test that sends a real task | unset |
| `NUDGEIQ_TEST_DSN` | Empty throwaway database for the PostgreSQL tests (it gets wiped) | unset |

Examples:

    # macOS / Linux
    export NUDGEIQ_DATABASE_URL=postgresql://nudgeiq:nudgeiq@localhost:5432/nudgeiq
    export NUDGEIQ_LLM=groq GROQ_API_KEY=... NUDGEIQ_GROQ_MODEL=...

    # Windows PowerShell
    $env:NUDGEIQ_DATABASE_URL="postgresql://nudgeiq:nudgeiq@localhost:5432/nudgeiq"
    $env:NUDGEIQ_LLM="groq"; $env:GROQ_API_KEY="..."; $env:NUDGEIQ_GROQ_MODEL="..."

A wrong model setting (unknown provider, missing key, bad timeout) stops the server at start-up with a
clear message; it never silently falls back to the mock. Keep real keys out of the repository.

The frontend has its own optional `frontend/.env` (see `frontend/.env.example`):

| Variable | Meaning | Default |
|---|---|---|
| `VITE_API_TARGET` | Backend the dev proxy forwards `/api` to | `http://127.0.0.1:8000` |
| `VITE_DEV_PORT` | Dev server port | `5173` |

## The pages

| Page | What it does |
|---|---|
| Configure | 7-step wizard: context, cohort, rewards, options, templates, variations, launch |
| Live | Decide for a customer, read the agent trace, simulate a reaction |
| Insights | Action, User and Uplift analytics |

How the wizard saves:
- Steps 1-4 only change local state.
- Opening the templates step clones the live campaign into a **draft**. Variants are generated and
  approved on the draft; approvals are saved immediately.
- **Launch** validates the draft and, only if there are no errors, makes it live, **replacing the
  running campaign**.
- A rejected variant cannot be approved again; generate a new one.
- LLM-written variants are always `pending` until a person approves them.
- An unfinished wizard run leaves a draft behind; archive it with `POST /campaigns/{id}/archive`.
- The cohort picked in step 1 is not saved (the campaign config has no target-cohort field).

## Known gaps in the current zips

The frontend calls three endpoints that are **not in this backend zip**. Until they are added, the
matching screens show an error or an empty state:

| Endpoint | Screen affected |
|---|---|
| `POST /demo/respond` | Live page: "simulate customer reaction" |
| `GET /insights/messages` | Insights, Action Analytics |
| `GET /insights/guardrails` | Insights, Uplift tab: guardrail rates |

Their contracts are in `frontend/API-CONTRACT-4A.md` and `frontend/API-CONTRACT-5A.md`.

Other limits worth knowing: guardrail hours are checked in UTC; one campaign is live at a time;
dispatch only logs (nothing is really sent); one shared database connection; no login, so reviewers
are not recorded.

## Decision engine: classic or LangGraph

The decision flow (state, guardrails, propose, Master, message, dispatch) exists twice and must
behave the same:

- `classic`: `Orchestrator._decide_classic()`, the default.
- `graph`: the same flow as a LangGraph (`backend/app/graph/`), turned on with `NUDGEIQ_ENGINE=graph`.
  The diagram and design are in `docs/langgraph-design.md`.

The Live page's agent trace looks the same with either engine. Switching needs a server restart.
With `graph`, each decision also gets a per-node trace (timing, outcome, keys written):
`GET /decisions/{id}/trace`. See "Reading a decision's trace" below.
`tests/test_graph_engine.py` runs both engines on the same customers and seeds and requires identical
decisions, outcomes and learned values. Run it before you switch, and keep `classic` if it fails.
Outcomes (`record_outcome`) always use the plain code.

## Variant generation with an approval pause (LangGraph)

Besides `POST /campaigns/{id}/variants/generate`, there is a second way to draft variants that
stops for a person. It needs the `langgraph` package (already in `requirements.txt`) and is
available whatever `NUDGEIQ_ENGINE` says.

| Call | What it does |
|---|---|
| `POST /campaigns/{id}/variant-runs` body `{"template_id": "...", "count": 3}` | Asks the model, checks every draft, saves the good ones as PENDING, then waits. Returns the run with its variants and `"status": "awaiting_review"` |
| `GET /campaigns/{id}/variant-runs/{run_id}` | The run as it is now |
| `POST /campaigns/{id}/variant-runs/{run_id}/review` body `{"decisions": [{"variant_id": "...", "action": "approve", "note": "..."}]}` | Applies the decisions and finishes the run. `action` is `approve` or `reject` |

Nothing is approved until someone calls `review`. Variants you do not mention stay pending. Runs
are kept in memory, so after a restart use the ordinary approve and reject endpoints on the pending
variants. Design and picture: `docs/langgraph-design.md`. Test: `tests/test_variant_graph.py`.

### Reading a run's trace

Every run response has a `trace`: one entry per attempt of each node, in order, and an `error`
that is set only when the latest attempt failed.

    {"seq": 2, "node": "draft_variants", "attempt": 1, "status": "retry",
     "started_at": "2026-11-03T10:15:02.114+00:00", "duration_ms": 412.5,
     "wrote": [], "notes": [], "error": {"type": "LLMError", "message": "rate limited"}}

| Field | Meaning |
|---|---|
| `status` | `ok` finished; `paused` stopped at the approval pause (not a failure); `retry` failed, another attempt follows; `error` failed for good |
| `attempt` | 1, 2, ... The model call (`draft_variants`) is tried up to 2 times, only for model errors. No other node is retried |
| `wrote`, `notes` | The state keys the node returned, and the note it added to `steps`. Never the values themselves |
| `error` | Exception class and a message cut to 300 characters. Never a traceback |

After a review, `await_review` shows up twice: once `paused`, once `ok`. That is how LangGraph
resumes (the node starts again and gets the answer), not a duplicate.

Failures: a bad request is still 404 / 409 / 422 and a model failure is still 502. Anything else
(a bug, a lost database) is a `500` whose message names the node and the error type, for example
`variant run 'ab12...' failed at 'apply_review' (RuntimeError); see the server log`. The traceback
is in the server log, on the logger `nudgeiq.graph`, which also writes one line per attempt:
`node=draft_variants run=ab12... attempt=2 status=ok ms=388.1`. A run that fails while applying a
review ends as `"status": "error"` and its variants stay pending; use the ordinary approve and
reject endpoints on them. Traces live in memory with the run. Tests: `tests/test_graph_trace.py`.

### Reading a decision's trace

With `NUDGEIQ_ENGINE=graph`, `GET /decisions/{decision_id}/trace` returns the route the decision took
through the graph, one entry per node, in the same shape as a variant run's trace:

    {"decision_id": "d_9f3c1a", "engine": "graph", "entries": [
      {"seq": 1, "node": "load_state", "attempt": 1, "status": "ok", "duration_ms": 1.8,
       "wrote": ["bucket", "decision_id", "journey", "state", "steps"], "notes": [], ...},
      {"seq": 2, "node": "entry_guardrail", ...}, ... {"seq": 8, "node": "finalize", ...}]}

- The route is the story: `load_state, finalize` means the journey was closed; a path through
  `no_candidates` means nothing legal was left; all eight nodes means a message was sent.
- `notes` are the same lines the Live page shows (`Decision.steps`), grouped by the node that wrote them.
- It is **not** part of the Decision. `GET /decisions/{id}` and `POST /decide` are unchanged.
- `entries` is empty with the `classic` engine, after a restart, or for a decision older than the newest
  2000 (it is held in memory). The endpoint is a 404 only when the decision itself does not exist.
- No node is retried here (a send must not repeat). If a node fails, the error is raised as before and
  the failed attempt is logged on `nudgeiq.graph`: `node=dispatch run=r_ab12 attempt=1 status=error ...`.
  When a decision succeeds, a line ties the run to its id: `run=r_ab12 is decision d_9f3c1a`.

Tests: `tests/test_decision_trace.py`.

## Logs, errors and request ids

- Every response carries an `X-Request-ID` header. Send your own (letters, digits, `.`, `_`, `-`) and it
  is reused; otherwise one is made. Quote it when reporting a problem, then search the logs for it.
- One log line per request: method, path, status, time, request id (health checks only at `DEBUG`).
- An unexpected error returns `500 {"detail": "internal server error", "request_id": "..."}`; the
  traceback goes to the log, never to the client.
- A lost database connection returns `503 {"detail": "database unavailable", ...}`.
- Docker: `docker compose logs -f backend`.

## Smoke check

With the server running:

    cd backend
    python -m app.smoke                  # read-only: health, campaigns, decisions, uplift
    python -m app.smoke --write          # also seeds 5 demo customers and runs one decision
    docker compose exec backend python -m app.smoke        # inside Docker

It prints PASS or FAIL per check and exits with 1 if any failed. `--write` adds fake data, so do not
use it on a database with real customers.

## Tests and checks (backend)

    cd backend
    pytest                 # the PostgreSQL tests are skipped unless NUDGEIQ_TEST_DSN is set
    ruff check .
    mypy app

Frontend:

    cd frontend
    npm run typecheck
    npm run build          # type check, then a production build into frontend/dist

`npm run build` makes static files only. The `/api` proxy exists in the dev server alone, so a real
deployment needs a reverse proxy to send `/api` to the backend. The Docker setup does this with
`frontend/nginx.conf`.

## Simulator and demos (no server needed)

    python -m app.simulator.run --customers 30000 --seed 1       # bandit vs random vs oracle
    python -m app.simulator.pipeline_demo                        # 6 sample customers through the agents
    python -m app.simulator.loop_demo                            # one full decide/outcome loop
    python -m app.simulator.full_run --customers 6000 --seed 1   # learner through real guardrails

All numbers come from fake data and placeholder config. They are not real business results.

## Background worker (Celery + Redis)

**8d-1** is the plumbing: a Redis queue, a Celery worker and a test task (`nudgeiq.ping`).
**8d-2** uses it for scheduled sends. **8d-3b** adds a `nudgeiq.process_outcome` task that rewards and learns
from one stored outcome. The API does not queue it yet: `POST /outcomes` still does the work itself (8d-3c). The
shared policy the outcomes learn into is already safe for several processes (8d-3a). See `backend/README.md`,
"Outcome processing".

### How scheduled sends work

With `NUDGEIQ_SCHEDULED_SENDS=1` (on in docker compose), a decision that sends a nudge does not
"send" it. It writes a row to the `scheduled_sends` table with the message and its send time.
`celery beat` wakes up every 30 seconds and asks the worker to deliver the rows that are due.

    API  --writes row-->  PostgreSQL  <--claims due rows--  worker  <--wake-up every 30 s--  beat (via Redis)

- **The schedule is in the database, not in Redis.** If Redis or the worker is down, the API keeps
  deciding and the rows wait; when they come back, due rows go out. (Checked live: Redis stopped,
  9 new decisions made, Redis restarted, all 9 delivered once.)
- **Never sent twice.** A worker takes a row by moving it from `scheduled` to `sending` in one
  database step, so two workers (or a repeated task) cannot both get it.
- **At most once.** If a worker dies after taking a row, that row becomes `failed` after 10 minutes
  and is **not** retried, because the message may already have gone out. Look at
  `GET /scheduled-sends?status=failed`.
- **Checked again at send time.** A send is skipped (status `skipped`, with a reason) if the customer
  unsubscribed or became uncontactable, lost the opt-in for that channel, or the journey already
  converted or exited. It is also skipped if it is found more than `NUDGEIQ_SEND_MAX_LATE_MINUTES`
  (default 120) after its time, so a worker that was down all night does not text people at 3 a.m.
- **Delivery today is a log line** (`SEND decision=... channel=...` in the worker log). A real SMS,
  push or email provider replaces the one function `log_deliver` in `app/worker/sends.py`.
- **Only with PostgreSQL.** With the switch on and no `NUDGEIQ_DATABASE_URL`, the API stops at
  start-up with a clear message. With the switch off, nothing changes from before.

Statuses: `scheduled` -> `sending` -> `sent` or `failed`; or `scheduled` -> `skipped`.
Read them with `GET /scheduled-sends` (optional `status` and `limit`) and
`GET /scheduled-sends/{decision_id}`.

Why not Celery's built-in delayed tasks? They hold a worker slot until the time comes (a T+3d send
would block everything behind it) and Redis re-delivers them after one hour. A table plus a
periodic check has neither problem.

### Running it without Docker

You need PostgreSQL and a Redis (for example `docker run -p 6379:6379 redis:7-alpine`). In each
terminal, set the same `NUDGEIQ_DATABASE_URL`:

    cd backend
    export NUDGEIQ_DATABASE_URL=postgresql://user:password@localhost:5432/nudgeiq
    export NUDGEIQ_SCHEDULED_SENDS=1
    uvicorn app.api.main:app                                     # terminal 1
    celery -A app.worker.celery_app worker --loglevel=INFO       # terminal 2
    celery -A app.worker.celery_app beat --loglevel=INFO         # terminal 3 (exactly one)
    python -m app.worker.check                                   # PASS or FAIL

To watch a send go out quickly, use `NUDGEIQ_DISPATCH_INTERVAL_SECONDS=5` and `/decide` with an
`as_of` in the past plus `NUDGEIQ_SEND_MAX_LATE_MINUTES=100000` (otherwise a send due days ago is
skipped as too late, which is the correct behaviour).

`check` fails in about a second with a plain message if Redis is down, and after `--timeout`
seconds (default 10) if Redis is up but no worker answers.

Tasks are acknowledged **after** they finish, so a worker that dies mid-task does not lose it,
which also means a task can run twice; the send guard above makes that safe. Messages are JSON
only (no pickle). Task results expire after one hour.

The tests need no Redis or PostgreSQL, except the ones skipped unless you set `NUDGEIQ_TEST_DSN`
(PostgreSQL) or `NUDGEIQ_TEST_REDIS` (for example `redis://localhost:6379/15`; use a spare database
number).

## Troubleshooting

- **"Backend not reachable"**: uvicorn is not running, or `VITE_API_TARGET` is wrong.
- **503 "no campaign is active"**: every campaign is archived or a draft. Activate one with
  `POST /campaigns/{id}/activate` (use `{"replace_active": true}` to swap).
- **`/decide` answers "wait: outside allowed hours"**: guardrails only allow 09:00-21:00 UTC. Pass
  `"as_of": "2026-11-02T10:00:00Z"` to pretend it is Monday morning.
- **Many customers are never nudged**: about 10% are holdout (control) by design.
- **Server will not start, mentions `NUDGEIQ_LLM`**: see the model settings above.
- **Data gone after restart**: you are on in-memory storage. See "Keeping your data".
- **Generate variants returns 502**: model call failed (bad key 401, rate limit 429, timeout). Nothing was
  stored; try again. Use `NUDGEIQ_LLM=mock` to work offline.
- **`check` says "could not reach Redis"**: Redis is not running, or `NUDGEIQ_REDIS_URL` points at the
  wrong host. In Docker the host is `redis`, not `localhost`.
- **Nudges stay `scheduled` and never go out**: no `beat` is running (it is what wakes the worker),
  or the worker has no `NUDGEIQ_DATABASE_URL`. Check `docker compose logs beat worker`. A send whose
  time was days ago is `skipped` as too late; that is intended.
- **API stops with "NUDGEIQ_SCHEDULED_SENDS is on"**: scheduled sends need PostgreSQL. Set
  `NUDGEIQ_DATABASE_URL` or turn the switch off.
- **`check` says "no answer ... is a worker running?"**: Redis is fine but no worker is listening on
  the `nudgeiq` queue. Start one, or look at `docker compose logs worker`.
- **Port 8000 or 5173 busy**: `uvicorn app.api.main:app --port 8001` plus `VITE_API_TARGET=http://127.0.0.1:8001`.

## Where to read more

- `backend/README.md`: models, agents, every endpoint, analytics and variant rules in detail.
- `frontend/README.md`: how the UI talks to the API.
- `frontend/API-CONTRACT-*.md`: endpoints the UI needs.
