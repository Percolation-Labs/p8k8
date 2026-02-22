-- =============================================================================
-- 04_payments.sql — Stripe payment tables
--
-- Three tables for Stripe integration: customer mapping, payment intent
-- tracking, and idempotent webhook event log.
--
-- Run AFTER 01_install_entities.sql (depends on uuid-ossp extension).
-- =============================================================================


-- ---------------------------------------------------------------------------
-- stripe_customers — maps p8 users to Stripe customer IDs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stripe_customers (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                 UUID NOT NULL,
    tenant_id               VARCHAR(100),
    stripe_customer_id      VARCHAR(255) NOT NULL UNIQUE,
    email                   VARCHAR(255),
    plan_id                 VARCHAR(50) NOT NULL DEFAULT 'free',
    subscription_status     VARCHAR(50) NOT NULL DEFAULT 'active',
    stripe_subscription_id  VARCHAR(255),
    current_period_end      TIMESTAMPTZ,
    metadata                JSONB DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at              TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_stripe_customers_user_tenant
    ON stripe_customers (user_id, tenant_id) WHERE deleted_at IS NULL;

-- updated_at trigger
CREATE OR REPLACE FUNCTION stripe_customers_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = CURRENT_TIMESTAMP; RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stripe_customers_updated_at ON stripe_customers;
CREATE TRIGGER trg_stripe_customers_updated_at
    BEFORE UPDATE ON stripe_customers
    FOR EACH ROW EXECUTE FUNCTION stripe_customers_updated_at();


-- ---------------------------------------------------------------------------
-- payment_intents — tracks every Stripe PaymentIntent
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS payment_intents (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                     UUID NOT NULL,
    tenant_id                   VARCHAR(100),
    stripe_payment_intent_id    VARCHAR(255) NOT NULL UNIQUE,
    stripe_customer_id          VARCHAR(255),
    amount                      INTEGER NOT NULL,
    currency                    VARCHAR(10) NOT NULL DEFAULT 'usd',
    status                      VARCHAR(50) NOT NULL DEFAULT 'requires_payment_method',
    description                 TEXT,
    metadata                    JSONB DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payment_intents_user_created
    ON payment_intents (user_id, created_at DESC);

-- updated_at trigger
CREATE OR REPLACE FUNCTION payment_intents_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = CURRENT_TIMESTAMP; RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_payment_intents_updated_at ON payment_intents;
CREATE TRIGGER trg_payment_intents_updated_at
    BEFORE UPDATE ON payment_intents
    FOR EACH ROW EXECUTE FUNCTION payment_intents_updated_at();


-- ---------------------------------------------------------------------------
-- webhook_events — idempotent Stripe webhook event log
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_events (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    stripe_event_id     VARCHAR(255) NOT NULL UNIQUE,
    event_type          VARCHAR(100) NOT NULL,
    payload             JSONB NOT NULL,
    processed           BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


-- ---------------------------------------------------------------------------
-- usage_tracking — metered resource usage per billing period
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS usage_tracking (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    tenant_id       VARCHAR(100),
    resource_type   VARCHAR(30) NOT NULL,    -- 'chat_tokens' | 'dreaming_minutes' | 'web_searches_daily'
    period_start    DATE NOT NULL,           -- first of month or day for daily resources
    used            BIGINT NOT NULL DEFAULT 0,
    granted_extra   BIGINT NOT NULL DEFAULT 0,  -- add-on credits
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_tracking_user_resource_period
    ON usage_tracking (user_id, resource_type, period_start);

-- updated_at trigger
CREATE OR REPLACE FUNCTION usage_tracking_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = CURRENT_TIMESTAMP; RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_usage_tracking_updated_at ON usage_tracking;
CREATE TRIGGER trg_usage_tracking_updated_at
    BEFORE UPDATE ON usage_tracking
    FOR EACH ROW EXECUTE FUNCTION usage_tracking_updated_at();


-- ---------------------------------------------------------------------------
-- usage_increment() — atomic upsert + limit check (avoids races)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION usage_increment(
    p_user_id       UUID,
    p_resource_type VARCHAR,
    p_amount        BIGINT,
    p_limit         BIGINT,
    p_period_start  DATE DEFAULT date_trunc('month', CURRENT_DATE)::date
)
RETURNS TABLE(new_used BIGINT, effective_limit BIGINT, exceeded BOOLEAN)
LANGUAGE plpgsql AS $$
DECLARE
    v_used   BIGINT;
    v_extra  BIGINT;
    v_limit  BIGINT;
BEGIN
    INSERT INTO usage_tracking (user_id, resource_type, period_start, used)
    VALUES (p_user_id, p_resource_type, p_period_start, p_amount)
    ON CONFLICT (user_id, resource_type, period_start)
    DO UPDATE SET used = usage_tracking.used + p_amount
    RETURNING usage_tracking.used, usage_tracking.granted_extra
    INTO v_used, v_extra;

    v_limit := p_limit + v_extra;

    RETURN QUERY SELECT v_used, v_limit, (v_used > v_limit);
END;
$$;
