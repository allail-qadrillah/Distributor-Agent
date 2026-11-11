"""Schema validation tests. Pure Pydantic — no database connection."""
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import (
    CustomerCreate,
    CustomerResponse,
    CustomerUpdate,
    InventoryResponse,
    OrderCreate,
    OrderItemCreate,
    OrderItemResponse,
    OrderResponse,
    OrderStatus,
    PriceListCreate,
    PriceListResponse,
    PriceListUpdate,
    ProductAliasCreate,
    ProductAliasResponse,
    ProductCreate,
    ProductPriceCreate,
    ProductPriceResponse,
    ProductPriceUpdate,
    ProductResponse,
    ProductUpdate,
)

NOW = datetime.now(timezone.utc)
MIGRATION = Path(__file__).parents[1] / "src/app/database/migrations/001_initial_schema.sql"


def test_customer_create_valid() -> None:
    c = CustomerCreate(
        code="CUST-001",
        name="Toko Sinar Jaya",
        phone="+6281234567890",
        credit_limit=Decimal("1000000.00"),
    )
    assert c.credit_limit == Decimal("1000000.00")
    assert c.price_list_id is None
    assert c.is_active is True


def test_customer_invalid_phone() -> None:
    base = {"code": "C1", "name": "x"}
    for bad in ("081234567890", "+62 8123456789", "+6281234567890123456", "phone"):
        with pytest.raises(ValidationError):
            CustomerCreate(**base, phone=bad)
    assert CustomerCreate(**base, phone="+123456789012345")  # 15 digits: max valid


def test_customer_name_blank() -> None:
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            CustomerCreate(code="C1", name=bad, phone="+6281234567890")


def test_product_create_valid() -> None:
    p = ProductCreate(sku="AQUA-600ML", name="Aqua Botol 600ml", unit="pcs")
    assert p.is_active is True


def test_negative_price_rejected() -> None:
    with pytest.raises(ValidationError):
        ProductPriceCreate(
            price_list_id=uuid4(), product_id=uuid4(), unit_price=Decimal("-1.00")
        )
    with pytest.raises(ValidationError):
        CustomerCreate(code="C1", name="x", phone="+6281234567890", credit_limit=Decimal("-5"))


def test_price_more_than_two_decimals_rejected() -> None:
    with pytest.raises(ValidationError):
        ProductPriceCreate(
            price_list_id=uuid4(), product_id=uuid4(), unit_price=Decimal("1.234")
        )


def test_quantity_zero_and_negative_rejected() -> None:
    for q in (0, -5):
        with pytest.raises(ValidationError):
            OrderItemCreate(product_id=uuid4(), quantity=q)


def test_valid_uuid_parsing() -> None:
    pid = uuid4()
    item = OrderItemCreate(product_id=str(pid), quantity=10)
    assert item.product_id == pid


def test_invalid_uuid_rejected() -> None:
    with pytest.raises(ValidationError):
        CustomerCreate(code="C1", name="x", phone="+6281234567890", price_list_id="not-a-uuid")


def test_generated_fields_not_accepted_as_input() -> None:
    base_customer = {"code": "C1", "name": "x", "phone": "+6281234567890"}
    with pytest.raises(ValidationError):  # system-maintained balance
        CustomerCreate(**base_customer, outstanding_balance=Decimal("1"))
    with pytest.raises(ValidationError):  # generated column
        OrderItemCreate(product_id=uuid4(), quantity=1, subtotal=Decimal("5"))
    with pytest.raises(ValidationError):  # system-managed status
        OrderCreate(customer_id=uuid4(), items=[{"product_id": str(uuid4()), "quantity": 1}], status="draft")
    with pytest.raises(ValidationError):  # system-generated order number
        OrderCreate(customer_id=uuid4(), items=[{"product_id": str(uuid4()), "quantity": 1}], order_number="ORD-00000001")


