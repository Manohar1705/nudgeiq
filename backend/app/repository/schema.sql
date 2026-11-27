-- -- Drop existing tables to ensure a clean slate. CASCADE handles dependent objects.
-- DROP TABLE IF EXISTS experiments CASCADE;
-- DROP TABLE IF EXISTS rewards CASCADE;
-- DROP TABLE IF EXISTS purchases CASCADE;
-- DROP TABLE IF EXISTS customer_events CASCADE;
-- DROP TABLE IF EXISTS messages CASCADE;
-- DROP TABLE IF EXISTS campaign_decisions CASCADE;
-- DROP TABLE IF EXISTS model_versions CASCADE;
-- DROP TABLE IF EXISTS customer_consents CASCADE;
-- DROP TABLE IF EXISTS customer_preferences CASCADE;
-- DROP TABLE IF EXISTS campaigns CASCADE;
-- DROP TABLE IF EXISTS promotions CASCADE;
-- DROP TABLE IF EXISTS products CASCADE;
-- DROP TABLE IF EXISTS brands CASCADE;
-- DROP TABLE IF EXISTS customers CASCADE;

-- Create tables in the correct order to satisfy foreign key constraints

CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    first_name VARCHAR NOT NULL,
    last_name VARCHAR NOT NULL,
    email VARCHAR UNIQUE NOT NULL,
    phone_number VARCHAR,
    address VARCHAR,
    whatsapp_number VARCHAR,
    facebook_handle VARCHAR,
    instagram_handle VARCHAR,
    twitter_handle VARCHAR,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS brands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR NOT NULL UNIQUE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(id),
    name VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    price FLOAT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS promotions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR NOT NULL,
    description TEXT,
    discount_percentage FLOAT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id UUID NOT NULL REFERENCES brands(id),
    product_id UUID NOT NULL REFERENCES products(id),
    promotion_id UUID REFERENCES promotions(id),
    name VARCHAR NOT NULL,
    description TEXT,
    start_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    end_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    budget FLOAT,
    max_audience INTEGER,
    status VARCHAR DEFAULT 'draft',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS customer_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id),
    interests TEXT,
    communication_channel VARCHAR,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS customer_consents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id),
    email_marketing BOOLEAN DEFAULT FALSE,
    whatsapp_marketing BOOLEAN DEFAULT FALSE,
    instagram_marketing BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS model_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    description TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS campaign_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES campaigns(id),
    customer_id UUID NOT NULL REFERENCES customers(id),
    model_version_id UUID REFERENCES model_versions(id),
    relevance_score FLOAT,
    selection_reason VARCHAR,
    action VARCHAR,
    timing VARCHAR,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES campaign_decisions(id),
    channel VARCHAR NOT NULL,
    content TEXT,
    status VARCHAR,
    sent_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS customer_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id),
    campaign_id UUID REFERENCES campaigns(id),
    decision_id UUID REFERENCES campaign_decisions(id),
    event_type VARCHAR NOT NULL,
    event_data TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id),
    product_id UUID NOT NULL REFERENCES products(id),
    campaign_id UUID REFERENCES campaigns(id),
    purchase_date TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    purchase_amount FLOAT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS rewards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES campaign_decisions(id),
    reward_value FLOAT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR NOT NULL,
    model_version_id UUID NOT NULL REFERENCES model_versions(id),
    start_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    end_date TIMESTAMP WITHOUT TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

-- ---- NudgeIQ engine tables (the agent's own campaigns, templates, variants, policies) ----
-- engine_campaigns must come after campaigns, because it references it.

CREATE TABLE IF NOT EXISTS engine_campaigns (
    campaign_id   TEXT PRIMARY KEY,
    business_campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    trigger_event TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'active', 'archived')),
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    config        JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at  TIMESTAMPTZ,
    archived_at   TIMESTAMPTZ,
    CHECK (status <> 'active' OR activated_at IS NOT NULL),
    CHECK (status <> 'archived' OR archived_at IS NOT NULL),
    CHECK (status = 'archived' OR archived_at IS NULL)
);
CREATE INDEX IF NOT EXISTS engine_campaigns_status_idx ON engine_campaigns (status);

