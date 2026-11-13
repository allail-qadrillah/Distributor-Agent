"""Customer repository — read-only database access for the customers table.

Rules:
- Database operations only. No business rules: no credit logic, no pricing,
  no inventory, no order workflows, no LLM, no API.
- Parameterized SQL only (never string-formatted).
- Returns Pydantic schemas or None; never raw cursors.
- Psycopg exceptions propagate to the caller (never swallowed) — mapping
  them to user-safe responses is the API layer's job.
"""

from uuid import UUID

from app.database.connection import get_connection
from app.schemas.customer import CustomerResponse

# Exactly the CustomerResponse columns — nothing more
_COLUMNS = (
    "id, code, name, phone, address, credit_limit, outstanding_balance, "
    "price_list_id, is_active, created_at, updated_at"
)
_GET_BY_ID = f"SELECT {_COLUMNS} FROM customers WHERE id = %s"
_GET_BY_PHONE = f"SELECT {_COLUMNS} FROM customers WHERE phone = %s"


class CustomerRepository:
    def get_by_id(self, customer_id: UUID) -> CustomerResponse | None:
        """Return the customer with this id, or None."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_GET_BY_ID, (customer_id,))
                row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None

    def get_by_phone(self, phone: str) -> CustomerResponse | None:
        """Return the customer with this phone (E.164), or None."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_GET_BY_PHONE, (phone,))
                row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None
