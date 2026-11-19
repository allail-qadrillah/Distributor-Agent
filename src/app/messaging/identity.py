import psycopg

from app.repositories.customer import CustomerRepository
from app.schemas.customer import CustomerResponse


class IdentityResolver:
    def __init__(self, customer_repository: CustomerRepository) -> None:
        self._customers = customer_repository

    def resolve(self, conn: psycopg.Connection, phone: str) -> CustomerResponse | None:
        return self._customers.get_by_phone(conn, phone)
