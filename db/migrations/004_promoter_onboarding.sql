-- Track whether a promoter has completed the bot's name/city onboarding
-- questions. Backfilled to created_at for existing rows so promoters who
-- already went through the old flow (Telegram profile name, admin-set city)
-- aren't asked again.
ALTER TABLE promoters ADD COLUMN IF NOT EXISTS onboarding_completed_at timestamptz;

UPDATE promoters SET onboarding_completed_at = created_at WHERE onboarding_completed_at IS NULL;
