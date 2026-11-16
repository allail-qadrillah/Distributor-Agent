"""InventoryRepository unit tests — mocked database access, no live Supabase.

Connection-injection contract is under test: the repository receives the
caller's connection, uses it, and never manages its lifecycle.
"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

import app.repositories.inventory as inventory_repo
from app.repositories.inventory import InventoryRepository
from app.schemas.inventory import InventoryResponse

NOW = datetime.now(UTC)
PROD_ID = uuid4()
OTHER_PROD_ID = uuid4()

ROW = {
    "id": uuid4(),
    "product_id": PROD_ID,
    "on_hand_stock": 100,
    "reserved_stock": 25,
    "available_stock": 75,  # as returned by the DB generated column
    "updated_at": NOW,
}
OTHER_ROW = {
    "id": uuid4(),
    "product_id": OTHER_PROD_ID,
    "on_hand_stock": 50,
    "reserved_stock": 0,
    "available_stock": 50,
    "updated_at": NOW,
}


class FakeCursor:
    def __init__(self, row=None, rows=None, raising=None):
        self.row = row
        self.rows = list(rows or [])
        self.raising = raising
        self.executed: tuple | None = None
        self.closed = False

    def execute(self, sql, params=None):
        if self.raising:
            raise self.raising
        self.executed = (sql, params)

    def fetchone(self):
        if self.raising:
            raise self.raising
        return self.row

    def fetchall(self):
        if self.raising:
            raise self.raising
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeConn:
    """Caller-supplied connection; records lifecycle calls the repo must not make."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.calls: list[str] = []  # any commit/rollback/close/BEGIN lands here

    def cursor(self):
        return self._cursor

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")

    def execute(self, sql, params=None):
        self.calls.append(f"execute: {sql}")


@pytest.fixture
def fake_db():
    def install(row=None, rows=None, raising=None):
        cursor = FakeCursor(row=row, rows=rows, raising=raising)
        conn = FakeConn(cursor)
        return cursor, conn

    return install


def test_found_by_product_id(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = InventoryRepository().get_by_product_id(conn, PROD_ID)
    assert isinstance(result, InventoryResponse)
    assert result.product_id == PROD_ID
    assert result.on_hand_stock == 100
    assert result.reserved_stock == 25


def test_not_found_returns_none(fake_db):
    cursor, conn = fake_db(row=None)
    assert InventoryRepository().get_by_product_id(conn, uuid4()) is None


def test_available_stock_mapped_from_db_value(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = InventoryRepository().get_by_product_id(conn, PROD_ID)
    # mapped straight from the row dict — no Python arithmetic
    assert result.available_stock == 75
    sql, _ = cursor.executed
    assert "available_stock" in sql  # selected from the generated column


def test_sql_parameters_passed_separately(fake_db):
    cursor, conn = fake_db(row=ROW)
    InventoryRepository().get_by_product_id(conn, PROD_ID)
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
        InventoryRepository().get_by_product_id(conn, PROD_ID)
    with pytest.raises(psycopg.OperationalError):
        InventoryRepository().lock_by_product_ids(conn, [PROD_ID])
    with pytest.raises(psycopg.OperationalError):
        InventoryRepository().reserve_stock(conn, PROD_ID, 5)


def test_repository_never_commits_rolls_back_or_closes(fake_db):
    cursor, conn = fake_db(row=ROW)
    InventoryRepository().get_by_product_id(conn, PROD_ID)

    cursor, conn = fake_db(rows=[ROW, OTHER_ROW])
    InventoryRepository().lock_by_product_ids(conn, [PROD_ID, OTHER_PROD_ID])

    cursor, conn = fake_db(row=ROW)
    InventoryRepository().reserve_stock(conn, PROD_ID, 5)

    assert conn.calls == []  # no commit, no rollback, no close, no BEGIN
    assert cursor.closed is True  # cursor context still exited


def test_repository_module_has_no_connection_factory():
    # connection injection: the module must never import its own way in
    assert "get_connection" not in vars(inventory_repo)
    assert "connect" not in vars(inventory_repo)


# ---------------------------------------------------------------------------
# lock_by_product_ids — pessimistic locking for the confirm workflow
# ---------------------------------------------------------------------------


def test_lock_by_product_ids_locks_in_ascending_order(fake_db):
    cursor, conn = fake_db(rows=[ROW, OTHER_ROW])
    result = InventoryRepository().lock_by_product_ids(conn, [OTHER_PROD_ID, PROD_ID])
    assert [r.product_id for r in result] == [PROD_ID, OTHER_PROD_ID]
    sql, params = cursor.executed
    assert "FOR UPDATE" in sql  # row locks held until the caller commits
    assert "ORDER BY product_id" in sql  # deterministic lock acquisition
    assert "product_id = ANY(%s)" in sql  # one statement for all rows
    assert params == ([OTHER_PROD_ID, PROD_ID],)


def test_lock_by_product_ids_returns_only_existing_rows(fake_db):
    # missing inventory rows are the service's problem to detect
    cursor, conn = fake_db(rows=[ROW])
    result = InventoryRepository().lock_by_product_ids(conn, [PROD_ID, OTHER_PROD_ID])
    assert [r.product_id for r in result] == [PROD_ID]


def test_lock_by_product_ids_empty_list(fake_db):
    cursor, conn = fake_db(rows=[])
    assert InventoryRepository().lock_by_product_ids(conn, []) == []
    assert cursor.executed is not None  # still one parameterized statement


# ---------------------------------------------------------------------------
# reserve_stock — SQL increment, on_hand untouched
# ---------------------------------------------------------------------------


def test_reserve_stock_increments_in_sql(fake_db):
    cursor, conn = fake_db(row={**ROW, "reserved_stock": 30, "available_stock": 70})
    result = InventoryRepository().reserve_stock(conn, PROD_ID, 5)
    assert isinstance(result, InventoryResponse)
    assert result.reserved_stock == 30  # as returned by the database
    sql, params = cursor.executed
    assert "UPDATE inventory" in sql
    assert "SET reserved_stock = reserved_stock + %s" in sql  # atomic increment
    assert "WHERE product_id = %s" in sql
    assert "RETURNING" in sql
    assert params == (5, PROD_ID)


def test_reserve_stock_never_touches_on_hand(fake_db):
    cursor, conn = fake_db(row=ROW)
    InventoryRepository().reserve_stock(conn, PROD_ID, 5)
    sql, _ = cursor.executed
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "reserved_stock" in set_clause
    assert "on_hand_stock" not in set_clause  # physical stock is delivery's job


def test_reserve_stock_missing_row_returns_none(fake_db):
    cursor, conn = fake_db(row=None)
    assert InventoryRepository().reserve_stock(conn, uuid4(), 5) is None
