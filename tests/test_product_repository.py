"""ProductRepository unit tests — mocked database access, no live Supabase.

Connection-injection contract is under test: the repository receives the
caller's connection, uses it, and never manages its lifecycle.
"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

import app.repositories.product as product_repo
from app.repositories.product import ProductRepository
from app.schemas.product import ProductResponse

NOW = datetime.now(UTC)
PROD_ID = uuid4()
OTHER_ID = uuid4()

PROD_ROW = {
    "id": PROD_ID,
    "sku": "AQUA-600ML",
    "name": "Aqua Botol 600ml",
    "unit": "pcs",
    "is_active": True,
    "created_at": NOW,
    "updated_at": NOW,
}
OTHER_ROW = {**PROD_ROW, "id": OTHER_ID, "sku": "IND-MIE-GORENG"}


class FakeCursor:
    def __init__(self, row=None, rows=None, raising=None):
        self.row = row
        self.rows = rows if rows is not None else []
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


def test_get_by_id_found(fake_db):
    cursor, conn = fake_db(row=PROD_ROW)
    result = ProductRepository().get_by_id(conn, PROD_ID)
    assert isinstance(result, ProductResponse)
    assert result.sku == "AQUA-600ML"
    sql, params = cursor.executed
    assert "WHERE id = %s" in sql
    assert params == (PROD_ID,)


def test_get_by_id_not_found(fake_db):
    cursor, conn = fake_db(row=None)
    assert ProductRepository().get_by_id(conn, uuid4()) is None


def test_get_by_sku_found(fake_db):
    cursor, conn = fake_db(row=PROD_ROW)
    result = ProductRepository().get_by_sku(conn, "AQUA-600ML")
    assert isinstance(result, ProductResponse)
    sql, params = cursor.executed
    assert "WHERE sku = %s" in sql
    assert params == ("AQUA-600ML",)


def test_get_by_sku_not_found(fake_db):
    cursor, conn = fake_db(row=None)
    assert ProductRepository().get_by_sku(conn, "NOPE") is None


def _search_sql(cursor, query: str) -> str:
    sql, params = cursor.executed
    assert params == (f"%{query}%", f"%{query}%")  # same pattern for name and alias
    return sql


def test_search_matches_product_name(fake_db):
    cursor, conn = fake_db(rows=[PROD_ROW])
    results = ProductRepository().search_by_name_or_alias(conn, "Aqua")
    assert [r.sku for r in results] == ["AQUA-600ML"]
    sql = _search_sql(cursor, "Aqua")
    assert "FROM products p" in sql
    assert "p.name ILIKE" in sql


def test_search_matches_product_alias(fake_db):
    cursor, conn = fake_db(rows=[PROD_ROW])
    ProductRepository().search_by_name_or_alias(conn, "Aqua")
    sql = _search_sql(cursor, "Aqua")
    assert "LEFT JOIN product_aliases pa" in sql
    assert "pa.alias ILIKE" in sql


def test_search_is_case_insensitive(fake_db):
    cursor, conn = fake_db(rows=[PROD_ROW])
    ProductRepository().search_by_name_or_alias(conn, "AQUA")
    sql = _search_sql(cursor, "AQUA")
    assert "ILIKE" in sql  # ILIKE = case-insensitive match operator
    # pattern not pre-lowered: case folding is the database's job
    _, params = cursor.executed
    assert params[0] == "%AQUA%"


def test_search_returns_unique_products(fake_db):
    cursor, conn = fake_db(rows=[PROD_ROW, PROD_ROW, OTHER_ROW])
    results = ProductRepository().search_by_name_or_alias(conn, "Aqua")
    # DISTINCT is in SQL; the repository maps rows 1:1 — DB dedupes
    sql = _search_sql(cursor, "Aqua")
    assert "DISTINCT" in sql
    assert len(results) == 3  # what the (deduped-by-SQL) DB returns


def test_search_excludes_inactive_products(fake_db):
    cursor, conn = fake_db(rows=[])
    ProductRepository().search_by_name_or_alias(conn, "Aqua")
    sql = _search_sql(cursor, "Aqua")
    assert "p.is_active = true" in sql


def test_search_no_matches_returns_empty_list(fake_db):
    cursor, conn = fake_db(rows=[])
    assert ProductRepository().search_by_name_or_alias(conn, "zzznomatch") == []


def test_search_parameters_passed_separately_and_escaped(fake_db):
    cursor, conn = fake_db(rows=[])
    ProductRepository().search_by_name_or_alias(conn, "100%_x")
    sql, params = cursor.executed
    # pattern passed as parameter, never interpolated into SQL
    assert "%s" in sql
    assert "%100\\%\\_x%" == params[0]  # user wildcards escaped


def test_db_exceptions_propagate(fake_db):
    cursor, conn = fake_db(row=PROD_ROW, raising=psycopg.OperationalError())
    with pytest.raises(psycopg.OperationalError):
        ProductRepository().get_by_id(conn, PROD_ID)
    with pytest.raises(psycopg.OperationalError):
        ProductRepository().search_by_name_or_alias(conn, "Aqua")


def test_repository_never_commits_rolls_back_or_closes(fake_db):
    _, conn = fake_db(row=PROD_ROW)
    ProductRepository().get_by_id(conn, PROD_ID)

    _, conn = fake_db(row=PROD_ROW)
    ProductRepository().get_by_sku(conn, "AQUA-600ML")

    cursor, conn = fake_db(rows=[PROD_ROW])
    ProductRepository().search_by_name_or_alias(conn, "Aqua")

    assert conn.calls == []  # no commit, no rollback, no close, no BEGIN
    assert cursor.closed is True  # cursor context still exited


def test_repository_module_has_no_connection_factory():
    # connection injection: the module must never import its own way in
    assert "get_connection" not in vars(product_repo)
    assert "connect" not in vars(product_repo)
