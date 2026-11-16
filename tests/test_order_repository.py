"""OrderRepository unit tests — mocked database access, no live Supabase.

Connection-injection contract is under test here: the repository receives
the caller's connection, uses it, and never manages its lifecycle.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

import app.repositories.order as order_repo
from app.repositories.order import OrderRepository
from app.schemas.order import (
    OrderCreate,
    OrderDetailResponse,
    OrderItemCreate,
    OrderItemInsert,
    OrderItemResponse,
    OrderResponse,
    OrderStatus,
)

NOW = datetime.now(UTC)
ORDER_ID = uuid4()
CUSTOMER_ID = uuid4()
PRODUCT_ID = uuid4()
OTHER_PRODUCT_ID = uuid4()

ORDER_ROW = {
    "id": ORDER_ID,
    "order_number": "ORD-00000001",
    "customer_id": CUSTOMER_ID,
    "status": "draft",
    "total_amount": Decimal("0.00"),
    "notes": "deliver before noon",
    "confirmed_at": None,
    "cancelled_at": None,
    "delivered_at": None,
    "created_at": NOW,
    "updated_at": NOW,
}


def _item_row(
    *,
    order_id: uuid4 = ORDER_ID,
    product_id: uuid4 = PRODUCT_ID,
    quantity: int = 2,
    unit_price: Decimal = Decimal("150.00"),
    subtotal: Decimal | None = None,
) -> dict:
    return {
        "id": uuid4(),
        "order_id": order_id,
        "product_id": product_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "subtotal": subtotal
        if subtotal is not None
        else Decimal(quantity) * unit_price,
        "created_at": NOW,
    }


ORDER_CREATE = OrderCreate(
    customer_id=CUSTOMER_ID,
    notes="deliver before noon",
    items=[
        OrderItemCreate(product_id=PRODUCT_ID, quantity=2),
        OrderItemCreate(product_id=OTHER_PRODUCT_ID, quantity=1),
    ],
)
ITEMS_IN = [
    OrderItemInsert(product_id=PRODUCT_ID, quantity=2, unit_price=Decimal("150.00")),
    OrderItemInsert(
        product_id=OTHER_PRODUCT_ID, quantity=1, unit_price=Decimal("2500.00")
    ),
]


class FakeCursor:
    """Records execute() SQL and params; serves canned rows.

    results is consumed one entry per fetch: a dict (or None) for
    fetchone(), a list of dicts for fetchall().
    """

    def __init__(self, results=None, raising=None):
        self.results = list(results or [])
        self.raising = raising
        self.executed: list[tuple] = []
        self.closed = False

    def execute(self, sql, params=None):
        if self.raising:
            raise self.raising
        self.executed.append((sql, params))

    def fetchone(self):
        if self.raising:
            raise self.raising
        return self.results.pop(0) if self.results else None

    def fetchall(self):
        if self.raising:
            raise self.raising
        entry = self.results.pop(0) if self.results else []
        return entry if isinstance(entry, list) else [entry]

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
    def install(results=None, raising=None):
        cursor = FakeCursor(results=results, raising=raising)
        conn = FakeConn(cursor)
        return cursor, conn

    return install


# ---------------------------------------------------------------------------
# 1. create_order — insertion
# ---------------------------------------------------------------------------


def test_create_order_inserts_row_and_returns_response(fake_db):
    cursor, conn = fake_db(results=[ORDER_ROW])
    result = OrderRepository().create_order(conn, ORDER_CREATE)
    assert isinstance(result, OrderResponse)
    assert result.id == ORDER_ID
    assert result.order_number == "ORD-00000001"
    assert result.status is OrderStatus.DRAFT
    assert result.total_amount == Decimal("0.00")
    sql, params = cursor.executed[0]
    assert "INSERT INTO orders" in sql
    assert "RETURNING" in sql
    assert params == (CUSTOMER_ID, "deliver before noon")


def test_create_order_writes_only_customer_and_notes(fake_db):
    cursor, conn = fake_db(results=[ORDER_ROW])
    OrderRepository().create_order(conn, ORDER_CREATE)
    sql, _ = cursor.executed[0]
    columns = sql.split("(", 1)[1].split(")")[0]
    assert "customer_id" in columns and "notes" in columns
    # system-generated values never written by the repository
    for forbidden in ("status", "order_number", "total_amount"):
        assert forbidden not in columns


# ---------------------------------------------------------------------------
# 2-3. create_order_items — insertion + generated subtotal
# ---------------------------------------------------------------------------


def test_create_order_items_inserts_all_items(fake_db):
    cursor, conn = fake_db(
        results=[
            _item_row(),
            _item_row(
                product_id=OTHER_PRODUCT_ID,
                quantity=1,
                unit_price=Decimal("2500.00"),
            ),
        ]
    )
    result = OrderRepository().create_order_items(conn, ORDER_ID, ITEMS_IN)
    assert len(result) == 2
    assert all(isinstance(i, OrderItemResponse) for i in result)
    assert all(i.order_id == ORDER_ID for i in result)
    assert [i.product_id for i in result] == [
        PRODUCT_ID,
        OTHER_PRODUCT_ID,
    ]  # input order
    assert len(cursor.executed) == 2  # one parameterized INSERT per item
    for (sql, params), (_, qty, price) in zip(
        cursor.executed,
        [(PRODUCT_ID, 2, Decimal("150.00")), (OTHER_PRODUCT_ID, 1, Decimal("2500.00"))],
        strict=True,
    ):
        assert "INSERT INTO order_items" in sql
        assert "RETURNING" in sql
        assert params == (ORDER_ID, _, qty, price)


def test_create_order_items_returns_db_generated_subtotal(fake_db):
    # DB subtotal deliberately != quantity * unit_price: proves the
    # repository returns the database value instead of computing one.
    db_row = _item_row(
        quantity=2, unit_price=Decimal("150.00"), subtotal=Decimal("333.33")
    )
    cursor, conn = fake_db(results=[db_row])
    result = OrderRepository().create_order_items(conn, ORDER_ID, ITEMS_IN[:1])
    assert result[0].subtotal == Decimal("333.33")
    assert result[0].unit_price == Decimal("150.00")  # snapshot passed through
    sql, params = cursor.executed[0]
    assert "RETURNING" in sql and "subtotal" in sql
    assert len(params) == 4  # subtotal never bound as a parameter


def test_create_order_items_empty_list_no_sql(fake_db):
    cursor, conn = fake_db(results=[])
    assert OrderRepository().create_order_items(conn, ORDER_ID, []) == []
    assert cursor.executed == []


# ---------------------------------------------------------------------------
# 4-5. get_by_id — order with items / miss
# ---------------------------------------------------------------------------


def test_get_by_id_returns_order_with_items(fake_db):
    item_rows = [
        _item_row(),
        _item_row(
            product_id=OTHER_PRODUCT_ID,
            quantity=1,
            unit_price=Decimal("2500.00"),
            subtotal=Decimal("2500.00"),
        ),
    ]
    cursor, conn = fake_db(results=[ORDER_ROW, item_rows])
    result = OrderRepository().get_by_id(conn, ORDER_ID)
    assert isinstance(result, OrderDetailResponse)
    assert result.order_number == "ORD-00000001"
    assert len(result.items) == 2
    assert {i.product_id for i in result.items} == {PRODUCT_ID, OTHER_PRODUCT_ID}
    assert result.items[0].subtotal == Decimal("300.00")
    sql_order, params_order = cursor.executed[0]
    sql_items, params_items = cursor.executed[1]
    assert "FROM orders WHERE id = %s" in sql_order
    assert params_order == (ORDER_ID,)
    assert "FROM order_items WHERE order_id = %s" in sql_items
    assert params_items == (ORDER_ID,)


def test_get_by_id_missing_returns_none(fake_db):
    cursor, conn = fake_db(results=[None])
    assert OrderRepository().get_by_id(conn, uuid4()) is None
    # no items query after a miss
    assert len(cursor.executed) == 1


# ---------------------------------------------------------------------------
# 6. SQL parameterization
# ---------------------------------------------------------------------------


def test_create_order_parameters_bound_never_interpolated(fake_db):
    cursor, conn = fake_db(results=[ORDER_ROW])
    OrderRepository().create_order(conn, ORDER_CREATE)
    sql, params = cursor.executed[0]
    assert sql.count("%s") == 2
    assert str(CUSTOMER_ID) not in sql  # value travels as parameter only
    assert params == (CUSTOMER_ID, "deliver before noon")


def test_create_order_items_parameters_bound_never_interpolated(fake_db):
    cursor, conn = fake_db(results=[_item_row()])
    OrderRepository().create_order_items(conn, ORDER_ID, ITEMS_IN[:1])
    sql, params = cursor.executed[0]
    assert sql.count("%s") == 4
    assert str(PRODUCT_ID) not in sql
    assert str(Decimal("150.00")) not in sql
    assert params == (ORDER_ID, PRODUCT_ID, 2, Decimal("150.00"))


def test_get_by_id_parameters_bound_never_interpolated(fake_db):
    cursor, conn = fake_db(results=[None])
    OrderRepository().get_by_id(conn, ORDER_ID)
    for sql, params in cursor.executed:
        assert str(ORDER_ID) not in sql
        assert params == (ORDER_ID,)


# ---------------------------------------------------------------------------
# 7. error propagation
# ---------------------------------------------------------------------------


def test_db_exceptions_propagate(fake_db):
    for results in ([ORDER_ROW], [_item_row()], [ORDER_ROW, []]):
        _, conn = fake_db(results=results, raising=psycopg.OperationalError())
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().create_order(conn, ORDER_CREATE)
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().create_order_items(conn, ORDER_ID, ITEMS_IN)
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().update_total_amount(conn, ORDER_ID, Decimal("1.00"))
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().get_by_id(conn, ORDER_ID)
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().get_for_update(conn, ORDER_ID)
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().sum_confirmed_totals(conn, CUSTOMER_ID)
        with pytest.raises(psycopg.OperationalError):
            OrderRepository().mark_confirmed(conn, ORDER_ID)


# ---------------------------------------------------------------------------
# 8. no transaction lifecycle, no own connection
# ---------------------------------------------------------------------------


def test_repository_never_commits_rolls_back_or_closes(fake_db):
    _, conn = fake_db(results=[ORDER_ROW])
    OrderRepository().create_order(conn, ORDER_CREATE)

    _, conn = fake_db(results=[_item_row()])
    OrderRepository().create_order_items(conn, ORDER_ID, ITEMS_IN[:1])

    _, conn = fake_db(results=[{**ORDER_ROW, "total_amount": Decimal("2800.00")}])
    OrderRepository().update_total_amount(conn, ORDER_ID, Decimal("2800.00"))

    _, conn = fake_db(results=[ORDER_ROW, [_item_row()]])
    OrderRepository().get_by_id(conn, ORDER_ID)

    _, conn = fake_db(results=[ORDER_ROW, [_item_row()]])
    OrderRepository().get_for_update(conn, ORDER_ID)

    _, conn = fake_db(results=[{"confirmed_total": Decimal("0.00")}])
    OrderRepository().sum_confirmed_totals(conn, CUSTOMER_ID)

    _, conn = fake_db(
        results=[{**ORDER_ROW, "status": "confirmed", "confirmed_at": NOW}]
    )
    OrderRepository().mark_confirmed(conn, ORDER_ID)

    assert conn.calls == []  # no commit, no rollback, no close, no BEGIN


def test_repository_module_has_no_connection_factory():
    # connection injection: the module must never import its own way in
    assert "get_connection" not in vars(order_repo)
    assert "connect" not in vars(order_repo)


# ---------------------------------------------------------------------------
# 9. update_total_amount — total maintenance
# ---------------------------------------------------------------------------


def test_update_total_amount_updates_and_returns_response(fake_db):
    updated_row = {**ORDER_ROW, "total_amount": Decimal("2800.00")}
    cursor, conn = fake_db(results=[updated_row])
    result = OrderRepository().update_total_amount(conn, ORDER_ID, Decimal("2800.00"))
    assert isinstance(result, OrderResponse)
    assert result.id == ORDER_ID
    assert result.total_amount == Decimal("2800.00")
    assert result.status is OrderStatus.DRAFT
    sql, params = cursor.executed[0]
    assert "UPDATE orders" in sql
    assert "SET total_amount = %s" in sql
    assert "WHERE id = %s" in sql  # scoped: only the intended order
    assert "RETURNING" in sql
    assert params == (Decimal("2800.00"), ORDER_ID)


def test_update_total_amount_missing_order_returns_none(fake_db):
    cursor, conn = fake_db(results=[None])
    result = OrderRepository().update_total_amount(conn, uuid4(), Decimal("10.00"))
    assert result is None


def test_update_total_amount_writes_only_total(fake_db):
    cursor, conn = fake_db(results=[ORDER_ROW])
    OrderRepository().update_total_amount(conn, ORDER_ID, Decimal("1.00"))
    sql, _ = cursor.executed[0]
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "total_amount" in set_clause
    # system-generated values never written by the repository
    for forbidden in ("status", "order_number", "customer_id"):
        assert forbidden not in set_clause


def test_update_total_amount_parameters_bound_never_interpolated(fake_db):
    cursor, conn = fake_db(results=[{**ORDER_ROW}])
    OrderRepository().update_total_amount(conn, ORDER_ID, Decimal("2800.00"))
    sql, params = cursor.executed[0]
    assert sql.count("%s") == 2
    assert str(ORDER_ID) not in sql  # value travels as parameter only
    assert str(Decimal("2800.00")) not in sql
    assert params == (Decimal("2800.00"), ORDER_ID)


# ---------------------------------------------------------------------------
# 10. get_for_update — locked order read for the confirm workflow
# ---------------------------------------------------------------------------


def test_get_for_update_locks_order_and_returns_detail(fake_db):
    item_rows = [_item_row()]
    cursor, conn = fake_db(results=[ORDER_ROW, item_rows])
    result = OrderRepository().get_for_update(conn, ORDER_ID)
    assert isinstance(result, OrderDetailResponse)
    assert result.id == ORDER_ID
    assert len(result.items) == 1
    sql, params = cursor.executed[0]
    assert "FROM orders WHERE id = %s" in sql
    assert "FOR UPDATE" in sql  # order row locked under the caller's transaction
    assert params == (ORDER_ID,)
    sql_items, params_items = cursor.executed[1]
    assert "FROM order_items WHERE order_id = %s" in sql_items
    assert "FOR UPDATE" not in sql_items  # items read unlocked
    assert params_items == (ORDER_ID,)


def test_get_for_update_missing_returns_none(fake_db):
    cursor, conn = fake_db(results=[None])
    assert OrderRepository().get_for_update(conn, uuid4()) is None
    assert len(cursor.executed) == 1  # no items query after a miss


# ---------------------------------------------------------------------------
# 11. sum_confirmed_totals — credit exposure input
# ---------------------------------------------------------------------------


def test_sum_confirmed_totals_returns_decimal_sum(fake_db):
    cursor, conn = fake_db(results=[{"confirmed_total": Decimal("1234.56")}])
    result = OrderRepository().sum_confirmed_totals(conn, CUSTOMER_ID)
    assert result == Decimal("1234.56")
    sql, params = cursor.executed[0]
    assert "SUM(total_amount)" in sql
    assert "COALESCE" in sql  # NULL sum (no confirmed orders) -> 0.00
    assert "status = 'confirmed'" in sql  # draft/cancelled/delivered excluded
    assert params == (CUSTOMER_ID,)


def test_sum_confirmed_totals_zero_when_no_confirmed_orders(fake_db):
    cursor, conn = fake_db(results=[{"confirmed_total": Decimal("0.00")}])
    assert OrderRepository().sum_confirmed_totals(conn, CUSTOMER_ID) == Decimal("0.00")


# ---------------------------------------------------------------------------
# 12. mark_confirmed — guarded draft -> confirmed transition
# ---------------------------------------------------------------------------


CONFIRMED_ROW = {**ORDER_ROW, "status": "confirmed", "confirmed_at": NOW}


def test_mark_confirmed_guarded_update(fake_db):
    cursor, conn = fake_db(results=[CONFIRMED_ROW])
    result = OrderRepository().mark_confirmed(conn, ORDER_ID)
    assert isinstance(result, OrderResponse)
    assert result.status is OrderStatus.CONFIRMED
    assert result.confirmed_at == NOW
    sql, params = cursor.executed[0]
    assert "UPDATE orders" in sql
    assert "SET status = 'confirmed'" in sql
    assert "confirmed_at = now()" in sql  # database clock
    assert "WHERE id = %s AND status = 'draft'" in sql  # guarded
    assert "RETURNING" in sql
    assert params == (ORDER_ID,)


def test_mark_confirmed_zero_rows_returns_none(fake_db):
    # status no longer draft (or row gone): guarded UPDATE matches nothing
    cursor, conn = fake_db(results=[None])
    assert OrderRepository().mark_confirmed(conn, ORDER_ID) is None
