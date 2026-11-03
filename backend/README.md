# NudgeIQ Backend (Zip 5B: campaigns, analytics, variant drafts and selection)

An in-house rebuild of an RL-driven next-best-action engine.

- Zip 1: project setup, shared data models, use-case config.
- Zip 2: fake-customer simulator, reward agent, Master bandit, milestone script.
- Zip 3: Guardrail, Channel/Timing/Offer, Data/State and Message agents.
- Zip 4: orchestrator (`decide`, `record_outcome`), full repository interface
  (in-memory and JSON-file), decision trace, FastAPI endpoints, and a second milestone that
  runs the learner through the real guardrails.
- Zip 5A (this one): the database. SQL tables (`app/repository/schema.sql`), a PostgreSQL
  repository that behaves exactly like the in-memory one, and tests that run every repository
  check against both.

## Prerequisites
- Python 3.12 or newer (3.14 works if all packages install; use the same version on every machine)

## Setup (Windows)
```
py -3.14 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pytest
ruff check .
mypy app
python -m app.simulator.run --customers 30000 --seed 1   # Zip 2 milestone
python -m app.simulator.pipeline_demo                    # Zip 3 demo with 6 sample customers
python -m app.simulator.loop_demo                        # Zip 4: one full loop, step by step
python -m app.simulator.full_run --customers 6000 --seed 1   # Zip 4 milestone (about 15 s)
uvicorn app.api.main:app --reload                        # API; then open http://127.0.0.1:8000/docs
```
On macOS/Linux: `python3 -m venv venv && source venv/bin/activate`.

## What is inside
```
app/
  models/      Pydantic models = the shared contract (send this folder to the DB teammate)
  config/      loader + use_cases/target_cart.json (placeholder values, edit freely)
  agents/      master.py (bandit), reward.py, action_space.py (full menu, used by the simulator)
               guardrail.py, channel.py, timing.py, offer.py, eligibility.py, candidates.py
               state_agent.py (Data/State), message.py
  repository/  base.py (interface), memory.py (in-memory version)
  config/templates/   message templates and variants (approved / pending / rejected)
  simulator/   customers.py (fake customers), responses.py (hidden behaviour rules), run.py (milestone)
  orchestrator/ orchestrator.py (decide, record_outcome, handle_event), dispatch.py, insights.py,
               analytics.py (uplift by channel, timing, offer, lever, cohort, variant)
  campaigns/   service.py (rules), validation.py (checks), generator.py (LLM variant drafts)
  llm/         base.py (client interface), mock.py (offline stand-in model)
  api/         main.py (FastAPI app)
  repository/  also jsonfile.py (set NUDGEIQ_DATA_FILE=data.json to keep data between restarts)
tests/         models, agents, repository, orchestrator, API and full-simulation tests
```

## The models (contract)
| Model | Purpose |
|---|---|
| `CustomerState` | What the agent sees. Fixed guardrail fields + a flexible `signals` dict (JSONB in Postgres) |
| `TriggerEvent` / `Outcome` | Events in, and what the customer did afterwards |
| `Action` | channel x timing x offer x lever; `Action.wait()` means do nothing |
| `Decision` | Full record of a decision: candidates, blocked actions, choice, explore flag, trace steps |
| `RewardRecord` | Points awarded for an outcome |
| `MessageTemplate` / `MessageVariant` | Templates with `{{vars}}`; only APPROVED variants may be sent |
| `UseCaseConfig` | Cohorts, signals, action space, rewards, guardrails, learning settings |

## Notes for the database teammate
- `CustomerState.signals`, `Decision.candidates`, `Decision.steps` are nested/flexible: use JSONB.
- Suggested tables: customers, decisions, outcomes, rewards, templates, variants, use_cases.
- Keep ids as strings (`customer_id`, `decision_id`). Timestamps are timezone-aware UTC.

## Rules
- Config values are placeholders. Nothing is hard-coded outside `app/config/`.
- Guardrails are hard rules; the learned policy can never override them.

## Zip 2: how the learning works
1. `customers.py` makes fake customers (cart value, loyalty, preferred channel, a 10% holdout control group).
2. `responses.py` holds hidden rules for how they react to a nudge: open -> click -> checkout restart -> order,
   plus an unsubscribe risk. The agent never sees these rules, only the outcomes.
3. `reward.py` turns outcomes into points using the config (order +40, and so on).
4. `master.py` buckets each customer (preferred channel, member or guest, cart size) and keeps a running
   reward estimate for every (bucket, action). It explores 10% of the time and otherwise picks the best.
5. `run.py` runs random choice and the learning agent on the same customers and compares them,
   plus a perfect 'oracle' as an upper bound and the holdout group as the control for uplift.

The sample output should show the learning agent clearly above random and a positive uplift.
All numbers come from fake data and placeholder config; they are not real business results.

## Zip 3: how the agents fit together
```
StateAgent        builds the customer state (only signals the business switched on) + contact history
GuardrailAgent    entry rule -> contact cap -> per action: cool-down, hours, weekdays, journey window
Channel / Timing / Offer   propose only options this customer is eligible for
generate_candidates        proposes, drops actions with no approved message, then applies guardrails
MasterAgent       picks one legal candidate (or waits when none are left)
MessageAgent      picks an APPROVED variant that fits the channel and offer (uniform, or epsilon-greedy if the campaign asks), fills the {{variables}}
```
- Guardrails run outside the learned policy and cannot be overridden by it.
- Eligibility rules are named in the config (`action_space.eligibility`); the rules live in `eligibility.py`.
  A rule that needs a context signal fails closed if the business switched that signal off.
