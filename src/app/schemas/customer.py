from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.schemas.common import BaseSchema, Money, NonBlankStr, PhoneStr


class CustomerCreate(BaseSchema):
    # outstanding_balance is system-maintained (delivery increases it) —
    # not accepted as input. Same for id, created_at, updated_at.
    code: NonBlankStr
    name: NonBlankStr
    phone: PhoneStr
    address: str | None = None
    # NULL = not configured (rejects confirmation, never "unlimited");
    # explicit 0 = configured zero (migration 002 semantics)
    credit_limit: Money | None = None
    price_list_id: UUID | None = None  # NULL => 'GENERAL' fallback
    is_active: bool = True


class CustomerUpdate(BaseSchema):
    # id and outstanding_balance are not updatable via API
    code: NonBlankStr | None = None
    name: NonBlankStr | None = None
    phone: PhoneStr | None = None
    address: str | None = None
    credit_limit: Money | None = None
    price_list_id: UUID | None = None
    is_active: bool | None = None


class CustomerResponse(BaseSchema):
    id: UUID
    code: str
    name: str
    phone: str
    address: str | None
    credit_limit: Decimal | None  # NULL = not configured (migration 002)
    outstanding_balance: Decimal
    price_list_id: UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
