"""Customer repository — read-only database access for the customers table.

Rules:
- Database operations only. No business rules: no credit logic, no pricing,
  no inventory, no order workflows, no LLM, no API.
- Parameterized SQL only (never string-formatted).
- Returns Pydantic schemas or None; never raw cursors.
- Psycopg exceptions propagate to the caller (never swallowed) — mapping
  them to user-safe responses is the API layer's job.

Connection injection (same contract as InventoryRepository/OrderRepository):
- Every method receives the caller's connection and uses it for all
  operations. This module never opens a connection and never manages
  transaction lifecycle: no BEGIN, no COMMIT, no ROLLBACK, no close. The
  caller owns the transaction.
- The connection must use dict_row (app.database.connection.connect and
  transaction() provide it).
"""

from uuid import UUID

import psycopg

from app.schemas.customer import CustomerResponse

# Exactly the CustomerResponse columns — nothing more
_COLUMNS = (
    "id, code, name, phone, address, credit_limit, outstanding_balance, "
    "price_list_id, is_active, created_at, updated_at"
)
_GET_BY_ID = f"SELECT {_COLUMNS} FROM customers WHERE id = %s"
_GET_BY_PHONE = f"SELECT {_COLUMNS} FROM customers WHERE phone = %s"
_GET_FOR_UPDATE = f"SELECT {_COLUMNS} FROM customers WHERE id = %s FOR UPDATE"


class CustomerRepository:
    def get_by_id(
        self, conn: psycopg.Connection, customer_id: UUID
    ) -> CustomerResponse | None:
        """Return the customer with this id, or None.

        No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_BY_ID, (customer_id,))
            row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None

    def get_by_phone(
        self, conn: psycopg.Connection, phone: str
    ) -> CustomerResponse | None:
        """Return the customer with this phone (E.164), or None.

        No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_BY_PHONE, (phone,))
            row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None

    def get_for_update(
        self, conn: psycopg.Connection, customer_id: UUID
    ) -> CustomerResponse | None:
        """Return the customer with this id, row-locked, or None.

        SELECT ... FOR UPDATE serializes concurrent credit-limit checks
        for orders of the same customer. Migration 001 lock order: the
        customer row is always locked FIRST. Under READ COMMITTED a
        lock wait re-reads the latest committed row version, so callers
        validate fresh data. No commit/rollback: the caller owns the
        transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_FOR_UPDATE, (customer_id,))
            row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None
