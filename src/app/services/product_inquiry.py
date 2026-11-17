"""Product inquiry service — "what is X, at my price, and is it in stock?"

Rules:
- Read-only: no writes, no stock reservation, no inventory mutation, no
  order behavior. The transaction() boundary exists so one inquiry sees
  one consistent snapshot (customer, price list, prices, inventory on the
  same connection); its commit writes nothing.
- Services own business rules and orchestration; repositories own SQL.
  The service never executes SQL and never creates connections.
- Customer must exist (CustomerNotFoundError) — GENERAL pricing is a
  fallback for a customer's unassigned price list, never for a missing
  customer.
- Search policy: an exact case-insensitive SKU match wins and short-circuits
  the broader name/alias search; otherwise the existing name/alias partial
  search runs. All active candidates are returned — never an arbitrary
  first pick.
- Missing data is an error, not a default: a product without a price in
  the resolved list raises ProductPriceNotFoundError; a product without an
  inventory row raises InventoryNotFoundError. An inventory row with zero
  available stock is a legitimate answer and returns zero.
"""

from uuid import UUID

import psycopg

from app.database.connection import transaction
from app.repositories.customer import CustomerRepository
from app.repositories.inventory import InventoryRepository
from app.repositories.price import PriceRepository
from app.repositories.product import ProductRepository
from app.schemas.customer import CustomerResponse
from app.schemas.product import ProductResponse
from app.schemas.product_inquiry import (
    ProductInquiryItem,
    ProductInquiryRequest,
    ProductInquiryResponse,
)
from app.services.exceptions import (
    CustomerNotFoundError,
    InventoryNotFoundError,
    ProductPriceNotFoundError,
)
from app.services.price_list_resolver import resolve_price_list


class ProductInquiryService:
    """Orchestrates product inquiry over injected repositories."""

    def __init__(
        self,
        customer_repository: CustomerRepository,
        product_repository: ProductRepository,
        price_repository: PriceRepository,
        inventory_repository: InventoryRepository,
    ) -> None:
        self._customers = customer_repository
        self._products = product_repository
        self._prices = price_repository
        self._inventory = inventory_repository

    def search_products(self, request: ProductInquiryRequest) -> ProductInquiryResponse:
        """Resolve one customer's search term into priced, stocked products.

        The whole workflow runs inside one transaction(): customer
        validation, price-list resolution, product search, and the
        per-product price and inventory lookups all read the same
        consistent snapshot.
        """
        with transaction() as conn:
            customer = self._require_customer(conn, request.customer_id)
            price_list = resolve_price_list(conn, self._prices, customer)
            products = self._search_products(conn, request.search_term)
            items = [
                self._build_item(conn, product, price_list.id) for product in products
            ]
            return ProductInquiryResponse(products=items)

    def _require_customer(
        self, conn: psycopg.Connection, customer_id: UUID
    ) -> CustomerResponse:
        customer = self._customers.get_by_id(conn, customer_id)
        if customer is None:
            raise CustomerNotFoundError(customer_id)
        return customer

    def _search_products(
        self, conn: psycopg.Connection, search_term: str
    ) -> list[ProductResponse]:
        # exact SKU match first (case-insensitive, active only); the
        # broader name/alias search only when no SKU matches
        matches = self._products.search_by_exact_sku(conn, search_term)
        if matches:
            return matches
        return self._products.search_by_name_or_alias(conn, search_term)

    def _build_item(
        self, conn: psycopg.Connection, product: ProductResponse, price_list_id: UUID
    ) -> ProductInquiryItem:
        price = self._prices.get_product_price(conn, price_list_id, product.id)
        if price is None:
            # no silent GENERAL fallback: the customer's list is the price
            raise ProductPriceNotFoundError(product.id, price_list_id)
        inventory = self._inventory.get_by_product_id(conn, product.id)
        if inventory is None:
            # missing row is unknown stock, not zero stock
            raise InventoryNotFoundError(product.id)
        return ProductInquiryItem(
            product_id=product.id,
            sku=product.sku,
            name=product.name,
            unit_price=price.unit_price,
            available_stock=inventory.available_stock,
        )
