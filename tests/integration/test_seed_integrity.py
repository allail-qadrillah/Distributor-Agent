import os

import pytest

from app.database.connection import get_connection
from app.repositories.price import PriceRepository
from app.services.price_list_resolver import GENERAL_PRICE_LIST_CODE

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="RUN_DB_SMOKE=1 required (live database)",
)


def test_general_price_list_seed_row_exists() -> None:
    with get_connection() as conn:
        row = PriceRepository().get_price_list_by_code(conn, GENERAL_PRICE_LIST_CODE)
    assert row is not None, (
        f"Seed price list '{GENERAL_PRICE_LIST_CODE}' is missing.  "
        f"Restore it from migration 001 seed INSERT and add a guard "
        f"to the test cleanup that deleted it."
    )
