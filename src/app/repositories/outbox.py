"""Outbox repository — SQL for the outbox_messages table.

Outbox delivery state machine (policy lives in the messaging layer):
pending -> sending (atomic claim) -> sent | failed | unknown. A delivery
report may reconcile pending/sending/unknown into sent/failed. 'unknown'
is never treated as failed and is never auto-resent.
"""

from uuid import UUID

import psycopg

from app.schemas.conversation import OutboxMessageResponse, OutboxStatus

_COLUMNS = (
    "id, message_id, conversation_id, body, status, attempts, last_error, "
    "sent_at, claimed_at, created_at, updated_at"
)

_INSERT = f"""
INSERT INTO outbox_messages (message_id, conversation_id, body)
VALUES (%(message_id)s, %(conversation_id)s, %(body)s)
RETURNING {_COLUMNS}
"""

# atomic claim: SKIP LOCKED lets concurrent dispatchers work different
# rows; 'failed' rows are re-claimable only under the attempt budget
# (the claim itself increments attempts and stamps claimed_at: one
# attempt per claim, ever; claimed_at is the stale-detection lease)
_CLAIM = f"""
UPDATE outbox_messages
SET status = 'sending', attempts = attempts + 1, claimed_at = now(), updated_at = now()
WHERE id = (
    SELECT id FROM outbox_messages
    WHERE status = 'pending'
       OR (status = 'failed' AND attempts < %(max_attempts)s)
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING {_COLUMNS}
"""

_CLAIM_FOR_CONVERSATION = f"""
UPDATE outbox_messages
SET status = 'sending', attempts = attempts + 1, claimed_at = now(), updated_at = now()
WHERE id = (
    SELECT id FROM outbox_messages
    WHERE conversation_id = %(conversation_id)s
      AND (status = 'pending' OR (status = 'failed' AND attempts < %(max_attempts)s))
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING {_COLUMNS}
"""

# stale claim recovery (M3): a 'sending' row whose claim lease expired is
# a dispatcher that died mid-send. The outcome CANNOT be determined, so
# the row becomes 'unknown' — the state that is never auto-resent and
# waits for a delivery report. This never sends anything.
_RECONCILE_STALE = f"""
UPDATE outbox_messages
SET status = 'unknown', last_error = 'stale sending claim reconciled', updated_at = now()
WHERE status = 'sending' AND claimed_at < now() - make_interval(secs => %(stale_after)s)
RETURNING {_COLUMNS}
"""


class OutboxRepository:
    def create(
        self,
        conn: psycopg.Connection,
        message_id: UUID,
        conversation_id: UUID,
        body: str,
    ) -> OutboxMessageResponse:
        """Persist the outbound intent BEFORE any send attempt. The
        UNIQUE (message_id) constraint makes one outbound row per reply;
        a second insert for the same message raises, never duplicates."""
        with conn.cursor() as cur:
            cur.execute(
                _INSERT,
                {
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "body": body,
                },
            )
            row = cur.fetchone()
        assert row is not None  # single-row INSERT ... RETURNING
        return OutboxMessageResponse.model_validate(row)

    def claim_next(
        self,
        conn: psycopg.Connection,
        max_attempts: int,
        conversation_id: UUID | None = None,
    ) -> OutboxMessageResponse | None:
        """Atomically claim the oldest dispatchable row: pending, or
        failed within the attempt budget. 'sending' and 'unknown' rows
        are never claimed: 'sending' belongs to another dispatcher,
        'unknown' waits for a delivery report (no blind resend)."""
        sql = _CLAIM_FOR_CONVERSATION if conversation_id is not None else _CLAIM
        with conn.cursor() as cur:
            cur.execute(
                sql,
                {
                    "conversation_id": conversation_id,
                    "max_attempts": max_attempts,
                },
            )
            row = cur.fetchone()
        return OutboxMessageResponse.model_validate(row) if row else None

    def record_send_result(
        self,
        conn: psycopg.Connection,
        outbox_id: UUID,
        status: OutboxStatus,
        last_error: str | None = None,
    ) -> OutboxMessageResponse | None:
        """Record the adapter's send outcome for a claimed row.
        Guarded from 'sending' so a strayed report can never overwrite a
        reconciled state. 'sent' stamps sent_at; attempts were already
        incremented by the claim."""
        sent_at_expr = "now()" if status == "sent" else "sent_at"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE outbox_messages
                SET status = %(status)s, last_error = %(last_error)s,
                    sent_at = {sent_at_expr}, updated_at = now()
                WHERE id = %(id)s AND status = 'sending'
                RETURNING {_COLUMNS}
                """,
                {"id": outbox_id, "status": status, "last_error": last_error},
            )
            row = cur.fetchone()
        return OutboxMessageResponse.model_validate(row) if row else None

    def apply_delivery_report(
        self, conn: psycopg.Connection, outbox_id: UUID, delivered: bool
    ) -> OutboxMessageResponse | None:
        """Reconcile a late delivery report: pending/sending/unknown may
        become sent or failed. An already-terminal row returns None
        (duplicate or late report, the caller treats it as a no-op)."""
        status = "sent" if delivered else "failed"
        sent_at_expr = "now()" if delivered else "sent_at"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE outbox_messages
                SET status = %(status)s, updated_at = now(), sent_at = {sent_at_expr}
                WHERE id = %(id)s
                  AND status IN ('pending', 'sending', 'unknown')
                RETURNING {_COLUMNS}
                """,
                {"id": outbox_id, "status": status},
            )
            row = cur.fetchone()
        return OutboxMessageResponse.model_validate(row) if row else None

    def reconcile_stale_sending(
        self, conn: psycopg.Connection, stale_after_seconds: float
    ) -> list[OutboxMessageResponse]:
        """Reconcile expired 'sending' claims to 'unknown' (M3). A row
        stays 'sending' only while its dispatcher is alive; past the
        lease the outcome is unknowable — never resent automatically,
        only resolved by a delivery report or an operator. Idempotent:
        already-reconciled rows are no longer 'sending'."""
        with conn.cursor() as cur:
            cur.execute(_RECONCILE_STALE, {"stale_after": stale_after_seconds})
            rows = cur.fetchall()
        return [OutboxMessageResponse.model_validate(r) for r in rows]

    def get_by_id(
        self, conn: psycopg.Connection, outbox_id: UUID
    ) -> OutboxMessageResponse | None:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM outbox_messages WHERE id = %s", (outbox_id,)
            )
            row = cur.fetchone()
        return OutboxMessageResponse.model_validate(row) if row else None

    def list_by_conversation(
        self, conn: psycopg.Connection, conversation_id: UUID
    ) -> list[OutboxMessageResponse]:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM outbox_messages WHERE conversation_id = %s ORDER BY created_at, id",
                (conversation_id,),
            )
            rows = cur.fetchall()
        return [OutboxMessageResponse.model_validate(r) for r in rows]
