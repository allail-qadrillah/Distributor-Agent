"""Trusted execution context, application-owned, never model-owned.

Separation enforced here (P2.3 core rule): the model can produce tool
ARGUMENTS, but the customer identity, the conversation, and the explicit
confirmation approval are established by application logic (the caller:
future WhatsApp/API layer). Nothing in this module parses model output.

ConfirmOrderApproval: create_draft_order does NOT set it (draft creation
is not confirmation). Only the application's own approval detection,
the user's explicit "yes, confirm this order" captured outside the model
path, sets it. The confirm_order handler (P2.4) refuses to run without
it; the model cannot supply or forge it via arguments.
"""

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class ToolContext:
    """Per-request trusted context for tool execution.

    customer_id: the authenticated customer (from the WhatsApp binding /
    API auth, never from model output).
    conversation_id: caller-assigned conversation correlation id when the
    channel provides one; None is valid (no persistent storage exists).
    metadata: small caller notes for logging (e.g. channel, session ref);
    never business data.
    confirm_order_approved: explicit user approval for order confirmation,
    captured by application logic. Absent (False) by default.
    """

    customer_id: UUID
    conversation_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    confirm_order_approved: bool = False

    def __post_init__(self) -> None:
        # boundary typing: accept a UUID-shaped string from the caller,
        # reject anything else loudly (garbage identity must not flow)
        if not isinstance(self.customer_id, UUID):
            object.__setattr__(self, "customer_id", UUID(str(self.customer_id)))

    def with_confirm_order_approval(self) -> "ToolContext":
        """Copy of this context with confirmation approval granted."""
        return ToolContext(
            customer_id=self.customer_id,
            conversation_id=self.conversation_id,
            metadata=self.metadata,
            confirm_order_approved=True,
        )
