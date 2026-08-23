-- ============================================================================
-- Telegram OTP & Verification System — Phase 5 schema
-- Migration: 003_promoter_roster.sql
--
-- Adds an admin-managed promoter roster (allowlist), so that:
--   - Only Telegram accounts an admin has explicitly registered can complete
--     /start and use the bot (INDOMIE requirement: access tied to
--     registered accounts, revocable without engineering involvement).
--   - A promoter's city is tracked (INDOMIE's 28 promoters span 12 cities).
--
-- How it works:
--   - An admin runs /addpromoter <telegram_username> <city> [campaign_slug]
--     BEFORE the promoter ever messages the bot. This inserts an
--     'allowed_promoters' row with status='active'.
--   - When that Telegram username later runs /start, main.py checks this
--     table (case-insensitive, '@' stripped) before creating/activating the
--     promoters row. No match (or status='revoked') -> bot politely refuses
--     and tells them to contact their administrator.
--   - /removepromoter <telegram_username> sets status='revoked'. This does
--     NOT delete verification history — it just blocks future bot use by
--     flipping promoters.status to 'blocked' (existing CHECK constraint
--     already allows 'blocked') and revoking the roster entry, so all past
--     verified customers/records stay intact and attributed correctly.
--
-- Additive only — 001_init.sql / 002_add_customers.sql are untouched.
-- Apply with:
--   psql "$DATABASE_URL" -f db/migrations/003_promoter_roster.sql
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS allowed_promoters (
    id                  SERIAL PRIMARY KEY,
    telegram_username   TEXT NOT NULL UNIQUE,   -- stored lowercase, no leading '@'
    city                TEXT,
    campaign_id         INTEGER REFERENCES campaigns(id),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'revoked')),
    added_by_admin_id   BIGINT,                  -- Telegram user id of the admin who added them
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_allowed_promoters_status ON allowed_promoters(status);
CREATE INDEX IF NOT EXISTS idx_allowed_promoters_campaign ON allowed_promoters(campaign_id);

DROP TRIGGER IF EXISTS trg_allowed_promoters_updated_at ON allowed_promoters;
CREATE TRIGGER trg_allowed_promoters_updated_at
BEFORE UPDATE ON allowed_promoters
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Track which city each active promoter is in (helps the "28 promoters /
-- 12 cities" reporting requirement). Nullable/additive — existing rows are
-- unaffected.
ALTER TABLE promoters ADD COLUMN IF NOT EXISTS city TEXT;

COMMIT;
