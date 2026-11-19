from decimal import Decimal
from uuid import UUID

import psycopg

from app.database.connection import transaction
from app.repositories.customer import CustomerRepository
from app.repositories.inventory import InventoryRepository
from app.repositories.order import OrderRepository
from app.repositories.price import PriceRepository
from app.repositories.product import ProductRepository
from app.schemas.customer import CustomerResponse
from app.schemas.order import (
    OrderCreate,
    OrderDetailResponse,
    OrderItemInsert,
    OrderStatus,
)
from app.schemas.price_list import PriceListResponse
from app.services.exceptions import (
    CreditLimitExceededError,
    CreditLimitNotConfiguredError,
    CustomerNotFoundError,
    EmptyOrderError,
    InactiveCustomerError,
    InactiveProductError,
    InsufficientStockError,
    InvalidOrderStatusError,
    InventoryNotFoundError,
    OrderNotFoundError,
    OrderTotalUpdateError,
    ProductNotFoundError,
    ProductPriceNotFoundError,
)
from app.services.price_list_resolver import resolve_price_list


class OrderService:
    def __init__(
        self,
        customer_repository: CustomerRepository,
        product_repository: ProductRepository,
        price_repository: PriceRepository,
        order_repository: OrderRepository,
        inventory_repository: InventoryRepository,
    ) -> None:
        self._customers = customer_repository
        self._products = product_repository
        self._prices = price_repository
        self._orders = order_repository
        self._inventory = inventory_repository

    def create_draft_order(self, order: OrderCreate) -> OrderDetailResponse:
        with transaction() as conn:
            customer = self._require_active_customer(conn, order.customer_id)
            price_list = resolve_price_list(conn, self._prices, customer)
            items = self._resolve_items(conn, order, price_list)

            created = self._orders.create_order(conn, order)
            stored_items = self._orders.create_order_items(conn, created.id, items)
            total = sum((item.subtotal for item in stored_items), Decimal("0.00"))
            if self._orders.update_total_amount(conn, created.id, total) is None:
                raise OrderTotalUpdateError(created.id)

            detail = self._orders.get_by_id(conn, created.id)
            if detail is None:
                raise OrderNotFoundError(created.id)
            return detail

    def confirm_order(self, order_id: UUID) -> OrderDetailResponse:
        with transaction() as conn:
            existing = self._orders.get_by_id(conn, order_id)
            if existing is None:
                raise OrderNotFoundError(order_id)

            customer = self._customers.get_for_update(conn, existing.customer_id)
            if customer is None:
                raise CustomerNotFoundError(existing.customer_id)

            order = self._orders.get_for_update(conn, order_id)
            if order is None:
                raise OrderNotFoundError(order_id)
            if order.status is not OrderStatus.DRAFT:
                raise InvalidOrderStatusError(
                    order_id, order.status, OrderStatus.CONFIRMED.value
                )
            if not order.items:
                raise EmptyOrderError(order_id)

            self._validate_credit(conn, customer, order)
            self._reserve_inventory(conn, order)

            if self._orders.mark_confirmed(conn, order_id) is None:
                raise InvalidOrderStatusError(
                    order_id, order.status, OrderStatus.CONFIRMED.value
                )

            detail = self._orders.get_by_id(conn, order_id)
            if detail is None:
                raise OrderNotFoundError(order_id)
            return detail

    def get_order(self, order_id: UUID) -> OrderDetailResponse:
        with transaction() as conn:
            detail = self._orders.get_by_id(conn, order_id)
            if detail is None:
                raise OrderNotFoundError(order_id)
            return detail

    def _validate_credit(
        self,
        conn: psycopg.Connection,
        customer: CustomerResponse,
        order: OrderDetailResponse,
    ) -> None:
        if customer.credit_limit is None:
            raise CreditLimitNotConfiguredError(customer.id)
        confirmed_total = self._orders.sum_confirmed_totals(conn, customer.id)
        projected = customer.outstanding_balance + confirmed_total + order.total_amount
        if projected > customer.credit_limit:
            raise CreditLimitExceededError(
                customer.id, customer.credit_limit, projected
            )

    def _reserve_inventory(
        self, conn: psycopg.Connection, order: OrderDetailResponse
    ) -> None:
        product_ids = sorted(item.product_id for item in order.items)
        inventories = self._inventory.lock_by_product_ids(conn, product_ids)
        by_product = {inv.product_id: inv for inv in inventories}
        for product_id in product_ids:
            if product_id not in by_product:
                raise InventoryNotFoundError(product_id)

        for item in order.items:  # validate all before touching any row
            inventory = by_product[item.product_id]
            if inventory.available_stock < item.quantity:
                raise InsufficientStockError(
                    item.product_id, item.quantity, inventory.available_stock
                )

        for item in order.items:  # every item passed: reserve
            if (
                self._inventory.reserve_stock(conn, item.product_id, item.quantity)
                is None
            ):
                raise InventoryNotFoundError(item.product_id)

    def _require_active_customer(
        self, conn: psycopg.Connection, customer_id: UUID
    ) -> CustomerResponse:
        customer = self._customers.get_by_id(conn, customer_id)
        if customer is None:
            raise CustomerNotFoundError(customer_id)
        if not customer.is_active:
            raise InactiveCustomerError(customer_id)
        return customer

    def _resolve_items(
        self,
        conn: psycopg.Connection,
        order: OrderCreate,
        price_list: PriceListResponse,
    ) -> list[OrderItemInsert]:
        items: list[OrderItemInsert] = []
        for item in order.items:
            product = self._products.get_by_id(conn, item.product_id)
            if product is None:
                raise ProductNotFoundError(item.product_id)
            if not product.is_active:
                raise InactiveProductError(item.product_id)
            price = self._prices.get_product_price(conn, price_list.id, product.id)
            if price is None:
                raise ProductPriceNotFoundError(product.id, price_list.id)
            items.append(
                OrderItemInsert(
                    product_id=product.id,
                    quantity=item.quantity,
                    unit_price=price.unit_price,
                )
            )
        return items
