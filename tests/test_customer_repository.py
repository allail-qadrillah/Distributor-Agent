"""CustomerRepository unit tests — mocked database access, no live Supabase.

Connection-injection contract is under test: the repository receives the
caller's connection, uses it, and never manages its lifecycle.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

import app.repositories.customer as customer_repo
from app.repositories.customer import CustomerRepository
from app.schemas.customer import CustomerResponse

NOW = datetime.now(UTC)
CUST_ID = uuid4()
PL_ID = uuid4()

ROW = {
    "id": CUST_ID,
    "code": "CUST-001",
    "name": "Toko Sinar Jaya",
    "phone": "+6281234567890",
    "address": "Jl. Merdeka 1",
    "credit_limit": Decimal("5000000.00"),
    "outstanding_balance": Decimal("250000.00"),
    "price_list_id": PL_ID,
    "is_active": True,
    "created_at": NOW,
    "updated_at": NOW,
}

COLUMNS = set(
    re.search(r"SELECT (.*) FROM", customer_repo._GET_BY_ID)
    .group(1)
    .replace(" ", "")
    .split(",")
)


class FakeCursor:
    def __init__(self, row=None, raising=None):
        self.row = row
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
    def install(row=None, raising=None):
        cursor = FakeCursor(row=row, raising=raising)
        conn = FakeConn(cursor)
        return cursor, conn

    return install


def test_get_by_id_found(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = CustomerRepository().get_by_id(conn, CUST_ID)
    assert isinstance(result, CustomerResponse)
    assert result.id == CUST_ID
    assert result.name == "Toko Sinar Jaya"
    assert result.outstanding_balance == Decimal("250000.00")


def test_get_by_id_not_found(fake_db):
    cursor, conn = fake_db(row=None)
    assert CustomerRepository().get_by_id(conn, uuid4()) is None


def test_get_by_phone_found(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = CustomerRepository().get_by_phone(conn, "+6281234567890")
    assert isinstance(result, CustomerResponse)
    sql, params = cursor.executed
    assert "WHERE phone = %s" in sql
    assert params == ("+6281234567890",)


def test_get_by_phone_not_found(fake_db):
    cursor, conn = fake_db(row=None)
    assert CustomerRepository().get_by_phone(conn, "+628999999999") is None


def test_sql_parameterized_and_columns_exact(fake_db):
    cursor, conn = fake_db(row=ROW)
    CustomerRepository().get_by_id(conn, CUST_ID)
    sql, params = cursor.executed
    # placeholder SQL + separately passed params: no string interpolation
    assert "WHERE id = %s" in sql
    assert params == (CUST_ID,)
    # selects exactly the CustomerResponse columns
    selected = set(
        sql.split("SELECT ")[1].split(" FROM")[0].replace(" ", "").split(",")
    )
    assert selected == COLUMNS == set(CustomerResponse.model_fields)


def test_db_exceptions_not_swallowed(fake_db):
    cursor, conn = fake_db(row=ROW, raising=psycopg.OperationalError())
    with pytest.raises(psycopg.OperationalError):
        CustomerRepository().get_by_id(conn, CUST_ID)
    with pytest.raises(psycopg.OperationalError):
        CustomerRepository().get_by_phone(conn, "+6281234567890")


def test_repository_never_commits_rolls_back_or_closes(fake_db):
    cursor, conn = fake_db(row=ROW)
    CustomerRepository().get_by_id(conn, CUST_ID)
    CustomerRepository().get_by_phone(conn, "+6281234567890")
    assert conn.calls == []  # no commit, no rollback, no close, no BEGIN
    assert cursor.closed is True  # cursor context still exited


def test_repository_module_has_no_connection_factory():
    # connection injection: the module must never import its own way in
    assert "get_connection" not in vars(customer_repo)
    assert "connect" not in vars(customer_repo)
