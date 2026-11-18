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
        matches = self._products.search_by_exact_sku(conn, search_term)
        if matches:
            return matches
        return self._products.search_by_name_or_alias(conn, search_term)

    def _build_item(
        self, conn: psycopg.Connection, product: ProductResponse, price_list_id: UUID
    ) -> ProductInquiryItem:
        price = self._prices.get_product_price(conn, price_list_id, product.id)
        if price is None:
            raise ProductPriceNotFoundError(product.id, price_list_id)
        inventory = self._inventory.get_by_product_id(conn, product.id)
        if inventory is None:
            raise InventoryNotFoundError(product.id)
        return ProductInquiryItem(
            product_id=product.id,
            sku=product.sku,
            name=product.name,
            unit_price=price.unit_price,
            available_stock=inventory.available_stock,
        )
