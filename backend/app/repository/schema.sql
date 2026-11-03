-- NudgeIQ database schema (PostgreSQL 14+). Safe to run more than once.
-- Nested or flexible data (signals, scored candidates, trace steps, policy) is JSONB;
-- everything you will filter or aggregate on is a real column.

CREATE TABLE IF NOT EXISTS customers (
    customer_id           TEXT PRIMARY KEY,
    cohort_id             TEXT NOT NULL,
    cart_value            DOUBLE PRECISION NOT NULL CHECK (cart_value >= 0),
    abandoned_minutes     INTEGER NOT NULL CHECK (abandoned_minutes >= 0),
    timing_anchor_minutes INTEGER CHECK (timing_anchor_minutes >= 0),
    contactable           BOOLEAN NOT NULL DEFAULT TRUE,
    dnd                   BOOLEAN NOT NULL DEFAULT FALSE,
    has_app               BOOLEAN NOT NULL DEFAULT TRUE,
    sms_opt_in            BOOLEAN NOT NULL DEFAULT TRUE,
    email_opt_in          BOOLEAN NOT NULL DEFAULT TRUE,
    preferred_channel     TEXT,
    contacts_in_window    INTEGER NOT NULL DEFAULT 0 CHECK (contacts_in_window >= 0),
    last_contact_at       TIMESTAMPTZ,
    is_holdout            BOOLEAN NOT NULL DEFAULT FALSE,
    signals               JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every nudge sent or scheduled. Drives the contact cap and cool-down.
CREATE TABLE IF NOT EXISTS contacts (
    id          BIGSERIAL PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id) ON DELETE CASCADE,
    contact_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_customer_idx ON contacts (customer_id, contact_at);

-- One current journey per customer (a new trigger replaces it).
CREATE TABLE IF NOT EXISTS journeys (
    customer_id           TEXT PRIMARY KEY REFERENCES customers (customer_id) ON DELETE CASCADE,
    status                TEXT NOT NULL
                          CHECK (status IN ('active', 'converted', 'exited', 'suppressed')),
    started_at            TIMESTAMPTZ NOT NULL,
    base_abandoned_minutes INTEGER NOT NULL CHECK (base_abandoned_minutes >= 0),
    nudges_sent           INTEGER NOT NULL DEFAULT 0 CHECK (nudges_sent >= 0),
    closed_reason         TEXT,
    closed_at             TIMESTAMPTZ
);

-- The decision record: what was chosen and why.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id     TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers (customer_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL,
    is_holdout      BOOLEAN NOT NULL DEFAULT FALSE,
    explored        BOOLEAN NOT NULL DEFAULT FALSE,
    bucket          TEXT NOT NULL DEFAULT '',
    chosen_channel  TEXT,            -- NULL on all four chosen_* columns = "wait"
    chosen_timing   TEXT,
    chosen_offer    TEXT,
    chosen_lever    TEXT,
    scheduled_for   TIMESTAMPTZ,
    variant_id      TEXT,
    message         TEXT,
    state           JSONB NOT NULL,  -- customer snapshot at decision time
    candidates      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- top scored actions
    blocked         JSONB NOT NULL DEFAULT '[]'::jsonb,  -- action keys removed by guardrails
    steps           JSONB NOT NULL DEFAULT '[]'::jsonb   -- agent-by-agent trace
);
CREATE INDEX IF NOT EXISTS decisions_customer_idx ON decisions (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS decisions_created_idx ON decisions (created_at DESC);

-- What the customer did after a decision (many per decision).
CREATE TABLE IF NOT EXISTS outcomes (
    id           BIGSERIAL PRIMARY KEY,
    decision_id  TEXT NOT NULL REFERENCES decisions (decision_id) ON DELETE CASCADE,
    customer_id  TEXT NOT NULL REFERENCES customers (customer_id) ON DELETE CASCADE,
    outcome_type TEXT NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS outcomes_decision_idx ON outcomes (decision_id, occurred_at);
CREATE INDEX IF NOT EXISTS outcomes_customer_idx ON outcomes (customer_id, occurred_at);

-- One reward row per decision, replaced as more outcomes arrive.
CREATE TABLE IF NOT EXISTS rewards (
    decision_id TEXT PRIMARY KEY REFERENCES decisions (decision_id) ON DELETE CASCADE,
    customer_id TEXT NOT NULL REFERENCES customers (customer_id) ON DELETE CASCADE,
    points      INTEGER NOT NULL,
    reason      TEXT NOT NULL
);

-- The Master agent's learned values, so a restart does not erase learning.
CREATE TABLE IF NOT EXISTS policies (
    use_case_id TEXT PRIMARY KEY,
    policy      JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ======================================================================================
-- 5B: campaigns, message templates and variants (stored in the database, not JSON files)
-- ======================================================================================

-- A campaign is a stored use case. campaign_id equals the config's use_case_id, so the
-- learned policy (policies.use_case_id) and decisions point at the same id.
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    trigger_event TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'active', 'archived')),
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    config        JSONB NOT NULL,  -- the full UseCaseConfig
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at  TIMESTAMPTZ,
    archived_at   TIMESTAMPTZ,
    CHECK (status <> 'active' OR activated_at IS NOT NULL),
    CHECK (status <> 'archived' OR archived_at IS NOT NULL),
    CHECK (status = 'archived' OR archived_at IS NULL)
);
CREATE INDEX IF NOT EXISTS campaigns_status_idx ON campaigns (status);

-- Message templates belong to one campaign. seq keeps the order they were created in.
CREATE TABLE IF NOT EXISTS templates (
    campaign_id TEXT NOT NULL REFERENCES campaigns (campaign_id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    seq         BIGSERIAL,
    name        TEXT NOT NULL,
    channel     TEXT NOT NULL
                CHECK (channel IN ('push', 'email', 'sms', 'in_app', 'web', 'retargeting')),
    body        TEXT NOT NULL,
    offers      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- empty list = fits any offer
    PRIMARY KEY (campaign_id, template_id)
);

-- Variants: written by a person or an LLM; only 'approved' ones can ever be sent.
CREATE TABLE IF NOT EXISTS variants (
    campaign_id  TEXT NOT NULL,
    variant_id   TEXT NOT NULL,
    template_id  TEXT NOT NULL,
    seq          BIGSERIAL,
    text         TEXT NOT NULL,
    principle    TEXT NOT NULL,
    tone         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected')),
    source       TEXT NOT NULL DEFAULT 'human' CHECK (source IN ('human', 'llm')),
    generated_by TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at  TIMESTAMPTZ,
    review_note  TEXT,
    PRIMARY KEY (campaign_id, variant_id),
    FOREIGN KEY (campaign_id, template_id)
        REFERENCES templates (campaign_id, template_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS variants_status_idx ON variants (campaign_id, status);

-- Decisions now record which campaign version and template produced them. These columns
-- are added to the 5A table in place, so an existing database upgrades by re-running this file.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS use_case_id TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS campaign_version INTEGER
    CHECK (campaign_version >= 1);
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS template_id TEXT;
CREATE INDEX IF NOT EXISTS decisions_campaign_idx ON decisions (use_case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS decisions_variant_idx ON decisions (variant_id);

-- Scheduled sends (8d-2). One row per nudge decision; the worker delivers rows that are due.
-- Moving a row from 'scheduled' to 'sending' is the double-send guard.
CREATE TABLE IF NOT EXISTS scheduled_sends (
    decision_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    send_at     TIMESTAMPTZ NOT NULL,
    channel     TEXT NOT NULL,
    text        TEXT NOT NULL,
    variant_id  TEXT NOT NULL,
    template_id TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'sending', 'sent', 'skipped', 'failed')),
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL,
    claimed_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS scheduled_sends_due_idx ON scheduled_sends (status, send_at);

-- Policy safety (8d-3a). Several processes (the API, background workers) learn into the same
-- policy, so it carries a version that goes up on every change, and the policy remembers what
-- each decision has already added to it. Together they make learning safe to repeat and safe
-- to run side by side. 0 means "nothing stored yet"; existing rows start at 0 and that is fine.
ALTER TABLE policies ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;

-- What each decision's reward has already added to the policy. There is deliberately no
-- foreign key to decisions: this is an account of what the policy absorbed, and it must outlive
-- a deleted decision or a repeated task would learn the same reward again.
CREATE TABLE IF NOT EXISTS policy_credits (
    decision_id TEXT PRIMARY KEY,
    use_case_id TEXT NOT NULL,
    points      DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stored outcomes (8d-3b step 1). An outcome is saved the moment it arrives, with a status, and a
-- background task finishes the job. These columns are added to the existing table in place, so a
-- database upgrades by re-running this file.
--   * Rows that exist already were processed on arrival, so the status default is 'processed'
--     (a default of 'pending' would make the stuck-outcome sweep redo all of history). The
--     repository always sets the status explicitly; the default only fills in old rows.
--   * received_at is when WE got it (occurred_at is when the customer acted). Old rows get the
--     moment of this upgrade, which is harmless: they are not pending.
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'processed'
    CHECK (status IN ('pending', 'processed', 'failed'));
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0
    CHECK (attempts >= 0);
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
-- A repeated delivery with the same key is stored once. NULL keys (no key given) never collide.
CREATE UNIQUE INDEX IF NOT EXISTS outcomes_idempotency_idx ON outcomes (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- Small and fast however much history there is: only the rows still waiting are in it.
CREATE INDEX IF NOT EXISTS outcomes_pending_idx ON outcomes (received_at, id)
    WHERE status = 'pending';
