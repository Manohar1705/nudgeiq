# NudgeIQ frontend (Zips 6 + 7A + 7C + steps 3, 4a, 4b and 5a)

React + TypeScript + Vite. Needs the backend from `nudgeiq-backend-step2.zip` or later.

    npm install
    npm run dev        # http://localhost:5173, forwards /api/* to http://127.0.0.1:8000

Start the backend first: `uvicorn app.api.main:app --reload`.

What works: the 7-step Configure wizard (real API), the Live Engine, and the Uplift tab.
Action Analytics works (step 4a, see `API-CONTRACT-4A.md`, it needs a new `GET /insights/messages` on the backend).
Uplift Analytics now follows the demo layout (step 5a, see `API-CONTRACT-5A.md`; the guardrail rates need a new `GET /insights/guardrails`).
User Analytics works (step 4b). It needs no new backend endpoint: the cohort distributions are computed
in the browser from `GET /customers` (plus any text or yes/no `signals`), the findings come from the
`/insights/uplift/by/*` rows, and the customer audit trail uses `GET /decisions` and `GET /decisions/{id}`.

## How the wizard saves (step 3)

- Steps 1-4 only change local state. Nothing touches the server.
- Opening **step 5 (templates)** clones the live campaign into a new **draft** (`<id>_draft_<time>`),
  because templates, variants and review only work on a draft. The clone keeps its templates and
  variants and starts with a fresh learner.
- **Generate** asks the model for up to 5 new variants (always `pending`). **Approve / Reject** are
  saved on the draft immediately, so they stay even if you leave before launching. A rejected
  variant cannot be approved again; generate a new one instead.
- **Launch** saves your choices onto the draft, runs validation, and only if there are no errors
  makes the draft live, **replacing the running campaign**. Validation errors are listed on the
  screen and nothing is activated.
- The cohort picked in step 1 is not saved: the campaign config has no target-cohort field.
- An unfinished wizard run leaves a draft campaign behind. Archive it from the API if you do not want it.
