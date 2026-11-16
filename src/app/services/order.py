"""Order service — business workflows for the order lifecycle.

Current workflows: create_draft_order, confirm_order. Cancellation,
delivery, and payment are separate future workflows — none of them
belongs here.

Rules:
- Services own business rules and orchestration; repositories own SQL.
  The service never executes SQL and never creates connections.
- One transaction() per workflow: every repository call receives the
  same connection; commit happens only after all steps succeed; any
  failure rolls back the complete workflow. Commit, rollback, and
  connection lifecycle stay in app.database.connection — never here.
- Backend-resolved prices only: OrderCreate carries no prices; the
  unit_price snapshot comes from the customer's price list.
- Draft orders are side-effect free: no inventory reservation, no
  credit consumption, no outstanding_balance change (migration 001
  business rules). No inventory or credit validation either.
- Confirmation is all-or-nothing: credit validation, stock validation,
  reservation, and the guarded status transition all commit together or
  roll back together. Locking follows the migration 001 order: customer
  row first, then the order row, then inventory rows in ascending
  product_id (READ COMMITTED + SELECT ... FOR UPDATE, no SERIALIZABLE).
- Business-rule failures raise app.services.exceptions; psycopg errors
  propagate untouched for the API layer to map.
"""

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
    PriceListNotFoundError,
    ProductNotFoundError,
    ProductPriceNotFoundError,
)

# customers.price_list_id NULL => this list (migration 001 seed row)
GENERAL_PRICE_LIST_CODE = "GENERAL"


class OrderService:
    """Orchestrates order workflows over injected repositories.

    Dependencies are injected explicitly; the service never builds
    repositories, connections, or transactions itself.
    """

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
        """Create one draft order: validate, resolve prices, write, total.

        The whole workflow runs inside one transaction(): customer and
        product validation, price-list resolution, price lookups, order
        header, items, and the total update either all commit together
        or all roll back.
        """
        with transaction() as conn:
            customer = self._require_active_customer(conn, order.customer_id)
            price_list = self._resolve_price_list(conn, customer)
            items = self._resolve_items(conn, order, price_list)

            created = self._orders.create_order(
                conn, order
            )  # status: DB default 'draft'
            stored_items = self._orders.create_order_items(conn, created.id, items)
            # Decimal sum of database-generated subtotals — no float, no
            # Python-computed subtotals.
            total = sum((item.subtotal for item in stored_items), Decimal("0.00"))
            if self._orders.update_total_amount(conn, created.id, total) is None:
                raise OrderTotalUpdateError(created.id)

            detail = self._orders.get_by_id(conn, created.id)
            if detail is None:  # unreachable: same connection, row just written
                raise OrderNotFoundError(created.id)
            return detail

    def confirm_order(self, order_id: UUID) -> OrderDetailResponse:
        """Confirm a draft order: credit check, stock reservation, transition.

        One transaction with pessimistic locking in the migration 001
        order — customer row first (serializes credit checks for the
        same customer across orders), then the order row (re-read under
        lock; must still be draft), then all inventory rows in ascending
        product_id (serializes stock reservation across customers). Any
        failure rolls back everything: no partial reservation, no
        balance change, order stays draft.
        """
        with transaction() as conn:
            # unlocked read: only to learn which customer to lock
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
                # migration 001 service-level rule: confirmation requires
                # at least one item (not expressible as a SQL constraint)
                raise EmptyOrderError(order_id)

            self._validate_credit(conn, customer, order)
            self._reserve_inventory(conn, order)

            # guarded backstop: under the row lock the status cannot
            # have changed, but the UPDATE still only fires from draft
            if self._orders.mark_confirmed(conn, order_id) is None:
                raise InvalidOrderStatusError(
                    order_id, order.status, OrderStatus.CONFIRMED.value
                )

            detail = self._orders.get_by_id(conn, order_id)
            if detail is None:  # unreachable: same connection, row just written
                raise OrderNotFoundError(order_id)
            return detail

    def _validate_credit(
        self,
        conn: psycopg.Connection,
        customer: CustomerResponse,
        order: OrderDetailResponse,
    ) -> None:
        # NULL = not configured (migration 002): reject, never treat as
        # unlimited. Zero is a configured limit and flows into the check.
        if customer.credit_limit is None:
            raise CreditLimitNotConfiguredError(customer.id)
        confirmed_total = self._orders.sum_confirmed_totals(conn, customer.id)
        # delivered totals are already inside outstanding_balance, so
        # they are not double-counted here
        projected = customer.outstanding_balance + confirmed_total + order.total_amount
        if projected > customer.credit_limit:
            raise CreditLimitExceededError(
                customer.id, customer.credit_limit, projected
            )

    def _reserve_inventory(
        self, conn: psycopg.Connection, order: OrderDetailResponse
    ) -> None:
        """Lock, validate, then reserve — all items or none.

        Locks are acquired in ascending product_id (migration 001
        deadlock-avoidance order); every item is validated against the
        locked row's available_stock before any row is updated.
        """
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

    def _resolve_price_list(
        self, conn: psycopg.Connection, customer: CustomerResponse
    ) -> PriceListResponse:
        # customer's own list when assigned, GENERAL otherwise — never
        # anything derived from order input.
        if customer.price_list_id is not None:
            price_list = self._prices.get_price_list_by_id(conn, customer.price_list_id)
            source: UUID | str = customer.price_list_id
        else:
            price_list = self._prices.get_price_list_by_code(
                conn, GENERAL_PRICE_LIST_CODE
            )
            source = GENERAL_PRICE_LIST_CODE
        if price_list is None:
            raise PriceListNotFoundError(source)
        return price_list

    def _resolve_items(
        self,
        conn: psycopg.Connection,
        order: OrderCreate,
        price_list: PriceListResponse,
    ) -> list[OrderItemInsert]:
        """Validate every product and snapshot its backend-resolved price."""
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
