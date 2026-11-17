"""Trusted execution context, application-owned, never model-owned.

Separation enforced here (P2.3 core rule): the model can produce tool
ARGUMENTS, but the customer identity, the conversation, and the explicit
confirmation approval are established by application logic (the caller:
future WhatsApp/API layer). Nothing in this module parses model output.

ConfirmOrderApproval: create_draft_order does NOT set it (draft creation
is not confirmation). Only the application's own approval detection,
the user's explicit "yes, confirm this order" captured outside the model
path, sets it, and it names the ONE order the user approved: approval
for draft A does not authorize confirming draft B in the same run. The
confirm_order handler refuses any order other than the approved one;
the model cannot supply or forge the approval via arguments.
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
    approved_order_id: the one order the user explicitly approved for
    confirmation, captured by application logic. None (not approved)
    by default.
    """

    customer_id: UUID
    conversation_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    approved_order_id: UUID | None = None

    def __post_init__(self) -> None:
        # boundary typing: accept a UUID-shaped string from the caller,
        # reject anything else loudly (garbage identity must not flow)
        if not isinstance(self.customer_id, UUID):
            object.__setattr__(self, "customer_id", UUID(str(self.customer_id)))
        if self.approved_order_id is not None and not isinstance(
            self.approved_order_id, UUID
        ):
            object.__setattr__(
                self, "approved_order_id", UUID(str(self.approved_order_id))
            )

    def with_confirm_order_approval(self, order_id: UUID) -> "ToolContext":
        """Copy of this context with confirmation approved for one order."""
        return ToolContext(
            customer_id=self.customer_id,
            conversation_id=self.conversation_id,
            metadata=self.metadata,
            approved_order_id=order_id,
        )
