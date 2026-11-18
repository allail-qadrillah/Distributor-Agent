-- ============================================================================
-- Migration 004: Phase 3 remediation — outbox claim lease (claimed_at)
-- Distributor Operations Agent
--
-- Applied manually like 001/002/003 (no migration runner):
--   psql "$SUPABASE_DB_URL" -f src/app/database/migrations/20261118000100_phase3_1_outbox_claim_lease.sql
--
-- Additive only: no existing table or constraint is rewritten, no data is
-- deleted, and the live database is NOT assumed to be empty.
--
-- ----------------------------------------------------------------------------
-- BUSINESS CONSTRAINT ENFORCED HERE
--   * outbox_messages.claimed_at is the claim lease: every dispatch claim
--     stamps it. A 'sending' row whose lease is older than the configured
--     stale window was a dispatcher that died mid-send: its outcome cannot
--     be determined, so recovery reconciles it to 'unknown' (never resent
--     automatically, only resolved by a delivery report).
--   * Existing 'sending' rows (claimed before this migration) are
--     backfilled with claimed_at = updated_at so they enter the stale
--     window immediately instead of being stuck forever.
-- ============================================================================

ALTER TABLE outbox_messages ADD COLUMN claimed_at timestamptz;

UPDATE outbox_messages
SET claimed_at = updated_at
WHERE status = 'sending' AND claimed_at IS NULL;

-- recovery sweep lookup: only 'sending' rows matter, and few of them
CREATE INDEX outbox_sending_claimed_idx
    ON outbox_messages (claimed_at)
    WHERE status = 'sending';
