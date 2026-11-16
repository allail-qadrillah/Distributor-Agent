-- ============================================================================
-- Migration 002: customers.credit_limit becomes nullable, no default
-- Distributor Operations Agent — database v0.2
--
-- Apply via psql (same procedure as migration 001):
--   psql "$SUPABASE_DB_URL" -f src/app/database/migrations/002_credit_limit_nullable.sql
--
-- ----------------------------------------------------------------------------
-- WHY
-- Business rule (approved 2026-11-16): NULL and 0 mean different things.
--   NULL = credit limit not configured -> confirmation rejected
--          (CreditLimitNotConfiguredError). Never "unlimited".
--   0    = limit configured as zero    -> any positive order rejected
--          (CreditLimitExceededError).
-- Migration 001's NOT NULL DEFAULT 0 collapsed both states into 0, so
-- "unconfigured" was unrepresentable.
--
-- WHAT
--   * DROP NOT NULL on credit_limit: NULL becomes storable.
--   * DROP DEFAULT: INSERTs that omit credit_limit store NULL (no
--     implicit zero). Explicit 0 stays valid.
--   * No data changes: existing values — including existing zeros — are
--     preserved exactly. No UPDATE, no rewrite.
--   * CHECK (credit_limit >= 0) and all other columns/constraints
--     untouched (NULL passes the CHECK by SQL semantics).
-- ============================================================================

ALTER TABLE customers ALTER COLUMN credit_limit DROP NOT NULL;
ALTER TABLE customers ALTER COLUMN credit_limit DROP DEFAULT;