def _order_response(**over) -> OrderResponse:
    base = {
        "id": uuid4(),
        "order_number": "ORD-00000001",
        "customer_id": uuid4(),
        "status": "draft",
        "total_amount": Decimal("0.00"),
        "notes": None,
        "confirmed_at": None,
        "cancelled_at": None,
        "delivered_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(over)
    return OrderResponse(**base)


def test_order_status_valid_and_invalid() -> None:
    assert _order_response().status is OrderStatus.DRAFT
    assert _order_response(status="confirmed").status is OrderStatus.CONFIRMED
    with pytest.raises(ValidationError):
        _order_response(status="shipped")
    with pytest.raises(ValueError):
        OrderStatus("pending")


def test_valid_draft_order_structure() -> None:
    o = OrderCreate(
        customer_id=uuid4(),
        notes="deliver before 9am",
        items=[
            {"product_id": str(uuid4()), "quantity": 10},
            {"product_id": str(uuid4()), "quantity": 2},
        ],
    )
    assert len(o.items) == 2
    assert o.items[1].quantity == 2


def test_draft_order_empty_items_rejected() -> None:
    with pytest.raises(ValidationError):
        OrderCreate(customer_id=uuid4(), items=[])


def test_invalid_order_item_structure() -> None:
    with pytest.raises(ValidationError):  # missing product_id
        OrderItemCreate(quantity=1)
    with pytest.raises(ValidationError):  # missing quantity
        OrderItemCreate(product_id=uuid4())
    with pytest.raises(ValidationError):  # wrong type
        OrderItemCreate(product_id=uuid4(), quantity="ten")


def test_non_blank_strings_stripped() -> None:
    pl = PriceListCreate(code="  RETAIL  ", name="Retail price list")
    assert pl.code == "RETAIL"
    with pytest.raises(ValidationError):
        ProductCreate(sku="   ", name="x", unit="pcs")


# ---------------------------------------------------------------------------
# Price authority / duplicate products
# ---------------------------------------------------------------------------

def test_order_item_input_rejects_unit_price() -> None:
    with pytest.raises(ValidationError):
        OrderItemCreate(product_id=uuid4(), quantity=1, unit_price=Decimal("500.00"))


def test_order_item_response_accepts_unit_price() -> None:
    r = OrderItemResponse(
        id=uuid4(),
        order_id=uuid4(),
        product_id=uuid4(),
        quantity=3,
        unit_price=Decimal("3500.00"),
        subtotal=Decimal("10500.00"),
        created_at=NOW,
    )
    assert r.subtotal == Decimal("10500.00")


def test_product_price_create_accepts_unit_price() -> None:
    p = ProductPriceCreate(
        price_list_id=uuid4(), product_id=uuid4(), unit_price=Decimal("3500.00")
    )
    assert p.unit_price == Decimal("3500.00")


def test_duplicate_product_ids_rejected() -> None:
    pid = str(uuid4())
    with pytest.raises(ValidationError) as exc_info:
        OrderCreate(
            customer_id=uuid4(),
            items=[{"product_id": pid, "quantity": 1}, {"product_id": pid, "quantity": 2}],
        )
    assert "duplicate product_id" in str(exc_info.value)


def test_different_product_ids_accepted() -> None:
    o = OrderCreate(
        customer_id=uuid4(),
        items=[
            {"product_id": str(uuid4()), "quantity": 1},
            {"product_id": str(uuid4()), "quantity": 2},
        ],
    )
    assert len(o.items) == 2


# ---------------------------------------------------------------------------
# Schema fields vs actual database columns (migration is source of truth)
# ---------------------------------------------------------------------------

def _table_columns(sql: str, table: str) -> set[str]:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
    assert match, f"table {table} not found in migration"
    columns = set()
    for line in match.group(1).splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        # column lines: start with a name, not a table-level constraint
        if line and not line.upper().startswith(("CONSTRAINT", "UNIQUE", "CHECK", "PRIMARY KEY", "FOREIGN KEY")):
            columns.add(line.split()[0])
    return columns


# model -> (table, application-only fields documented in schema modules)
FIELD_SOURCES = {
    CustomerCreate: ("customers", set()),
    CustomerUpdate: ("customers", set()),
    CustomerResponse: ("customers", set()),
    ProductCreate: ("products", set()),
    ProductUpdate: ("products", set()),
    ProductResponse: ("products", set()),
    ProductAliasCreate: ("product_aliases", set()),
    ProductAliasResponse: ("product_aliases", set()),
    InventoryResponse: ("inventory", set()),
    PriceListCreate: ("price_lists", set()),
    PriceListUpdate: ("price_lists", set()),
    PriceListResponse: ("price_lists", set()),
    ProductPriceCreate: ("product_prices", set()),
    ProductPriceUpdate: ("product_prices", set()),
    ProductPriceResponse: ("product_prices", set()),
    OrderCreate: ("orders", {"items"}),  # aggregate input, no single column
    OrderResponse: ("orders", set()),
    OrderItemCreate: ("order_items", set()),  # unit_price absent by design
    OrderItemResponse: ("order_items", set()),
}


def test_schema_fields_match_database_columns() -> None:
    sql = MIGRATION.read_text()
    for model, (table, app_only) in FIELD_SOURCES.items():
        columns = _table_columns(sql, table)
        fields = set(model.model_fields)
        unexplained = fields - app_only - columns
        assert not unexplained, f"{model.__name__}: fields not in {table}: {unexplained}"


def test_order_status_enum_matches_db_check() -> None:
    sql = MIGRATION.read_text()
    match = re.search(r"CHECK \(status IN \((.*?)\)\)", sql)
    assert match
    db_values = {v.strip().strip("'") for v in match.group(1).split(",")}
    assert {s.value for s in OrderStatus} == db_values
