from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class ToolContext:
    customer_id: UUID
    conversation_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    approved_order_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.customer_id, UUID):
            object.__setattr__(self, "customer_id", UUID(str(self.customer_id)))
        if self.approved_order_id is not None and not isinstance(
            self.approved_order_id, UUID
        ):
            object.__setattr__(
                self, "approved_order_id", UUID(str(self.approved_order_id))
            )

    def with_confirm_order_approval(self, order_id: UUID) -> "ToolContext":
        return ToolContext(
            customer_id=self.customer_id,
            conversation_id=self.conversation_id,
            metadata=self.metadata,
            approved_order_id=order_id,
        )
