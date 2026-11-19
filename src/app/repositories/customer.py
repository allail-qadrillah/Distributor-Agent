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
        with conn.cursor() as cur:
            cur.execute(_GET_BY_ID, (customer_id,))
            row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None

    def get_by_phone(
        self, conn: psycopg.Connection, phone: str
    ) -> CustomerResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_BY_PHONE, (phone,))
            row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None

    def get_for_update(
        self, conn: psycopg.Connection, customer_id: UUID
    ) -> CustomerResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_FOR_UPDATE, (customer_id,))
            row = cur.fetchone()
        return CustomerResponse.model_validate(row) if row else None
