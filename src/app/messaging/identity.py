"""Identity resolution: WhatsApp phone number -> registered customer.

Unknown sender policy lives in the processor; this resolver only answers
the question "is this E.164 phone a registered customer?" using the same
lookup the rest of the application trusts (customers.phone is UNIQUE
and E.164-checked in the database).
"""

import psycopg

from app.repositories.customer import CustomerRepository
from app.schemas.customer import CustomerResponse


class IdentityResolver:
    def __init__(self, customer_repository: CustomerRepository) -> None:
        self._customers = customer_repository

    def resolve(self, conn: psycopg.Connection, phone: str) -> CustomerResponse | None:
        """The registered customer for this phone, or None (unknown
        sender). No fake customer row is ever created here."""
        return self._customers.get_by_phone(conn, phone)
