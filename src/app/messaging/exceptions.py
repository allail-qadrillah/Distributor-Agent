"""Application-layer (messaging) exceptions.

Mirrors the service/agents convention: domain errors the API layer can
map to responses; psycopg and provider exceptions propagate untouched.
"""

from uuid import UUID

from app.schemas.conversation import ConversationState


class MessagingError(Exception):
    """Base for messaging workflow failures."""


class ConversationNotFoundError(MessagingError):
    def __init__(self, conversation_id: UUID) -> None:
        super().__init__(f"conversation {conversation_id} not found")
        self.conversation_id = conversation_id


class InvalidConversationStateError(MessagingError):
    """The conversation is not in a state that allows the transition."""

    def __init__(
        self,
        conversation_id: UUID,
        current_state: ConversationState,
        target_state: ConversationState,
    ) -> None:
        super().__init__(
            f"conversation {conversation_id} is {current_state}, "
            f"cannot transition to {target_state}"
        )
        self.conversation_id = conversation_id
        self.current_state = current_state
        self.target_state = target_state
