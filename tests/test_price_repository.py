"""PriceRepository unit tests — mocked database access, no live Supabase."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from app.repositories import price as price_repo
from app.repositories.price import PriceRepository
from app.schemas.price_list import PriceListResponse, ProductPriceResponse

NOW = datetime.now(UTC)
PL_ID = uuid4()
PROD_ID = uuid4()

PL_ROW = {
    "id": PL_ID,
    "code": "RETAIL",
    "name": "Retail price list",
    "is_active": True,
    "created_at": NOW,
    "updated_at": NOW,
}
PP_ROW = {
    "id": uuid4(),
    "price_list_id": PL_ID,
    "product_id": PROD_ID,
    "unit_price": Decimal("3500.00"),
    "created_at": NOW,
    "updated_at": NOW,
}


class FakeCursor:
    def __init__(self, row=None, raising=None):
        self.row = row
        self.raising = raising
        self.executed: tuple | None = None
        self.closed = False

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchone(self):
        if self.raising:
            raise self.raising
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture
def fake_db(monkeypatch):
    def install(row=None, raising=None):
        cursor = FakeCursor(row=row, raising=raising)
        conn = FakeConn(cursor)
        monkeypatch.setattr(price_repo, "get_connection", lambda: conn)
        return cursor, conn

    return install


def test_price_list_found_by_id(fake_db):
    cursor, conn = fake_db(row=PL_ROW)
    result = PriceRepository().get_price_list_by_id(PL_ID)
    assert isinstance(result, PriceListResponse)
    assert result.code == "RETAIL"
    sql, params = cursor.executed
    assert "WHERE id = %s" in sql
    assert params == (PL_ID,)


def test_price_list_not_found_by_id(fake_db):
    fake_db(row=None)
    assert PriceRepository().get_price_list_by_id(uuid4()) is None


def test_price_list_found_by_code(fake_db):
    cursor, conn = fake_db(row=PL_ROW)
    result = PriceRepository().get_price_list_by_code("RETAIL")
    assert isinstance(result, PriceListResponse)
    assert result.id == PL_ID
    sql, params = cursor.executed
    assert "WHERE code = %s" in sql
    assert params == ("RETAIL",)


def test_price_list_not_found_by_code(fake_db):
    fake_db(row=None)
    assert PriceRepository().get_price_list_by_code("NOPE") is None


def test_product_price_found(fake_db):
    cursor, conn = fake_db(row=PP_ROW)
    result = PriceRepository().get_product_price(PL_ID, PROD_ID)
    assert isinstance(result, ProductPriceResponse)
    assert result.unit_price == Decimal("3500.00")
    sql, params = cursor.executed
    assert "WHERE price_list_id = %s AND product_id = %s" in sql
    assert params == (PL_ID, PROD_ID)


def test_product_price_not_found(fake_db):
    fake_db(row=None)
    assert PriceRepository().get_product_price(uuid4(), uuid4()) is None


def test_correct_columns_selected(fake_db):
    cursor, conn = fake_db(row=PL_ROW)
    PriceRepository().get_price_list_by_id(PL_ID)
    sql, _ = cursor.executed
    selected = set(
        sql.split("SELECT ")[1].split(" FROM")[0].replace(" ", "").split(",")
    )
    assert selected == set(PriceListResponse.model_fields)

    cursor, conn = fake_db(row=PP_ROW)
    PriceRepository().get_product_price(PL_ID, PROD_ID)
    sql, _ = cursor.executed
    selected = set(
        sql.split("SELECT ")[1].split(" FROM")[0].replace(" ", "").split(",")
    )
    assert selected == set(ProductPriceResponse.model_fields)


def test_db_exceptions_propagate(fake_db):
    cursor, conn = fake_db(row=PL_ROW, raising=psycopg.OperationalError())
    with pytest.raises(psycopg.OperationalError):
        PriceRepository().get_price_list_by_id(PL_ID)
    with pytest.raises(psycopg.OperationalError):
        PriceRepository().get_product_price(PL_ID, PROD_ID)
    assert conn.closed and cursor.closed


def test_contexts_exit_correctly(fake_db):
    cursor, conn = fake_db(row=PL_ROW)
    PriceRepository().get_price_list_by_id(PL_ID)
    assert conn.closed is True
    assert cursor.closed is True
