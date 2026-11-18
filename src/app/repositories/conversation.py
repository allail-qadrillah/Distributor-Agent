from uuid import UUID

import psycopg

from app.schemas.conversation import ConversationResponse, ConversationState

_COLUMNS = "id, phone, customer_id, state, handoff_reason, created_at, updated_at"

# atomic get-or-create: the UNIQUE (phone) constraint is the arbiter;
# a concurrent insert loses the race and falls to the SELECT. The lazy
# customer link is a separate guarded update (never overwrites a link).
_INSERT = f"""
INSERT INTO conversations (phone, customer_id)
VALUES (%(phone)s, %(customer_id)s)
ON CONFLICT (phone) DO NOTHING
RETURNING {_COLUMNS}
"""


class ConversationRepository:
    def get_or_create_by_phone(
        self,
        conn: psycopg.Connection,
        phone: str,
        customer_id: UUID | None = None,
    ) -> ConversationResponse:
        with conn.cursor() as cur:
            cur.execute(_INSERT, {"phone": phone, "customer_id": customer_id})
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM conversations WHERE phone = %s", (phone,)
                )
                row = cur.fetchone()
            assert row is not None  # conflict path just selected it
        return ConversationResponse.model_validate(row)

    def link_customer(
        self, conn: psycopg.Connection, conversation_id: UUID, customer_id: UUID
    ) -> ConversationResponse | None:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE conversations SET customer_id = %(customer_id)s
                WHERE id = %(id)s AND customer_id IS NULL
                RETURNING {_COLUMNS}
                """,
                {"id": conversation_id, "customer_id": customer_id},
            )
            row = cur.fetchone()
        return ConversationResponse.model_validate(row) if row else None

    def get_by_id(
        self, conn: psycopg.Connection, conversation_id: UUID
    ) -> ConversationResponse | None:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM conversations WHERE id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
        return ConversationResponse.model_validate(row) if row else None

    def list_conversations(
        self,
        conn: psycopg.Connection,
        state: ConversationState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationResponse]:
        where = "WHERE state = %(state)s" if state is not None else ""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM conversations {where}
                ORDER BY updated_at DESC, id
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {"state": state, "limit": limit, "offset": offset},
            )
            rows = cur.fetchall()
        return [ConversationResponse.model_validate(r) for r in rows]

    def set_state(
        self,
        conn: psycopg.Connection,
        conversation_id: UUID,
        from_states: tuple[ConversationState, ...],
        to_state: ConversationState,
        reason: str | None = None,
    ) -> ConversationResponse | None:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE conversations
                SET state = %(to_state)s, handoff_reason = %(reason)s
                WHERE id = %(id)s AND state = ANY(%(from_states)s)
                RETURNING {_COLUMNS}
                """,
                {
                    "id": conversation_id,
                    "from_states": list(from_states),
                    "to_state": to_state,
                    "reason": reason,
                },
            )
            row = cur.fetchone()
        return ConversationResponse.model_validate(row) if row else None
