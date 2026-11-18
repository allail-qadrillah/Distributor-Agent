"""Message repository — SQL for the messages table (transcript storage).

Same repository contract: caller's connection, parameterized SQL, no
transaction management. Inbound idempotency is the INSERT ... ON CONFLICT
(external_message_id) DO NOTHING: one atomic statement claims a webhook
delivery; the losing delivery gets None and is skipped.
"""

from uuid import UUID

import psycopg
from psycopg.types.json import Json

from app.schemas.conversation import (
    ConversationMessageResponse,
    MessageInsert,
)

_COLUMNS = (
    "id, conversation_id, seq, direction, role, content, tool_call_id, "
    "tool_calls, external_message_id, status, created_at"
)

_CLAIM_INBOUND = f"""
INSERT INTO messages (conversation_id, direction, role, content, external_message_id, status)
VALUES (%(conversation_id)s, 'inbound', 'user', %(content)s, %(external_message_id)s, 'unread')
ON CONFLICT (external_message_id) DO NOTHING
RETURNING {_COLUMNS}
"""

_INSERT_MESSAGE = f"""
INSERT INTO messages (conversation_id, direction, role, content, tool_call_id, tool_calls, status)
VALUES (%(conversation_id)s, %(direction)s, %(role)s, %(content)s, %(tool_call_id)s,
        %(tool_calls)s::jsonb, %(status)s)
RETURNING {_COLUMNS}
"""


class MessageRepository:
    def claim_inbound(
        self,
        conn: psycopg.Connection,
        conversation_id: UUID,
        external_message_id: str,
        content: str,
    ) -> ConversationMessageResponse | None:
        """Atomically claim one inbound external message id. None means a
        duplicate delivery: the message was already stored and processed
        (or is being processed under the conversation lock)."""
        with conn.cursor() as cur:
            cur.execute(
                _CLAIM_INBOUND,
                {
                    "conversation_id": conversation_id,
                    "external_message_id": external_message_id,
                    "content": content,
                },
            )
            row = cur.fetchone()
        return ConversationMessageResponse.model_validate(row) if row else None

    def append_message(
        self,
        conn: psycopg.Connection,
        conversation_id: UUID,
        message: MessageInsert,
    ) -> ConversationMessageResponse:
        """Append one transcript message (agent turn, tool result, or the
        outbound assistant text). Inserted one at a time in caller order:
        seq (identity) then preserves the transcript exactly."""
        payload = {
            "conversation_id": conversation_id,
            "direction": message.direction,
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "tool_calls": (
                Json([call.model_dump() for call in message.tool_calls])
                if message.tool_calls is not None
                else None
            ),
            "status": message.status,
        }
        with conn.cursor() as cur:
            cur.execute(_INSERT_MESSAGE, payload)
            row = cur.fetchone()
        assert row is not None  # single-row INSERT ... RETURNING
        return ConversationMessageResponse.model_validate(row)

    def list_by_conversation(
        self,
        conn: psycopg.Connection,
        conversation_id: UUID,
        before_seq: int | None = None,
        limit: int | None = None,
        roles: tuple[str, ...] | None = None,
    ) -> list[ConversationMessageResponse]:
        """Transcript order: ascending seq. before_seq/limit slice the
        tail for intent context: WITH limit the NEWEST rows are returned
        (still ascending); roles filters to specific roles with
        non-null content."""
        clauses = ["conversation_id = %(conversation_id)s"]
        if before_seq is not None:
            clauses.append("seq < %(before_seq)s")
        if roles is not None:
            clauses.append("role = ANY(%(roles)s) AND content IS NOT NULL")
        # limit selects the newest rows: DESC + LIMIT, reversed back to
        # transcript order in Python; without limit, full history ASC
        order = "DESC" if limit is not None else ""
        sql = (
            f"SELECT {_COLUMNS} FROM messages WHERE {' AND '.join(clauses)} "
            f"ORDER BY seq {order}"
        )
        if limit is not None:
            sql += " LIMIT %(limit)s"
        with conn.cursor() as cur:
            cur.execute(
                sql,
                {
                    "conversation_id": conversation_id,
                    "before_seq": before_seq,
                    "limit": limit,
                    "roles": list(roles) if roles is not None else None,
                },
            )
            rows = cur.fetchall()
        if limit is not None:
            rows = list(reversed(rows))
        return [ConversationMessageResponse.model_validate(r) for r in rows]

    def get_by_id(
        self, conn: psycopg.Connection, message_id: UUID
    ) -> ConversationMessageResponse | None:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM messages WHERE id = %s", (message_id,))
            row = cur.fetchone()
        return ConversationMessageResponse.model_validate(row) if row else None

    def list_unread_inbound(
        self, conn: psycopg.Connection, conversation_id: UUID
    ) -> list[ConversationMessageResponse]:
        """Stored unread inbound messages, oldest first: the deferred
        messages (approval pending delivery, handoff period) that wait
        for an explicit trigger before any agent run."""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM messages
                WHERE conversation_id = %s
                  AND direction = 'inbound'
                  AND status = 'unread'
                ORDER BY seq
                """,
                (conversation_id,),
            )
            rows = cur.fetchall()
        return [ConversationMessageResponse.model_validate(r) for r in rows]

    def mark_read(
        self, conn: psycopg.Connection, message_id: UUID
    ) -> ConversationMessageResponse | None:
        """Guarded: only unread -> read, so a human's unread marker is
        never silently consumed twice."""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE messages SET status = 'read'
                WHERE id = %s AND status = 'unread'
                RETURNING {_COLUMNS}
                """,
                (message_id,),
            )
            row = cur.fetchone()
        return ConversationMessageResponse.model_validate(row) if row else None
