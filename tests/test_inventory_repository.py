"""InventoryRepository unit tests — mocked database access, no live Supabase."""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

from app.repositories import inventory as inventory_repo
from app.repositories.inventory import InventoryRepository
from app.schemas.inventory import InventoryResponse

NOW = datetime.now(UTC)
PROD_ID = uuid4()

ROW = {
    "id": uuid4(),
    "product_id": PROD_ID,
    "on_hand_stock": 100,
    "reserved_stock": 25,
    "available_stock": 75,  # as returned by the DB generated column
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
        monkeypatch.setattr(inventory_repo, "get_connection", lambda: conn)
        return cursor, conn

    return install


def test_found_by_product_id(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = InventoryRepository().get_by_product_id(PROD_ID)
    assert isinstance(result, InventoryResponse)
    assert result.product_id == PROD_ID
    assert result.on_hand_stock == 100
    assert result.reserved_stock == 25


def test_not_found_returns_none(fake_db):
    fake_db(row=None)
    assert InventoryRepository().get_by_product_id(uuid4()) is None


def test_available_stock_mapped_from_db_value(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = InventoryRepository().get_by_product_id(PROD_ID)
    # mapped straight from the row dict — no Python arithmetic
    assert result.available_stock == 75
    sql, _ = cursor.executed
    assert "available_stock" in sql  # selected from the generated column


def test_sql_parameters_passed_separately(fake_db):
    cursor, conn = fake_db(row=ROW)
    InventoryRepository().get_by_product_id(PROD_ID)
    sql, params = cursor.executed
    assert "WHERE product_id = %s" in sql
    assert params == (PROD_ID,)
    selected = set(
        sql.split("SELECT ")[1].split(" FROM")[0].replace(" ", "").split(",")
    )
    assert selected == set(InventoryResponse.model_fields)  # exact columns


def test_db_exceptions_propagate(fake_db):
    cursor, conn = fake_db(row=ROW, raising=psycopg.OperationalError())
    with pytest.raises(psycopg.OperationalError):
        InventoryRepository().get_by_product_id(PROD_ID)
    assert conn.closed and cursor.closed


def test_contexts_exit_correctly(fake_db):
    cursor, conn = fake_db(row=ROW)
    InventoryRepository().get_by_product_id(PROD_ID)
    assert conn.closed is True
    assert cursor.closed is True
