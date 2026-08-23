-- ============================================================================
-- Telegram OTP & Verification System — Phase 4 schema
-- Migration: 002_add_customers.sql
--
-- Adds customer verification: a promoter collects a customer's phone number
-- (and optional name), an OTP goes to the CUSTOMER, and the resulting
-- verification is attributed to the PROMOTER who ran it.
--
-- Additive only — 001_init.sql is untouched. Apply with:
--   psql "$DATABASE_URL" -f db/migrations/002_add_customers.sql
-- (or the Node-based run-migration.js approach, pointed at this file)
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- customers — one row per unique phone number a promoter has verified,
-- deduped globally the same way promoters are deduped by telegram_user_id.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    id            SERIAL PRIMARY KEY,
    phone_number  TEXT NOT NULL UNIQUE,              -- E.164
    full_name     TEXT,                               -- optional, collected by the promoter
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'otp_sent', 'verified', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone_number);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);

-- Reuses the set_updated_at() function already defined in 001_init.sql.
DROP TRIGGER IF EXISTS trg_customers_updated_at ON customers;
CREATE TRIGGER trg_customers_updated_at
BEFORE UPDATE ON customers
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- customer_verifications — one row per OTP issuance attempt to a customer.
-- Mirrors otp_verifications, plus promoter_id: the promoter who ran it.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer_verifications (
    id                   SERIAL PRIMARY KEY,
    customer_id          INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    promoter_id          INTEGER NOT NULL REFERENCES promoters(id) ON DELETE CASCADE,
    otp_code_hash        TEXT NOT NULL,                -- HMAC-SHA256(phone + code, server secret)
    phone_number         TEXT NOT NULL,
    provider_message_id  TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'sent'
                         CHECK (status IN ('sent', 'verified', 'expired', 'failed')),
    expires_at           TIMESTAMPTZ NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_customer_verifications_customer_status
    ON customer_verifications(customer_id, status);
CREATE INDEX IF NOT EXISTS idx_customer_verifications_promoter
    ON customer_verifications(promoter_id);
CREATE INDEX IF NOT EXISTS idx_customer_verifications_created_at
    ON customer_verifications(created_at);

-- ----------------------------------------------------------------------------
-- verification_events — extend the existing funnel/audit table rather than
-- add a parallel one. promoter_id stays NOT NULL (every event is always
-- attributable to a promoter); customer_id is set only for customer_* event
-- types. The notify_verification_event() trigger from 001_init.sql needs NO
-- changes — row_to_json(NEW) picks up the new column automatically, so the
-- dashboard's existing LISTEN/NOTIFY/SSE pipeline starts carrying customer
-- events with zero code changes on the Postgres side.
-- ----------------------------------------------------------------------------
ALTER TABLE verification_events
    ADD COLUMN IF NOT EXISTS customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_events_customer ON verification_events(customer_id);

ALTER TABLE verification_events DROP CONSTRAINT IF EXISTS verification_events_event_type_check;
ALTER TABLE verification_events ADD CONSTRAINT verification_events_event_type_check
    CHECK (event_type IN (
        'started', 'phone_captured', 'otp_sent', 'otp_verified', 'otp_failed', 'otp_expired',
        'customer_started', 'customer_phone_captured', 'customer_otp_sent',
        'customer_otp_verified', 'customer_otp_failed', 'customer_otp_expired'
    ));

-- ----------------------------------------------------------------------------
-- sms_delivery_logs — let a delivery log row point at EITHER an
-- otp_verification (promoter self-verify) OR a customer_verification, never
-- both/neither. otp_verification_id was already nullable in 001_init.sql.
-- ----------------------------------------------------------------------------
ALTER TABLE sms_delivery_logs
    ADD COLUMN IF NOT EXISTS customer_verification_id INTEGER REFERENCES customer_verifications(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_sms_logs_customer_verification
    ON sms_delivery_logs(customer_verification_id);

ALTER TABLE sms_delivery_logs DROP CONSTRAINT IF EXISTS sms_delivery_logs_exactly_one_subject;
ALTER TABLE sms_delivery_logs ADD CONSTRAINT sms_delivery_logs_exactly_one_subject
    CHECK (
        (otp_verification_id IS NOT NULL AND customer_verification_id IS NULL)
        OR (otp_verification_id IS NULL AND customer_verification_id IS NOT NULL)
    );

COMMIT;