CREATE TABLE IF NOT EXISTS templates (
    campaign_id TEXT NOT NULL REFERENCES engine_campaigns (campaign_id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    seq         BIGSERIAL,
    name        TEXT NOT NULL,
    channel     TEXT NOT NULL
                CHECK (channel IN ('push', 'email', 'sms', 'in_app', 'web', 'retargeting')),
    body        TEXT NOT NULL,
    offers      JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (campaign_id, template_id)
);

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

CREATE TABLE IF NOT EXISTS policies (
    use_case_id TEXT PRIMARY KEY,
    policy      JSONB NOT NULL,
    version     BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---- NudgeIQ engine tables, part 2 (customers, decisions, outcomes, rewards, sends) ----
-- engine_customers and engine_rewards have new names because his customers and rewards
-- tables already use the old names. customer_id holds the id of the Admin customer, as text.

CREATE TABLE IF NOT EXISTS engine_customers (
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
    customer_id TEXT NOT NULL REFERENCES engine_customers (customer_id) ON DELETE CASCADE,
    contact_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_customer_idx ON contacts (customer_id, contact_at);

-- One current journey per customer (a new trigger replaces it).
CREATE TABLE IF NOT EXISTS journeys (
    customer_id            TEXT PRIMARY KEY REFERENCES engine_customers (customer_id) ON DELETE CASCADE,
    status                 TEXT NOT NULL
                           CHECK (status IN ('active', 'converted', 'exited', 'suppressed')),
    started_at             TIMESTAMPTZ NOT NULL,
    base_abandoned_minutes INTEGER NOT NULL CHECK (base_abandoned_minutes >= 0),
    nudges_sent            INTEGER NOT NULL DEFAULT 0 CHECK (nudges_sent >= 0),
    closed_reason          TEXT,
    closed_at              TIMESTAMPTZ
);

-- The decision record: what was chosen and why.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id      TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL REFERENCES engine_customers (customer_id) ON DELETE CASCADE,
    created_at       TIMESTAMPTZ NOT NULL,
    is_holdout       BOOLEAN NOT NULL DEFAULT FALSE,
    explored         BOOLEAN NOT NULL DEFAULT FALSE,
    bucket           TEXT NOT NULL DEFAULT '',
    chosen_channel   TEXT,            -- NULL on all four chosen_* columns = "wait"
    chosen_timing    TEXT,
    chosen_offer     TEXT,
    chosen_lever     TEXT,
    scheduled_for    TIMESTAMPTZ,
    variant_id       TEXT,
    message          TEXT,
    state            JSONB NOT NULL,  -- customer snapshot at decision time
    candidates       JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocked          JSONB NOT NULL DEFAULT '[]'::jsonb,
    steps            JSONB NOT NULL DEFAULT '[]'::jsonb,
    use_case_id      TEXT,
    campaign_version INTEGER CHECK (campaign_version >= 1),
    template_id      TEXT
);
CREATE INDEX IF NOT EXISTS decisions_customer_idx ON decisions (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS decisions_created_idx ON decisions (created_at DESC);
CREATE INDEX IF NOT EXISTS decisions_campaign_idx ON decisions (use_case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS decisions_variant_idx ON decisions (variant_id);

-- What the customer did after a decision (many per decision).
CREATE TABLE IF NOT EXISTS outcomes (
    id              BIGSERIAL PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES decisions (decision_id) ON DELETE CASCADE,
    customer_id     TEXT NOT NULL REFERENCES engine_customers (customer_id) ON DELETE CASCADE,
    outcome_type    TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'processed'
                    CHECK (status IN ('pending', 'processed', 'failed')),
    idempotency_key TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS outcomes_decision_idx ON outcomes (decision_id, occurred_at);
CREATE INDEX IF NOT EXISTS outcomes_customer_idx ON outcomes (customer_id, occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS outcomes_idempotency_idx ON outcomes (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS outcomes_pending_idx ON outcomes (received_at, id)
    WHERE status = 'pending';

-- One reward row per decision, replaced as more outcomes arrive.
CREATE TABLE IF NOT EXISTS engine_rewards (
    decision_id TEXT PRIMARY KEY REFERENCES decisions (decision_id) ON DELETE CASCADE,
    customer_id TEXT NOT NULL REFERENCES engine_customers (customer_id) ON DELETE CASCADE,
    points      INTEGER NOT NULL,
    reason      TEXT NOT NULL
);

-- Scheduled sends. One row per nudge decision; the worker delivers rows that are due.
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

-- What each decision's reward has already added to the policy (no foreign key on purpose).
CREATE TABLE IF NOT EXISTS policy_credits (
    decision_id TEXT PRIMARY KEY,
    use_case_id TEXT NOT NULL,
    points      DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);