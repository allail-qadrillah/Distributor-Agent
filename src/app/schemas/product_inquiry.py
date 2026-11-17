"""Product inquiry schemas: request and response for product search.

Customers ask "what is X, at my price, and is it in stock?" — the request
is a customer identity plus one search term; the response carries only
what that answer needs (no unit, no timestamps, no totals). unit_price is
the backend-resolved price from the customer's price list; clients/LLMs
never supply it. available_stock is the database generated column.
"""

from decimal import Decimal
from uuid import UUID

from app.schemas.common import BaseSchema, NonBlankStr


class ProductInquiryRequest(BaseSchema):
    """One search: which customer is asking, and what they typed."""

    customer_id: UUID
    search_term: NonBlankStr  # mirrors DB btrim-not-blank checks


class ProductInquiryItem(BaseSchema):
    product_id: UUID
    sku: str
    name: str
    unit_price: Decimal  # resolved from the customer's price list
    available_stock: int  # generated column, never recalculated in Python


class ProductInquiryResponse(BaseSchema):
    """Every matching candidate — empty list when nothing matches."""

    products: list[ProductInquiryItem]
