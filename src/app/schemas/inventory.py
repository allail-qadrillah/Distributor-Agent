"""Inventory schema. Read-only: single source of truth is the inventory table."""

from datetime import datetime
from uuid import UUID

from app.schemas.common import BaseSchema


class InventoryResponse(BaseSchema):
    # No create/update schema: stock changes only via confirm/deliver/cancel
    # business operations. available_stock is a generated column.
    id: UUID
    product_id: UUID
    on_hand_stock: int  # >= 0 guaranteed by DB CHECK
    reserved_stock: int
    available_stock: int
    updated_at: datetime
