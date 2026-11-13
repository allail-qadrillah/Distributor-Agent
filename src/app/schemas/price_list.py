"""Price list and product price schemas. Columns mirror price_lists/product_prices."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.schemas.common import BaseSchema, Money, NonBlankStr


class PriceListCreate(BaseSchema):
    code: NonBlankStr
    name: NonBlankStr
    is_active: bool = True


class PriceListUpdate(BaseSchema):
    code: NonBlankStr | None = None
    name: NonBlankStr | None = None
    is_active: bool | None = None


class PriceListResponse(BaseSchema):
    id: UUID
    code: str
    name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProductPriceCreate(BaseSchema):
    price_list_id: UUID
    product_id: UUID
    unit_price: Money


class ProductPriceUpdate(BaseSchema):
    # (price_list_id, product_id) is the identity pair — only the price moves
    unit_price: Money


class ProductPriceResponse(BaseSchema):
    id: UUID
    price_list_id: UUID
    product_id: UUID
    unit_price: Decimal
    created_at: datetime
    updated_at: datetime
