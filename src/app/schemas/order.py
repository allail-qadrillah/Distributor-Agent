"""Order schemas: status enum, items, draft creation, response.

Columns mirror orders/order_items. State transitions (confirm/deliver/cancel)
are business operations — service layer, not schemas.

Documented application-only deviation from column shape:
order_items.unit_price IS a database column (snapshot), but it is NOT
part of order input — the backend resolves prices from the customer's
price list. Clients/LLMs never supply prices. OrderItemResponse returns
the stored snapshot (unit_price + generated subtotal).
OrderItemInsert carries the service-resolved unit_price to the
repository — internal input, never customer-facing.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import BaseSchema, Money


class OrderStatus(str, Enum):  # noqa: UP042 -- Preserve Enum string conversion behavior.
    """Single source of truth for order status values.

    Values mirror the orders_status_allowed CHECK constraint.
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    DELIVERED = "delivered"


class OrderItemCreate(BaseSchema):
    # unit_price deliberately absent (documented above).
    product_id: UUID
    quantity: Annotated[int, Field(gt=0)]  # mirrors CHECK (quantity > 0)


class OrderItemResponse(BaseSchema):
    id: UUID
    order_id: UUID
    product_id: UUID
    quantity: int
    unit_price: Decimal  # stored snapshot
    subtotal: Decimal  # generated column, never input
    created_at: datetime


class OrderItemInsert(BaseSchema):
    """Internal insert input for order_items — NOT customer-facing.

    OrderItemCreate (customer input) deliberately has no unit_price; the
    service resolves it from the customer's price list and constructs
    this type. unit_price is the order-creation snapshot stored in
    order_items.
    """

    product_id: UUID
    quantity: Annotated[int, Field(gt=0)]  # mirrors CHECK (quantity > 0)
    unit_price: Money  # mirrors numeric(14,2) CHECK (unit_price >= 0)


class OrderCreate(BaseSchema):
    """Draft order: existing customer + at least one item.

    order_number, status, total_amount, timestamps, item prices and
    subtotals are system-generated.
    """

    customer_id: UUID
    notes: str | None = None
    items: Annotated[list[OrderItemCreate], Field(min_length=1)]

    @model_validator(mode="after")
    def _reject_duplicate_products(self) -> "OrderCreate":
        # Mirrors DB UNIQUE (order_id, product_id): fail fast at the
        # validation boundary with a clear message.
        ids = [item.product_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError(
                "duplicate product_id in order items: each product may appear once per order"
            )
        return self


class OrderResponse(BaseSchema):
    id: UUID
    order_number: str
    customer_id: UUID
    status: OrderStatus
    total_amount: Decimal
    notes: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OrderDetailResponse(OrderResponse):
    """Order plus its items — read model for get-by-id / detail views."""

    items: list[OrderItemResponse]