- Message variants must be approved by a person. SMS variants must contain STOP. Unknown `{{variables}}`
  in an approved variant are rejected when the agent starts.
- Holdout customers are chosen by a stable hash of the customer id, so a customer never switches groups.

## Changes to the Zip 1 models (tell the database teammate)
- `CustomerState`: new fields `has_app`, `sms_opt_in`, `email_opt_in` (all default true).
- `ContextSignal`: new `fields` list (which `signals` keys that context signal provides).
- `ActionSpace`: new `eligibility` map. `timings` are now validated (e.g. `T+1h`, `T+3d`).
- `MessageTemplate`: new `offers` list (empty = fits any offer). New `TemplateLibrary` model.

## Known simplifications
- Allowed hours are checked in UTC, not the customer's local time.
- By default the message is chosen at random among approved variants that fit. A campaign can switch on an epsilon-greedy choice (see "Variant selection").
- The `MasterAgent` bandit does not use the new candidate list inside the simulator yet (that arrives with the orchestrator).

## Not yet in this zip
Orchestrator (`decide` / `record_outcome`), decision and outcome storage, API, PostgreSQL, LLM variant generation.

## Next
Zip 4: orchestrator, repository for decisions and outcomes, FastAPI endpoints.

## Database (PostgreSQL 14+)
```
docker compose up -d                                   # local PostgreSQL (or use your own)
set NUDGEIQ_DATABASE_URL=postgresql://nudgeiq:nudgeiq@localhost:5432/nudgeiq
python -m app.repository.init_db                       # creates the tables (safe to repeat)
uvicorn app.api.main:app --reload                      # the API now stores everything in Postgres
```
On macOS/Linux use `export` instead of `set`. Without `NUDGEIQ_DATABASE_URL` the API uses
`NUDGEIQ_DATA_FILE` (JSON file) or plain memory, as before.

