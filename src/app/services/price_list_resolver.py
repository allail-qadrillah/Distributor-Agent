"""Shared price-list resolution — one rule for every service that prices.

The rule (migration 001): a customer's assigned price list when
customers.price_list_id is set, the GENERAL price list otherwise. This is
the only resolution logic here — product-price fallback (e.g. GENERAL when
a product is missing from the customer's list) is deliberately absent: a
missing product price is the caller's business failure to raise
(ProductPriceNotFoundError).

Component, not a service: no repositories, no transactions, no SQL. It
accepts the caller's connection and the caller's PriceRepository, so any
workflow (OrderService, ProductInquiryService, ...) resolves identically
inside its own transaction boundary.
"""

from uuid import UUID

import psycopg

from app.repositories.price import PriceRepository
from app.schemas.customer import CustomerResponse
from app.schemas.price_list import PriceListResponse
from app.services.exceptions import PriceListNotFoundError

# customers.price_list_id NULL => this list (migration 001 seed row)
GENERAL_PRICE_LIST_CODE = "GENERAL"


def resolve_price_list(
    conn: psycopg.Connection,
    price_repository: PriceRepository,
    customer: CustomerResponse,
) -> PriceListResponse:
    """Return the customer's assigned price list, or GENERAL when NULL.

    Raises PriceListNotFoundError (carrying the id or code that missed)
    when the assigned list or the GENERAL seed row is absent. No
    commit/rollback: the caller owns the transaction.
    """
    if customer.price_list_id is not None:
        price_list = price_repository.get_price_list_by_id(conn, customer.price_list_id)
        source: UUID | str = customer.price_list_id
    else:
        price_list = price_repository.get_price_list_by_code(
            conn, GENERAL_PRICE_LIST_CODE
        )
        source = GENERAL_PRICE_LIST_CODE
    if price_list is None:
        raise PriceListNotFoundError(source)
    return price_list
