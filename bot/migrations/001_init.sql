-- ============================================================================
-- Telegram OTP & Verification System — Phase 1 schema
-- Migration: 001_init.sql
--
-- Applies the full baseline schema described in the architecture doc:
--   campaigns, promoters, otp_verifications, verification_events,
--   sms_delivery_logs, dashboard_users, plus supporting indexes and triggers.
--
-- Apply with:
--   psql "$DATABASE_URL" -f db/migrations/001_init.sql
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- campaigns
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL UNIQUE,              -- used in t.me/<bot>?start=<slug>
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused', 'closed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- promoters — one row per Telegram user who has interacted with the bot
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promoters (
    id                 SERIAL PRIMARY KEY,
    telegram_user_id   BIGINT NOT NULL UNIQUE,
    telegram_username  TEXT,
    full_name          TEXT,
    phone_number       TEXT,                          -- E.164, nullable until captured
    campaign_id        INTEGER REFERENCES campaigns(id),
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'otp_sent', 'verified', 'failed', 'blocked')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_promoters_campaign ON promoters(campaign_id);
CREATE INDEX IF NOT EXISTS idx_promoters_phone ON promoters(phone_number);
CREATE INDEX IF NOT EXISTS idx_promoters_status ON promoters(status);

-- Keep updated_at current on every row change
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_promoters_updated_at ON promoters;
CREATE TRIGGER trg_promoters_updated_at
BEFORE UPDATE ON promoters
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- otp_verifications — one row per OTP issuance attempt
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS otp_verifications (
    id                   SERIAL PRIMARY KEY,
    promoter_id          INTEGER NOT NULL REFERENCES promoters(id) ON DELETE CASCADE,
    otp_code_hash        TEXT NOT NULL,                -- HMAC-SHA256(phone + code, server secret); raw code never stored
    phone_number         TEXT NOT NULL,
    provider_message_id  TEXT,                          -- id returned by the SMS gateway
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'sent'
                         CHECK (status IN ('sent', 'verified', 'expired', 'failed')),
    expires_at           TIMESTAMPTZ NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_otp_promoter_status ON otp_verifications(promoter_id, status);
CREATE INDEX IF NOT EXISTS idx_otp_created_at ON otp_verifications(created_at);

-- ----------------------------------------------------------------------------
-- verification_events — append-only funnel/audit trail; drives analytics
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS verification_events (
    id            BIGSERIAL PRIMARY KEY,
    promoter_id   INTEGER NOT NULL REFERENCES promoters(id) ON DELETE CASCADE,
    campaign_id   INTEGER REFERENCES campaigns(id),
    event_type    TEXT NOT NULL
                  CHECK (event_type IN (
                      'started', 'phone_captured', 'otp_sent',
                      'otp_verified', 'otp_failed', 'otp_expired'
                  )),
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_campaign_type ON verification_events(campaign_id, event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON verification_events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_promoter ON verification_events(promoter_id);

-- ----------------------------------------------------------------------------
-- sms_delivery_logs — raw SMS gateway responses, for delivery debugging
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sms_delivery_logs (
    id                   SERIAL PRIMARY KEY,
    otp_verification_id  INTEGER REFERENCES otp_verifications(id) ON DELETE CASCADE,
    provider             TEXT NOT NULL,
    provider_status      TEXT,
    raw_response         JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sms_logs_otp ON sms_delivery_logs(otp_verification_id);

-- ----------------------------------------------------------------------------
-- dashboard_users — admin accounts for the web dashboard (separate from promoters)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dashboard_users (
    id             SERIAL PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'viewer'
                   CHECK (role IN ('admin', 'viewer')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Real-time notification: NOTIFY on every new verification_events row so the
-- dashboard can LISTEN on 'verification_events_channel' and push updates over
-- SSE without polling.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_verification_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('verification_events_channel', row_to_json(NEW)::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_verification_event ON verification_events;
CREATE TRIGGER trg_notify_verification_event
AFTER INSERT ON verification_events
FOR EACH ROW EXECUTE FUNCTION notify_verification_event();

-- ----------------------------------------------------------------------------
-- Optional (commented out): least-privilege roles per §6 of the architecture
-- doc. Uncomment and set real passwords via your secrets manager before
-- running in an environment where the bot and dashboard should not share
-- one Postgres role.
-- ----------------------------------------------------------------------------
-- CREATE ROLE bot_service LOGIN PASSWORD '<set via secrets manager>';
-- CREATE ROLE dashboard_service LOGIN PASSWORD '<set via secrets manager>';
--
-- GRANT SELECT, INSERT, UPDATE ON campaigns, promoters, otp_verifications,
--     verification_events, sms_delivery_logs TO bot_service;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bot_service;
--
-- GRANT SELECT ON campaigns, promoters, verification_events, sms_delivery_logs TO dashboard_service;
-- GRANT SELECT, INSERT, UPDATE ON dashboard_users TO dashboard_service;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dashboard_service;

COMMIT;
