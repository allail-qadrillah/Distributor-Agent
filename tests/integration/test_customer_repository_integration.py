"""CustomerRepository live smoke test — NOT run by default.

Manual run (read-light, self-cleaning, touches only its own throwaway row):

    RUN_DB_SMOKE=1 uv run pytest tests/integration/test_customer_repository_integration.py -v

Requirements:
- Migration 001 applied to Supabase
- .env SUPABASE_DB_URL configured and reachable

Safety:
- Creates exactly one throwaway customer (random code/phone), never reuses
  or modiies existing records, deletes its own row in teardown.
- Read operations only otherwise. Nothing destructive outside the throwaway row.
"""

import os
from decimal import Decimal
from uuid import uuid4

import pytest

from app.database.connection import transaction
from app.repositories.customer import CustomerRepository
from app.schemas.customer import CustomerResponse

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="live Supabase smoke test: set RUN_DB_SMOKE=1 to run",
)


def test_get_by_id_and_phone_roundtrip():
    repo = CustomerRepository()
    cust_id = uuid4()
    code = f"SMOKE-{cust_id.hex[:8]}"
    # numeric-only: UUID hex can contain a-f, which the E.164 CHECK rejects.
    # Derived from the UUID int -> always digits, zero-padded to 9.
    phone = f"+62899{cust_id.int % 1_000_000_000:09d}"

    inserted = False
    try:
        with transaction() as conn:  # insert throwaway row
            conn.execute(
                "INSERT INTO customers (id, code, name, phone, credit_limit) "
                "VALUES (%s, %s, %s, %s, %s)",
                (cust_id, code, "Smoke Test Customer", phone, Decimal("1000.00")),
            )
        inserted = True  # only after the insert transaction committed
        by_id = repo.get_by_id(cust_id)
        assert isinstance(by_id, CustomerResponse)
        assert by_id.phone == phone
        assert by_id.credit_limit == Decimal("1000.00")

        by_phone = repo.get_by_phone(phone)
        assert by_phone is not None and by_phone.id == cust_id

        assert repo.get_by_id(uuid4()) is None  # typo-safety: unknown id
    finally:
        if inserted:
            with transaction() as conn:  # cleanup own row only
                conn.execute("DELETE FROM customers WHERE id = %s", (cust_id,))
