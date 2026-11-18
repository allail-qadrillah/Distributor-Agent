from decimal import Decimal
from uuid import UUID

from app.schemas.order import OrderStatus


class ServiceError(Exception):
    pass


class CustomerNotFoundError(ServiceError):
    def __init__(self, customer_id: UUID) -> None:
        super().__init__(f"customer not found: {customer_id}")
        self.customer_id = customer_id


class InactiveCustomerError(ServiceError):
    def __init__(self, customer_id: UUID) -> None:
        super().__init__(f"customer is inactive: {customer_id}")
        self.customer_id = customer_id


class PriceListNotFoundError(ServiceError):
    def __init__(self, price_list: UUID | str) -> None:
        super().__init__(f"price list not found: {price_list}")
        self.price_list = price_list


class ProductNotFoundError(ServiceError):
    def __init__(self, product_id: UUID) -> None:
        super().__init__(f"product not found: {product_id}")
        self.product_id = product_id


class InactiveProductError(ServiceError):
    def __init__(self, product_id: UUID) -> None:
        super().__init__(f"product is inactive: {product_id}")
        self.product_id = product_id


class ProductPriceNotFoundError(ServiceError):
    def __init__(self, product_id: UUID, price_list_id: UUID) -> None:
        super().__init__(
            f"product {product_id} has no price in price list {price_list_id}"
        )
        self.product_id = product_id
        self.price_list_id = price_list_id


class OrderNotFoundError(ServiceError):
    def __init__(self, order_id: UUID) -> None:
        super().__init__(f"order not found: {order_id}")
        self.order_id = order_id


class OrderTotalUpdateError(ServiceError):
    def __init__(self, order_id: UUID) -> None:
        super().__init__(f"order total update failed: no order {order_id}")
        self.order_id = order_id


class InvalidOrderStatusError(ServiceError):
    def __init__(
        self, order_id: UUID, current_status: OrderStatus | str, target_status: str
    ) -> None:
        super().__init__(
            f"order {order_id} cannot transition to {target_status}: "
            f"current status is {current_status}"
        )
        self.order_id = order_id
        self.current_status = current_status
        self.target_status = target_status


class EmptyOrderError(ServiceError):
    def __init__(self, order_id: UUID) -> None:
        super().__init__(f"order {order_id} has no items to confirm")
        self.order_id = order_id


class CreditLimitNotConfiguredError(ServiceError):
    def __init__(self, customer_id: UUID) -> None:
        super().__init__(f"customer {customer_id} has no credit limit configured")
        self.customer_id = customer_id


class CreditLimitExceededError(ServiceError):
    def __init__(
        self,
        customer_id: UUID,
        credit_limit: Decimal,
        projected_exposure: Decimal,
    ) -> None:
        super().__init__(
            f"credit limit exceeded for customer {customer_id}: "
            f"projected exposure {projected_exposure} > limit {credit_limit}"
        )
        self.customer_id = customer_id
        self.credit_limit = credit_limit
        self.projected_exposure = projected_exposure


class InventoryNotFoundError(ServiceError):
    def __init__(self, product_id: UUID) -> None:
        super().__init__(f"no inventory row for product {product_id}")
        self.product_id = product_id


class InsufficientStockError(ServiceError):
    def __init__(self, product_id: UUID, requested: int, available: int) -> None:
        super().__init__(
            f"insufficient stock for product {product_id}: "
            f"requested {requested}, available {available}"
        )
        self.product_id = product_id
        self.requested = requested
        self.available = available