**Tables:** `customers`, `contacts` (every nudge sent or scheduled), `journeys` (one current per
customer), `decisions` (the record: chosen action, message, trace), `outcomes`, `rewards` (one
row per decision, replaced as outcomes arrive) and `policies` (the Master's learned values).
Things you filter on are real columns; nested data (signals, scored candidates, trace steps,
policy) is JSONB. Foreign keys and CHECK constraints are enforced by the database.
`decisions` can be grouped directly in SQL, for example
`SELECT chosen_channel, count(*) FROM decisions GROUP BY 1`.

**Running the tests against PostgreSQL:** create an empty test database and set
`NUDGEIQ_TEST_DSN=postgresql://nudgeiq:nudgeiq@localhost:5432/nudgeiq_test`. The tests EMPTY that
database before each test, so never point it at real data. Without the variable, the 40
PostgreSQL tests are skipped and everything else still runs.

## API (try each one in /docs)
| Endpoint | What it does |
|---|---|
| `POST /demo/seed` | Create fake customers so you have something to decide on |
| `POST /customers`, `GET /customers/{id}` | Add or read a customer (stand-in for the real source) |
| `POST /events` | A `cart_abandoned` trigger: starts a fresh journey and decides |
| `POST /decide` | Decide for one customer. Optional `as_of` pretends it is another time |
| `POST /outcomes` | Report what the customer did (`order_placed`, `ignored` = timeout, ...) |
| `GET /decisions`, `GET /decisions/{id}` | Decision trace, outcomes and reward |
| `GET /decisions/{id}/trace` | The graph engine's per-node trace of one decision (empty with `classic`; see the root README) |
| `GET /insights/uplift` | Treated vs holdout conversion. Optional `campaign_id` limits it to one campaign |
| `GET /insights/uplift/by/{dimension}` | Uplift per channel, timing, offer, lever, cohort or variant, with a confidence interval (see "Analytics" below) |
| `GET /campaigns`, `GET /campaigns/{id}` | Stored campaigns with status and version. Create, edit, activate and the rest: see "Campaign endpoints" below |

Guardrails only allow sends between 09:00 and 21:00 UTC. If `/decide` answers "wait" with
"outside allowed hours", pass `"as_of": "2026-11-02T10:00:00Z"` (a Monday morning).
About 10% of customers are holdout (control): they are never nudged, by design.

## How learning works
- Each decision stores its segment (`bucket`). Outcomes for it can arrive one by one; the
  reward is recomputed from all of them and the Master is corrected by the difference, so one
  decision counts as one trial.
- A timeout (`ignored`) only counts when nothing else happened, and returns `next_step: "retry"`.
- The learned values live in the database (`policies`), so a restart keeps them. Learning goes
  through `repo.credit_policy`, not `save_policy` (see "Policy safety" below).
- After a nudge was sent, a retry measures `T+1h`, `T+24h` from now instead of from the
  abandonment (`CustomerState.timing_anchor_minutes = 0`).

## Changes to the models since Zip 3 (tell the database teammate)
- `Decision`: new `bucket` (string) and `scheduled_for` (datetime, nullable).
- `CustomerState`: new `timing_anchor_minutes` (int, nullable).
- `UseCaseConfig`: new `trigger_event` (string, default `cart_abandoned`).
- New models in `app/models/journey.py`: `Journey` (one current journey per customer) and `OutcomeResult`.
- The tables now exist: see `app/repository/schema.sql`. Review it, and tell me what to change.
- The full interface is `app/repository/base.py` (`Repository`). Tables needed: customers,
  contacts, journeys, decisions, outcomes, rewards (one row per decision, upserted), policies.

## Known simplifications
- Guardrail hours are still UTC.
- One campaign is live per server; `wait` is never chosen by the Master, only forced by rules.
- One database connection is shared by the API (fine for development; a connection pool
  comes with hardening).
- Dispatch only logs. Nothing is actually sent.
- The in-memory repository returns stored objects, not copies; treat them as read-only.

## Campaign validation (5B-3)

`app/campaigns/validation.py` checks a campaign before it is saved or activated. It is
pure: no database, no network. It returns a report of issues instead of raising on the
first one.

```python
from app.campaigns import validate_campaign, validate_draft

report = validate_campaign(config, library)  # parsed objects
report = validate_draft(config_dict, library_dict)  # raw dicts from the dashboard
report.errors, report.warnings, report.can_activate
```

- **ERROR** blocks activation. A draft can still be saved with errors.
- **WARNING** allows activation but is shown to the user.
- Each issue has a stable `code`, a field `path` and a readable `message`.

What it checks: schema (with field paths), ids, cohorts, the action space, eligibility rule
names, guardrails, rewards, template variables, the SMS STOP line, SMS length, and whether
each channel and offer has an approved variant. Content rules are shared with the message
agent (`KNOWN_VARIABLES`, `has_sms_opt_out`), so send-time and activation checks agree.

A channel with no approved variant is a warning, not an error. At least one approved
variant overall is required.

## Campaign service (5B-4)

`app/campaigns/service.py` holds the lifecycle rules. The repository stores, the validator
checks, and `CampaignService(repo)` decides what is allowed. It has no HTTP code; the
endpoints in 5B-6 will call it.

```python
from app.campaigns import CampaignService

svc = CampaignService(repo)
svc.create_draft(config)  # may still have validation errors
svc.update_config(id, new_config)  # drafts only; version + 1
svc.save_template(id, t)
svc.save_variant(id, v)  # drafts only
copy = svc.clone(id, "new_id")  # new draft with templates and variants
svc.validate(id)  # full report, any status
svc.activate(id)  # needs a report with no errors
svc.activate(id, replace_active=True)  # swaps the live campaign
svc.archive(id)
svc.get_active()
```

Rules:
- Only a **draft** can be edited. To change a live campaign, clone it, edit the copy, then
  activate the copy with `replace_active=True`.
- A draft can be saved while invalid. **Activation** is the gate: validation errors block it,
  warnings do not.
- **One active campaign** at a time. Activating a second one fails unless `replace_active=True`.
  Validation runs before anything changes, and the old campaign is archived first, so a
  failure never leaves two live campaigns.
- **Archived is final.** Clone it to reuse it.
- `version` goes up on every saved edit of the settings. Activating, archiving and changing
  templates or variants leave it alone.
- A clone starts as a draft at version 1 and does not copy the learned policy.
- Failures raise subclasses of `CampaignError` (`CampaignNotFoundError`,
  `CampaignExistsError`, `CampaignStateError`, `ActiveCampaignExistsError`,
  `CampaignIdMismatchError`, `ActivationBlockedError`). The last one carries the full
  validation report as `.report`.

## Campaign runtime (5B-5a)

`app/api/runtime.py` holds the orchestrator for the live campaign and swaps it when a
different campaign goes live, without a restart. It is not wired into the API yet (5B-5b).

```python
from app.api.runtime import CampaignRuntime, NoActiveCampaignError

rt = CampaignRuntime(repo)  # builds its own CampaignService
orch = rt.current()  # live campaign's orchestrator; raises NoActiveCampaignError
rt.activate("spring", replace_active=True)  # service.activate + reload together
rt.archive("spring")  # service.archive + reload
rt.reload()  # re-read which campaign is live
rt.orchestrator_for_decision(id)  # the campaign that made this decision
```

- **Explicit reload.** The runtime does not poll the database. Call `reload()` (or use
  `activate()` / `archive()` here) after the live campaign changes. Reloading when nothing
  changed keeps the same orchestrator.
- **Nothing live** clears the orchestrator; `current()` raises `NoActiveCampaignError`, which
  the API can turn into a 503.
- **Decisions are stamped** with `use_case_id` and `campaign_version`. An outcome for an old
  decision is routed to the campaign that made it, so it is rewarded with that campaign's rules
  and teaches that campaign's learner, not the new live one.
- **One dispatcher** is shared across swaps.
- The new orchestrator is built before the old one is replaced. If building fails the error
  propagates and the previous orchestrator keeps serving.
- Each campaign's learned policy is saved under its own id, so a swap never mixes them.

## API now runs on the campaign runtime (5B-5b)

`create_app` no longer loads the JSON files on every start. It uses the repository:

- **First start only.** If the repository holds no campaigns at all, the JSON use case
  (`use_case_id`, default `target_cart`) is copied in as the active campaign. If any campaign
  exists, nothing is seeded, so an edit is never undone and two active campaigns can't appear.
- **Serving.** `/events`, `/decide` and `/demo/seed` use the live campaign through
  `app.state.runtime`. `/outcomes` goes to the campaign that made the decision, so an outcome
  that arrives after a swap is rewarded by that campaign, not the new one.
- **Nothing live** (all archived, or only drafts): `/events`, `/decide` and `/demo/seed`
  answer **503** `no campaign is active`. `/health` answers 200 with
  `{"status": "no_active_campaign"}`. Reading endpoints keep working.
- **`/campaigns`** now lists stored campaigns with `status`, `version` and `active`;
  `/campaigns/{id}` returns the stored config (404 if unknown).
- `app.state.runtime` and `app.state.service` are what the 5B-6 endpoints will use.
  Swap campaigns with `runtime.activate(id, replace_active=True)`; do not change the live
  campaign through the service alone, or the server keeps serving the old one until reload.
- `GET /insights/uplift` pools every campaign unless you pass `campaign_id` (5B-7).

## Campaign endpoints (5B-6)

`app/api/campaigns.py` exposes the campaign service over HTTP. It only translates requests and
errors; the rules are in `CampaignService`, and activate and archive go through the runtime so
the server serves the new live campaign immediately.

| Endpoint | What it does |
|---|---|
| `POST /campaigns` | Create a draft from a `UseCaseConfig`. 201 with the campaign and its validation report |
| `PUT /campaigns/{id}` | Replace a draft's settings (version + 1). 409 if not a draft |
| `POST /campaigns/{id}/clone` | `{"new_id": "...", "name": "..."}` copies any campaign, with templates and variants, into a new draft |
| `POST /campaigns/{id}/activate` | Optional body `{"replace_active": true}`. Needs a report with no errors |
| `POST /campaigns/{id}/archive` | Retire a draft or the live campaign (final) |
| `GET /campaigns/{id}/validation` | Full validation report for the stored campaign |
| `POST /campaigns/validate` | Dry run on raw JSON `{"config": {...}, "library": {...}}`. Saves nothing; schema errors come back with field paths |
| `GET /campaigns/{id}/templates`, `PUT .../templates/{template_id}` | List, save (drafts only) |
| `GET /campaigns/{id}/variants?template_id=&status=`, `PUT .../variants/{variant_id}` | List and filter, save (drafts only). A human-written variant may still be saved as approved; an LLM-written one may not (see "Variant review") |
| `POST /campaigns/{id}/variants/generate` | Ask the model for new PENDING variants of one template (drafts only; see "Variant drafts") |
| `POST /campaigns/{id}/variants/{variant_id}/approve`, `.../reject` | Review a variant (drafts only). Optional body `{"note": "..."}` |

**Status codes:** 404 unknown id; 409 the campaign's status or an existing id does not allow it
(editing a live campaign, activating a second one without `replace_active`, creating a duplicate
id); 422 the request is wrong (schema error, id in the body not matching the URL) or activation
was blocked by validation (the body has `detail` and the full `validation` report); 503 nothing is
live (decision endpoints only).

**A draft with validation errors is saved.** The response carries the report, and only
activation is blocked by it. A config that does not parse at all is a 422 and is not saved.

The intended way to change a live campaign: clone it, edit the copy, check
`GET /campaigns/{copy}/validation`, then `POST /campaigns/{copy}/activate` with
`{"replace_active": true}`.

Not covered yet: there is no delete (archive instead), and approving a variant on a live campaign
is refused because only drafts can be edited. That comes with the LLM-draft step (5B-8).

## Analytics (5B-7)

`app/orchestrator/analytics.py` breaks uplift down by one dimension at a time:
`GET /insights/uplift/by/{dimension}` where dimension is `channel`, `timing`, `offer`, `lever`,
`cohort` or `variant`.

Query parameters: `campaign_id` (only that campaign's decisions; 404 if unknown),
`campaign_version` (needs `campaign_id`), `min_resolved` (default 30).

Each slice holds the nudged decisions that share one value, compared with the holdout group:

- **Counting.** Same rules as `/insights/uplift`: only decisions with a recorded outcome
  ("resolved") enter a rate, and "wait" decisions are left out.
- **Baseline.** Holdout customers are never nudged, so they have no channel, offer and so on.
  Action slices are compared with all holdout customers; cohort slices with the holdout of the
  same cohort.
- **Result per slice.** `uplift_abs` (treated minus holdout conversion), `uplift_rel` (that
  divided by the holdout rate; `null` when the holdout rate is 0), and a 95% interval
  `ci_low`..`ci_high` on `uplift_abs` (Newcombe-Wilson, which behaves well with small groups).
- **Not enough data.** `enough_data` is false unless both groups have at least `min_resolved`
  resolved decisions. `significant` is true only with enough data and an interval that excludes
  zero. Small groups still return their numbers, but never count as significant.
- **Variants.** Variant ids are only unique inside one campaign, so a report across all
  campaigns names them `campaign:variant`. Decisions with no variant show as `(none)`.
- **Read it as a signal, not proof.** The Master chose each action instead of assigning it at
  random, so a slice that beats the holdout may simply have been picked for easier customers.
- **Cost.** It reads every decision in one pass. That is fine for demo-sized data; very large
  tables will want a database-side aggregate later.

## Variant review (5B-8a)

A variant is sendable only after a person approves it. These endpoints (and the matching
`CampaignService.approve_variant` / `reject_variant`) are how that happens.

- `POST /campaigns/{id}/variants/{variant_id}/approve` and `.../reject`, optional body
  `{"note": "..."}` (max 500 characters; a blank note is stored as none). The response is
  `{"variant": ..., "warnings": [...]}`; warnings are always empty for a rejection.
- **Drafts only.** Like every other edit, a live or archived campaign answers 409. To change the
  live campaign's variants: clone it, review the copy, then activate it with `replace_active`.
- **Approve** works on a `pending` variant only (409 otherwise). It is refused with 422, and
  nothing is saved, when the variant would fail validation as an approved one: unknown
  `{{variable}}`, blank text, or an SMS without STOP. The body lists the issues. Warnings, such as
  an SMS that needs more than one text message, do not block; they come back in `warnings`. This
  is the same check validation runs on approved variants (`variant_approval_issues`), so approving
  and activating cannot disagree.
- **Reject** works on a `pending` variant, or withdraws an `approved` one. Rejecting a rejected
  variant is a 409.
- Both set `reviewed_at` and `review_note`. There is no login in this project, so who reviewed is
  not recorded.
- **LLM variants cannot skip review.** `PUT .../variants/{id}` refuses (422) to save a variant with
  `source: "llm"` as `approved`, unless it is already approved with the same text (so a re-save
  with only a tone or principle change works). A reworded LLM variant has to be saved as `pending`
  and approved again. An LLM variant also cannot be relabelled as human-written. Human-written
  variants can still be saved as approved directly, as before.
- Approving or rejecting does not change the campaign `version`.

## Variant drafts with a language model (5B-8b)

`POST /campaigns/{id}/variants/generate` asks a language model to write alternative wordings of
one template and stores them as variants for a person to review (5B-8a).

Body: `{"template_id": "...", "count": 3, "principles": [], "tones": []}`. `count` is 1 to 5
(default 3). `principles` and `tones` are optional hints such as `["social_proof"]`.

Response: `{"model", "requested", "created": [variants], "skipped": [{"text", "code", "message"}]}`.

- **Always pending.** Every created variant has `status: "pending"`, `source: "llm"` and
  `generated_by` set to the client's name. Any status the model claims is ignored. A person still
  approves it with the approve endpoint.
- **Drafts only.** A live or archived campaign answers 409, an unknown campaign or template 404,
  and a bad body 422. In each of these cases the model is not called.
- **The model's output is not trusted.** The reply is read as JSON (code fences and chatter
  around it are tolerated). A draft is skipped, with its reason in `skipped`, when it would fail
  validation as an approved variant (`unknown_variable`, `empty_body`, `sms_missing_stop`), is
  over 1000 characters (`too_long`), is not an object with a text (`invalid_item`), repeats an
  existing wording of that template or an earlier draft in the same reply (`duplicate`), or is
  past `count` (`over_limit`). One bad draft never fails the batch.
- **No repeats.** Existing variants of the template count as taken in every status, so a wording
  a person rejected does not come back. The most recent twelve are also shown to the model.
- **Ids** are `<template_id>_llm_<n>`, using the lowest unused n. Principles and tones are
  stored as lowercase snake_case labels; a missing one becomes `unspecified`.
- **Model down or unusable reply:** 502 and nothing is stored.

**Mock mode.** `app/llm/mock.py` is an offline stand-in that needs no key or network. It adds one
closing line to the template body, so its drafts use only the template's own placeholders and keep
any SMS STOP. It follows the requested principles and tones, skips wordings that already exist, and
runs out after about 15 new wordings per template (5 principles x 3 lines). `create_app` uses it by
default. It is for development, demos and tests, not for real campaign copy.

**Plugging in a real model.** A client is any object with a `name` and a
`complete(request) -> str` method that returns the model's raw reply or raises `LLMError`
(`LLMRequest` carries the `system` text, the `prompt` and the structured `brief`).
`create_app(repo, llm=my_client)` uses it. The real client is described next.

## Using a real model (free providers)

`app/llm/openai_compat.py` calls any OpenAI-style `/chat/completions` API with `httpx` (already in
`requirements.txt`). Groq, Google AI Studio (Gemini) and OpenRouter all work, and the mock stays the
default. `app/llm/fallback.py` tries several in order.

| Variable | Meaning |
|---|---|
| `NUDGEIQ_LLM` | unset or `mock`: offline mock. `groq`, `gemini`, `openrouter`: one provider. `chain`: Groq, then Gemini, then OpenRouter, then the mock |
| `GROQ_API_KEY`, `NUDGEIQ_GROQ_MODEL`, `NUDGEIQ_GROQ_MODEL_2` | Groq key, main model, optional second model |
| `GEMINI_API_KEY`, `NUDGEIQ_GEMINI_MODEL` | Google AI Studio key and model |
| `OPENROUTER_API_KEY`, `NUDGEIQ_OPENROUTER_MODEL` | OpenRouter key and model (`:free` models cost nothing) |
| `NUDGEIQ_LLM_TIMEOUT` | seconds to wait for each answer. Default 60 |

Model names change often, so none are built in: copy the current names from each console.

```
set NUDGEIQ_LLM=chain
set GROQ_API_KEY=your-key
set NUDGEIQ_GROQ_MODEL=current-model-name
uvicorn app.api.main:app --reload
```

On macOS/Linux use `export`. Then `POST /campaigns/{id}/variants/generate` on a draft campaign;
the response's `model` and each variant's `generated_by` show which model wrote them (`mock` means
every real provider failed and the offline stand-in answered).

- **Fails early.** A provider without its key or model, an unknown provider, or a bad timeout
  stops the server at start-up with a clear message.
- **Chain mode** skips a provider that fails at run time (rate limit, blocked network, bad key) and
  moves to the next, ending in the mock.
- **The key** is sent only in the `Authorization` header. It is not stored, logged, printed in an
  error message, or shown in the client's `repr`. Keep it out of the repository.
- **Failures** in a single-provider mode (no network, timeout, non-200, empty or cut-off reply)
  become a `502` with a short reason, and nothing is stored. No retries; call generate again.
- **What the real client does not change.** Its output goes through the same parsing, validation,
  duplicate filtering and pending-only rules as the mock's, and a person still approves everything.
- **Tests** use a fake HTTP transport, so no key or network is needed, and they clear these
  variables first. The live API has not been called from this project's tests: do one generate
  call with your key as the first check.

## Variant selection (5B-9)

The Message agent chooses among **approved** variants that fit the action's channel and offer.
Pending and rejected variants are never candidates. How it chooses is a per-campaign setting in
the config's `learning` block, so it is versioned with the campaign like every other rule:

```json
"learning": {
  "variant_selection": "uniform",
  "variant_epsilon": 0.1,
  "variant_min_resolved": 30
}
```

- **`uniform`** (default): every approved option is equally likely. Same behaviour as before.
- **`epsilon_greedy`**:
  1. *Warm-up*: any option with fewer than `variant_min_resolved` resolved decisions is picked
     first (uniformly among those), so a new variant gets a fair look.
  2. *Explore*: otherwise, with probability `variant_epsilon`, pick uniformly among all options.
  3. *Exploit*: otherwise pick the option with the highest conversion (ties at random).
- **Data used**: decisions of this campaign **and this version** that stored a `variant_id`, and
  have at least one outcome ("resolved", same rule as analytics). Decisions still waiting for a
  response are not counted as failures. A new version starts from zero, because its wording may
  have changed. The numbers are re-read every 50 selections, not on every send.
- **Trace**: each send's decision record now says how the variant was picked, for example
  `used approved variant v_push_warm (exploit: best conversion 18.0% among 2 variants)`, and
  `template_id` is now stored on the decision (the column already existed but was never filled).
- **No approved variant** for a channel and offer: unchanged. That action is not offered, so the
  customer waits. The system does not fall back to a template's raw body, because that text has not
  been reviewed and could break the SMS `STOP` rule.

Limits to know about: conversion is compared across all variants that fit the action, even if they
belong to different templates, and it is not adjusted for which cohort or timing the Master chose
for them (see the caveat under "Analytics"). The bandit state lives in the database through the
stored decisions, so it survives a restart and a campaign swap. Thompson sampling and per-cohort
selection are not built.

## Policy safety (8d-3a)
Several processes (the API, later background workers) learn into the same stored policy, so
learning had to become safe to repeat and safe to run side by side. Before this, each process
kept the whole policy in memory and overwrote the stored copy after every outcome: two
processes lost each other's learning, and the same outcome delivered twice was counted twice.

- **`repo.credit_policy(use_case_id, decision_id, bucket, action_key, points, legacy_points=None)`**
  adds one decision's reward, atomically and at most once. The policy remembers what each
  decision has already added (`policy_credits`, no foreign key on purpose, so the record outlives
  a deleted decision). A first call adds a try and the points; a later call with different points
  moves the total by the difference; a later call with the same points changes nothing.
  PostgreSQL locks the policy row for the change, so a second writer waits and then sees the
  first one's result. A writer that holds the lock for more than 10 seconds
  (`POLICY_LOCK_TIMEOUT`) makes the others fail instead of hanging.
- **Versions**: `policies.version` goes up on every change. `repo.policy_version()` is a cheap
  read, `repo.read_policy()` returns the policy with its version. `0` means "nothing stored";
  an older database or JSON snapshot starts at 1.
- **The orchestrator** checks the version before every decision and reloads the policy when
  another process changed it. After crediting it adopts the policy the repository returns.
- **Bad data**: `PolicyError` for a malformed stored policy (negative count, NaN total, missing
  field) and for a reward that is not a finite number. Nothing is changed when it is raised. At
  start-up it stops the process. While running, a policy that has gone bad is logged once per
  version and ignored: decisions go on with the policy already in memory.
- **`save_policy`** still replaces the whole policy (admin or reset) and bumps the version. It
  does not validate, so a bad policy can be stored; readers refuse it.
- A `MasterAgent` that already learned something, given to an empty repository, is stored at
  start-up so the first credit adds to it.
- `policy_credits` grows by one small row per learned decision. Nothing prunes it yet.
- Tests: `tests/test_policy_safety.py` (85, each repository test runs on memory and PostgreSQL).

## Outcome storage (8d-3b, step 1 of 3)
An outcome can now be saved the moment it arrives and processed later, so it is not lost while a
worker is down. This step is storage only: no worker code, no new endpoint, and `POST /outcomes`
behaves exactly as before. The next steps are the Celery task (step 2) and the sweep for stuck
outcomes plus tests with a real queue (step 3).

- **Status.** Every stored outcome is `pending` (saved, not rewarded yet), `processed`, or
  `failed` (can never be processed, kept with its reason). Outcomes that existed before this step
  were processed on arrival, so they become `processed`: the column default is `'processed'` on
  purpose, because a `pending` default would make the sweep redo all of history. The repository
  always sets the status itself; the default only fills in old rows.
- **`repo.save_outcome(outcome, *, status="processed", idempotency_key=None, received_at=None)`**
  now returns a `SavedOutcome(record, created)`. Callers that ignored the old `None` are
  unaffected. With a key, a repeat returns the stored record with `created=False` (one row, even
  from two processes at once: a unique index and `ON CONFLICT`). A key reused for a different
  decision, customer or outcome type raises `OutcomeKeyConflict` instead of dropping the second
  outcome. Without a key nothing is deduplicated, as before.
- **`OutcomeRecord`** is the outcome plus `outcome_id`, `status`, `idempotency_key`,
  `received_at`, `processed_at`, `attempts` and `last_error`. `received_at` is when we got it, not
  `occurred_at` (when the customer acted): the sweep will use it to tell how long something has
  been waiting.
- **New repository methods**: `get_outcome(id)`, `mark_outcome(id, status, at, error="")`,
  `count_outcome_attempt(id, error="")` (for retryable errors) and
  `list_pending_outcomes(received_by, limit=100)`. Unknown ids raise `KeyError`.
- **`list_outcomes(..., status=None)`** gained an optional status filter. The default still
  returns every status, so a pending outcome is listed, and so counted by the reward calculation,
  the journey exit check and the analytics ("resolved" decisions). Nothing creates pending
  outcomes yet, so nothing changes today. Who must filter before step 2 is switched on is an open
  decision, pinned by a test so it is changed on purpose.
- **`Orchestrator.accept_outcome(outcome, idempotency_key=None)`** checks the outcome (unknown
  decision: `DecisionNotFoundError`, other customer's decision: `OutcomeMismatchError`, as in
  `record_outcome`), stores it as `pending` and stops: no reward, no learning, no journey change.
  The check was moved into a shared `_check_outcome`.
- **Not changed on purpose.** `record_outcome` still stores its outcome as `processed` before it
  has finished the work, as it always did. Step 2 splits it into accept and apply.
- **Upgrade.** `schema.sql` adds the columns with `ADD COLUMN IF NOT EXISTS` and two indexes
  (a unique one on the key, a small partial one on pending rows), so re-running it upgrades an
  existing database. The JSON snapshot now stores records; a snapshot written before this step
  (bare outcomes) still loads, as `processed`.
- Tests: `tests/test_outcome_storage.py` (62: each repository test runs on memory and PostgreSQL,
  plus a schema upgrade test, real two-connection concurrency, and the JSON snapshot).

## Outcome processing (8d-3b, step 2 of 3)
A stored `pending` outcome can now be turned into a reward, learning and a journey update by a
background task. The API does not use it yet: `POST /outcomes` still does all the work inside the
request, so nothing a client sees has changed. Queueing from the API is 8d-3c. The sweep for
outcomes that stay pending is step 3.

- **One class does the work.** `app/orchestrator/outcomes.py` has `OutcomeProcessor.apply(decision,
  outcome, now)`: reward, learning (`credit_policy`), unsubscribe, journey. It needs the repository
  and the campaign's config, not a full `Orchestrator`. `Orchestrator.record_outcome` now calls it,
  so the API and the worker run the same code. The journey helpers (`journey_for`,
  `minutes_since_abandonment`) moved there too; the orchestrator's own methods still exist and
  delegate.
- **`record_outcome` stores `pending` first.** It saves the outcome as `pending`, does the work, and
  marks it `processed` last. A crash in between leaves it `pending` for the sweep. Before, it was
  stored as `processed` before the work started. The result returned is unchanged.
- **Which outcomes count.** Rewards and the journey exit check read `pending` and `processed`
  outcomes together, in one query (`repo.list_outcome_records(..., statuses=...)`, new, memory and
  PostgreSQL). `pending` counts because the outcome being processed is itself still pending, and so
  are the ones stored beside it. `failed` never counts: it could not be processed, so it must not
  change a reward or close a journey.
- **The analytics count `processed` only (decision made for step 2).** `uplift_summary`,
  `uplift_by`, `message_funnel` and `guardrail_summary` now read `list_outcomes(status="processed")`.
  An outcome that is still waiting for the worker is therefore not in these numbers yet, and
  neither is one that stays `pending` or `failed`. With only the synchronous path nothing differs,
  because every outcome is `processed` by the time the request returns. `GET /decisions/{id}` still
  lists outcomes of every status.
- **The reward is settled.** After saving a reward, the processor reads the outcomes again. If
  another outcome for the same decision arrived meanwhile (a second worker), it saves the newer
  reward, up to 5 rounds. So a slow worker cannot leave an older, lower reward behind.
- **Safe to repeat.** Redelivery, a second worker, or a retry after a crash between the work and the
  `processed` mark change nothing the second time: the reward is recomputed, `credit_policy` adds
  only the difference, and the unsubscribe and journey close are overwrites.
- **The task.** `nudgeiq.process_outcome(outcome_id)` in `app/worker/tasks.py`, with the logic in
  `app/worker/outcomes.py` (plain Python, no Celery). `enqueue_outcome(outcome_id)` queues it and
  returns `False` instead of raising when the queue is down, because the outcome is already stored.
  The campaign comes from the decision (`decision.use_case_id`), as in the API, so an outcome is
  rewarded by the campaign that made the decision. A decision from before campaigns were stamped
  uses the one live campaign.
- **Time for the journey rules** is the outcome's `occurred_at` (as in the synchronous path), not
  the moment the worker gets to it. A late worker does not end a journey early.

What happens to one try:

| Situation | What the worker does |
|---|---|
| Already `processed` | Nothing (a repeated delivery is harmless). |
| Already `failed` | Nothing. A person puts it back to `pending` (`mark_outcome`) to try again. |
| Outcome id unknown | Logs it. Nothing to mark and nothing to retry. |
| Decision or campaign not found, outcome of another customer, malformed stored policy, invalid data | **Permanent.** Marked `failed` with the reason. Not retried, the try is not counted. |
| Lock timeout, deadlock, serialization failure | Retried after 5, 10, 20 ... 300 s. The try is counted. |
| Lost or closed database connection | Same, and the worker closes its connection and opens a new one next time. |
| Any other error | Retried like the above (it may be a bug a deploy fixes). |
| `attempts` reaches 8 | Marked `failed`: "gave up after 8 tries: ...". |
| The database cannot even count the try | Retried without counting. Celery stops after 12 retries and the outcome stays `pending` for the sweep. |

`attempts` and `last_error` are in the database, so the count survives a restart and a redelivery.

- **Not done.** `POST /outcomes` does not queue anything. There is no sweep of stuck `pending`
  outcomes (step 3), so an outcome whose task was lost stays `pending` until then. There is no
  command to put many `failed` outcomes back to `pending` (for example after repairing a bad
  policy); `repo.mark_outcome(id, "pending", now)` does one.
- **No new environment variables, tables or compose changes.** The existing worker already
  imports `app.worker.tasks`, where the task is registered.
- Tests: `tests/test_outcome_processing.py` (each repository-backed test runs on memory and
  PostgreSQL; 3 need `psycopg`) and `tests/test_worker_outcomes.py` (needs `celery`, no Redis).

### How step 2 was checked
Step 2 was first written with no packages installed. It was later run (see "Step 3" below):
its test files pass on memory, after fixing the test fixtures (`activated_at` / `archived_at`
that the `Campaign` model requires).

## Outcome sweep and background `POST /outcomes` (8d-3b, step 3)

**The sweep** (`app/worker/outcomes.py`, task `nudgeiq.sweep_outcomes`). Every 60 seconds `celery
beat` starts it. It finds outcomes still `pending` after the grace period and queues them again.
This is what keeps an outcome from staying pending for ever when Redis was down at the time, or a
worker died and the queue lost the message. Repeating is safe: a second delivery does nothing.
- `NUDGEIQ_OUTCOME_SWEEP_GRACE_SECONDS`: default 600, minimum 60. Longer than the longest retry
  delay (300 s), so an outcome only waiting for its retry is not queued a second time.
- At most 100 per pass, oldest first. If the queue is down the pass stops and the next one retries.
- Known limit: a queued outcome stays `pending` until a worker finishes it, so with a badly
  backed-up queue the next sweep can queue it again. The duplicate does nothing.

**`POST /outcomes` in background mode** (`app/api/main.py`). Switch: `NUDGEIQ_ASYNC_OUTCOMES`
(`1`/`true`/`yes`/`on`; default off = unchanged behaviour, answer 200 with the reward).
- On: the outcome is checked and stored as `pending`, a worker is asked to process it, and the
  answer is **202** `{outcome_id, decision_id, status, queued, duplicate}`. No reward is in the
  answer; read it later from `GET /decisions/{id}`.
- Header `Idempotency-Key` (max 200 chars): a retried request is stored once (`duplicate: true`).
  The same key for a different decision, customer or outcome type answers **409**. Unknown
  decision 404, wrong customer 422, as before.
- Queue unreachable: still 202 with `queued: false`. The outcome is stored, the sweep queues it.
- A repeated key whose outcome is still `pending` is queued again (the first message may have
  been lost); one already `processed` or `failed` is not.
- On without PostgreSQL stops start-up (the worker reads outcomes from the database).
- The API picks up what the worker learned at its **next decision**, so `/health`'s
  `cells_learned` can lag until then.
- Needs `NUDGEIQ_REDIS_URL` on the backend (compose sets it) and a running `worker` and `beat`.

**Not done:** the health-check count of stuck outcomes (optional), a separate outcomes queue, and
the frontend (no screen shows outcomes).

### How step 3 was checked
- **Run here:** `pytest` full suite 760 passed, 467 skipped (PostgreSQL and Redis variants);
  `ruff check` and `mypy app` clean. The new tests use a stand-in queue and call the worker's
  function directly. Two older test files fail `ruff format --check`; they are not changed here.
- **NOT run:** anything against real PostgreSQL or Redis, a real worker or beat, `docker compose
  up`, and a live `curl` of the 202 path. Please run: `NUDGEIQ_TEST_DSN=... pytest`, then
  compose with `NUDGEIQ_ASYNC_OUTCOMES=1`, post an outcome with an `Idempotency-Key`, and watch
  it go `pending` to `processed`; stop the worker, post, restart it, and watch the sweep.
