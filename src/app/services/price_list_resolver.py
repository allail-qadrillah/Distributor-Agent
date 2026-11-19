from uuid import UUID

import psycopg

from app.repositories.price import PriceRepository
from app.schemas.customer import CustomerResponse
from app.schemas.price_list import PriceListResponse
from app.services.exceptions import PriceListNotFoundError

GENERAL_PRICE_LIST_CODE = "GENERAL"


def resolve_price_list(
    conn: psycopg.Connection,
    price_repository: PriceRepository,
    customer: CustomerResponse,
) -> PriceListResponse:
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
