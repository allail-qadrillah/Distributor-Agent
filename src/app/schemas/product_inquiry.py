from decimal import Decimal
from uuid import UUID

from app.schemas.common import BaseSchema, NonBlankStr


class ProductInquiryRequest(BaseSchema):

    customer_id: UUID
    search_term: NonBlankStr  # mirrors DB btrim-not-blank checks


class ProductInquiryItem(BaseSchema):
    product_id: UUID
    sku: str
    name: str
    unit_price: Decimal  # resolved from the customer's price list
    available_stock: int  # generated column, never recalculated in Python


class ProductInquiryResponse(BaseSchema):

    products: list[ProductInquiryItem]
