-- ============================================================================
-- Migration 003: Phase 3 — conversations, messages, outbox, order approvals
-- Distributor Operations Agent — WhatsApp conversation persistence
--
-- Applied manually like 001/002 (no migration runner):
--   psql "$SUPABASE_DB_URL" -f src/app/database/migrations/20261118000000_phase3_conversations.sql
--
-- Additive only: no existing table or constraint is rewritten.
--
-- ----------------------------------------------------------------------------
-- BUSINESS CONSTRAINTS ENFORCED HERE
--   * One permanent conversation per WhatsApp number: conversations.phone
--     is globally unique and E.164-checked (same shape as customers.phone).
--   * conversations.customer_id is NULL for unknown senders: an unknown
--     number gets a conversation row (message retention) but never a fake
--     customer row. The link is set lazily by the application once a
--     registered customer sends from that number. ON DELETE SET NULL keeps
--     the conversation when the customer row is deleted.
--   * Message transcript protocol mirrors app.agents.schemas.AgentMessage:
--     role user/assistant/tool, assistant text (content) XOR tool_calls,
--     tool results carry tool_call_id. Enforced in the application layer;
--     the CHECKs below keep the storage shape sound (direction/role pairs,
--     external_message_id uniqueness for inbound idempotency).
--   * Inbound idempotency: UNIQUE (messages.external_message_id). The
--     claim is INSERT ... ON CONFLICT DO NOTHING RETURNING: one atomic
--     statement decides which webhook delivery processes a message.
--   * Outbox delivery states: pending → sending → sent | failed | unknown
--     (unknown is NOT failed: the outcome may still be either).
--   * Order approval lifecycle: pending_delivery → active → consumed.
--     UNIQUE (order_id): one approval row per order, ever.
--     'active' only after the summary message delivery is confirmed;
--     'consumed' is a guarded single-use transition.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. conversations (one permanent row per WhatsApp number)
-- ----------------------------------------------------------------------------
CREATE TABLE conversations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone          text        NOT NULL UNIQUE,               -- E.164, same shape as customers.phone
    customer_id    uuid        REFERENCES customers(id) ON DELETE SET NULL,
    state          text        NOT NULL DEFAULT 'active'
                   CONSTRAINT conversations_state_allowed
                       CHECK (state IN ('active', 'needs_human', 'human_active')),
    handoff_reason text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT conversations_phone_e164 CHECK (phone ~ '^\+[1-9][0-9]{1,14}$')
);

CREATE INDEX conversations_customer_idx ON conversations (customer_id);   -- FK + listing
CREATE INDEX conversations_state_idx    ON conversations (state);         -- operator queue

-- ----------------------------------------------------------------------------
-- 2. messages (full transcript: inbound user, agent tool turns, outbound text)
-- ----------------------------------------------------------------------------
CREATE TABLE messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid   NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq                 bigint GENERATED ALWAYS AS IDENTITY,
    direction           text   NOT NULL
                        CONSTRAINT messages_direction_allowed
                            CHECK (direction IN ('inbound', 'agent', 'outbound')),
    role                text   NOT NULL
                        CONSTRAINT messages_role_allowed
                            CHECK (role IN ('user', 'assistant', 'tool')),
    content             text,
    tool_call_id        text,
    tool_calls          jsonb,   -- [{id, name, arguments}] assistant tool-call turn
    external_message_id text,    -- WhatsApp message key id, inbound idempotency
    status              text
                        CONSTRAINT messages_status_allowed
                            CHECK (status IN ('unread', 'read')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_direction_role CHECK (
        (direction = 'inbound'  AND role = 'user')
     OR (direction = 'agent'    AND role IN ('assistant', 'tool'))
     OR (direction = 'outbound' AND role = 'assistant')
    ),
    CONSTRAINT messages_inbound_user CHECK (direction != 'inbound' OR status IS NOT NULL),
    CONSTRAINT messages_external_id_inbound
        CHECK (direction = 'inbound' OR external_message_id IS NULL)
);

CREATE UNIQUE INDEX messages_external_id_uniq ON messages (external_message_id);
CREATE INDEX messages_conversation_seq_idx ON messages (conversation_id, seq);

-- ----------------------------------------------------------------------------
-- 3. outbox_messages (outbound delivery intent, persisted before sending)
-- ----------------------------------------------------------------------------
CREATE TABLE outbox_messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      uuid NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    body            text NOT NULL CHECK (length(btrim(body)) > 0),
    status          text NOT NULL DEFAULT 'pending'
                    CONSTRAINT outbox_status_allowed
                        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      text,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX outbox_status_created_idx ON outbox_messages (status, created_at);
CREATE INDEX outbox_conversation_idx   ON outbox_messages (conversation_id);

-- ----------------------------------------------------------------------------
-- 4. order_approvals (approval lifecycle bound to one exact order)
-- ----------------------------------------------------------------------------
CREATE TABLE order_approvals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    order_id            uuid NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    delivery_message_id uuid NOT NULL REFERENCES outbox_messages(id) ON DELETE CASCADE,
    status              text NOT NULL DEFAULT 'pending_delivery'
                        CONSTRAINT order_approvals_status_allowed
                            CHECK (status IN ('pending_delivery', 'active', 'consumed')),
    activated_at        timestamptz,
    consumed_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT order_approvals_status_timestamps CHECK (
        (status = 'pending_delivery' AND activated_at IS NULL AND consumed_at IS NULL)
     OR (status = 'active'          AND activated_at IS NOT NULL AND consumed_at IS NULL)
     OR (status = 'consumed'        AND activated_at IS NOT NULL AND consumed_at IS NOT NULL)
    )
);

CREATE INDEX order_approvals_conversation_status_idx
    ON order_approvals (conversation_id, status);
CREATE INDEX order_approvals_delivery_idx
    ON order_approvals (delivery_message_id);

-- ----------------------------------------------------------------------------
-- updated_at maintenance (new mutable tables, same trigger as 001)
-- ----------------------------------------------------------------------------
CREATE TRIGGER trg_conversations_upd BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_outbox_messages_upd BEFORE UPDATE ON outbox_messages
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_order_approvals_upd BEFORE UPDATE ON order_approvals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
