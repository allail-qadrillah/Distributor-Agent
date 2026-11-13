"""CustomerRepository unit tests — mocked database access, no live Supabase."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from app.repositories import customer as customer_repo
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
    def __init__(self, row=None):
        self.row = row
        self.executed: tuple | None = None
        self.closed = False

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchone(self):
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
    """Patch get_connection inside the repository module; returns (cursor, conn)."""
    holder: dict = {}

    def _get_connection():
        conn = holder["conn"]
        return conn  # used as context manager

    def install(row=None, raising=None):
        cursor = FakeCursor(row)
        cursor.raising = raising

        orig_fetchone = cursor.fetchone

        def fetchone():
            if cursor.raising:
                raise cursor.raising
            return orig_fetchone()

        cursor.fetchone = fetchone
        holder["cursor"] = cursor
        holder["conn"] = FakeConn(cursor)
        monkeypatch.setattr(customer_repo, "get_connection", _get_connection)
        return cursor, holder["conn"]

    return install


def test_get_by_id_found(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = CustomerRepository().get_by_id(CUST_ID)
    assert isinstance(result, CustomerResponse)
    assert result.id == CUST_ID
    assert result.name == "Toko Sinar Jaya"
    assert result.outstanding_balance == Decimal("250000.00")


def test_get_by_id_not_found(fake_db):
    cursor, conn = fake_db(row=None)
    assert CustomerRepository().get_by_id(uuid4()) is None


def test_get_by_phone_found(fake_db):
    cursor, conn = fake_db(row=ROW)
    result = CustomerRepository().get_by_phone("+6281234567890")
    assert isinstance(result, CustomerResponse)
    sql, params = cursor.executed
    assert "WHERE phone = %s" in sql
    assert params == ("+6281234567890",)


def test_get_by_phone_not_found(fake_db):
    fake_db(row=None)
    assert CustomerRepository().get_by_phone("+628999999999") is None


def test_sql_parameterized_and_columns_exact(fake_db):
    cursor, conn = fake_db(row=ROW)
    CustomerRepository().get_by_id(CUST_ID)
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
        CustomerRepository().get_by_id(CUST_ID)
    assert conn.closed and cursor.closed  # context managers still exited


def test_connection_context_exited(fake_db):
    cursor, conn = fake_db(row=ROW)
    CustomerRepository().get_by_id(CUST_ID)
    assert conn.closed is True
    assert cursor.closed is True
