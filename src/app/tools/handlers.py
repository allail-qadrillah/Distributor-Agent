"""Tool handlers, bridge validated arguments + trusted context to services.

One handler per tool. The handler's only job is translation:
- build the INTERNAL service request from validated args (model-facing
  subset) + trusted context (customer identity, approval);
- call the existing business service;
- translate the service response into a model-safe payload dict (plain
  JSON types; Decimal becomes str to stay exact);
- refuse authorization-sensitive operations that lack trusted approval.

Handlers never execute SQL, never touch repositories, never open
transactions (services own all of that), and never re-implement business
rules, validation of prices, stock, credit, and status stays in
ProductInquiryService and OrderService.

Service failures propagate as service exceptions; the executor maps them
via map_service_error into model-safe tool errors.
"""

from dataclasses import dataclass

from app.schemas.order import OrderCreate, OrderDetailResponse, OrderItemCreate
from app.schemas.product_inquiry import (
    ProductInquiryItem,
    ProductInquiryRequest,
)
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
    ServiceError,
)
from app.services.order import OrderService
from app.services.product_inquiry import ProductInquiryService
from app.tools.arguments import (
    ConfirmOrderArgs,
    CreateDraftOrderArgs,
    SearchProductsArgs,
)
from app.tools.context import ToolContext
from app.tools.exceptions import ToolHandlerError


@dataclass(frozen=True)
class ToolServices:
    """The business services tools are allowed to reach, injected by the
    application exactly like every other service dependency."""

    product_inquiry: ProductInquiryService
    orders: OrderService


def execute_search_products(
    args: SearchProductsArgs, context: ToolContext, services: ToolServices
) -> dict:
    """search_products → ProductInquiryService.search_products.

    Customer identity comes from trusted context only; the search term is
    the single model-provided value.
    """
    response = services.product_inquiry.search_products(
        ProductInquiryRequest(
            customer_id=context.customer_id, search_term=args.search_term
        )
    )
    return {"products": [_inquiry_item(p) for p in response.products]}


def execute_create_draft_order(
    args: CreateDraftOrderArgs, context: ToolContext, services: ToolServices
) -> dict:
    """create_draft_order → OrderService.create_draft_order.

    OrderCreate is built HERE from validated args + trusted customer_id;
    its own validators run again on the complete object (duplicate
    product rule lives in OrderCreate, nowhere else). Unit prices are
    resolved by the service from the customer's price list, args carry
    none and can't.
    """
    order = OrderCreate(
        customer_id=context.customer_id,
        notes=args.notes,
        items=[
            OrderItemCreate(product_id=item.product_id, quantity=item.quantity)
            for item in args.items
        ],
    )
    detail = services.orders.create_draft_order(order)
    return _order_summary(detail)


def execute_confirm_order(
    args: ConfirmOrderArgs, context: ToolContext, services: ToolServices
) -> dict:
    """confirm_order → OrderService.confirm_order (guarded twice).

    Guard 1: trusted explicit approval, the user approved exactly ONE
    order (context.approved_order_id, set only by application logic
    via ToolContext.with_confirm_order_approval()); confirming a
    different order, even the same customer's, is refused.
    Guard 2: ownership, the draft must belong to the trusted customer.
    customer_id is immutable on an order row, so the read-then-confirm
    sequence cannot be raced into confirming another customer's order;
    status races are impossible inside confirm_order's own guarded
    transition.
    """
    if context.approved_order_id is None or context.approved_order_id != args.order_id:
        raise ToolHandlerError(
            "unauthorized",
            "Konfirmasi pesanan membutuhkan persetujuan eksplisit pelanggan "
            "untuk pesanan ini terlebih dahulu. Konfirmasi tidak dijalankan.",
            {"order_id": str(args.order_id)},
        )
    draft = services.orders.get_order(args.order_id)
    if draft.customer_id != context.customer_id:
        raise ToolHandlerError(
            "unauthorized",
            "Pesanan draft ini bukan milik pelanggan yang sedang dilayani.",
            {"order_id": str(args.order_id)},
        )
    confirmed = services.orders.confirm_order(args.order_id)
    return _order_summary(confirmed)


def _inquiry_item(product: ProductInquiryItem) -> dict:
    """One search hit, model-safe: Decimal price as exact string."""
    return {
        "product_id": str(product.product_id),
        "sku": product.sku,
        "name": product.name,
        "unit_price": str(product.unit_price),
        "available_stock": product.available_stock,
    }


def _order_summary(detail: OrderDetailResponse) -> dict:
    """Draft or confirmed order summary, model-safe: money as strings."""
    return {
        "order_id": str(detail.id),
        "order_number": detail.order_number,
        "status": detail.status.value,
        "total_amount": str(detail.total_amount),
        "notes": detail.notes,
        "confirmed_at": (
            detail.confirmed_at.isoformat() if detail.confirmed_at else None
        ),
        "items": [
            {
                "product_id": str(item.product_id),
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "subtotal": str(item.subtotal),
            }
            for item in detail.items
        ],
    }


def map_service_error(exc: ServiceError) -> ToolHandlerError:
    """Translate one service business-rule failure into a model-safe
    tool error: stable category, Bahasa message the model can relay,
    identifying detail only (ids, quantities, limits)."""
    if isinstance(exc, CustomerNotFoundError):
        return ToolHandlerError(
            "not_found",
            "Pelanggan tidak ditemukan.",
            {"customer_id": str(exc.customer_id)},
        )
    if isinstance(exc, InactiveCustomerError):
        return ToolHandlerError(
            "inactive",
            "Pelanggan sedang tidak aktif.",
            {"customer_id": str(exc.customer_id)},
        )
    if isinstance(exc, ProductNotFoundError):
        return ToolHandlerError(
            "not_found", "Produk tidak ditemukan.", {"product_id": str(exc.product_id)}
        )
    if isinstance(exc, InactiveProductError):
        return ToolHandlerError(
            "inactive", "Produk sudah tidak aktif.", {"product_id": str(exc.product_id)}
        )
    if isinstance(exc, ProductPriceNotFoundError):
        return ToolHandlerError(
            "missing_data",
            "Produk belum memiliki harga di daftar harga pelanggan.",
            {
                "product_id": str(exc.product_id),
                "price_list_id": str(exc.price_list_id),
            },
        )
    if isinstance(exc, PriceListNotFoundError):
        return ToolHandlerError(
            "missing_data",
            "Daftar harga tidak ditemukan.",
            {"price_list": str(exc.price_list)},
        )
    if isinstance(exc, InventoryNotFoundError):
        return ToolHandlerError(
            "missing_data",
            "Data stok produk belum tersedia.",
            {"product_id": str(exc.product_id)},
        )
    if isinstance(exc, OrderNotFoundError):
        return ToolHandlerError(
            "not_found", "Pesanan tidak ditemukan.", {"order_id": str(exc.order_id)}
        )
    if isinstance(exc, InvalidOrderStatusError):
        return ToolHandlerError(
            "business_rule",
            "Status pesanan tidak mengizinkan operasi ini.",
            {
                "order_id": str(exc.order_id),
                "current_status": str(exc.current_status),
                "target_status": exc.target_status,
            },
        )
    if isinstance(exc, EmptyOrderError):
        return ToolHandlerError(
            "business_rule",
            "Pesanan tidak memiliki item untuk dikonfirmasi.",
            {"order_id": str(exc.order_id)},
        )
    if isinstance(exc, OrderTotalUpdateError):
        # internal consistency failure inside the confirm transaction:
        # relayable as a generic failure, but explicitly mapped so a new
        # subclass can never fall through the fallback silently
        return ToolHandlerError(
            "execution_error",
            "Pembaruan total pesanan gagal.",
            {"order_id": str(exc.order_id)},
        )
    if isinstance(exc, CreditLimitNotConfiguredError):
        return ToolHandlerError(
            "missing_data",
            "Limit kredit pelanggan belum dikonfigurasi.",
            {"customer_id": str(exc.customer_id)},
        )
    if isinstance(exc, CreditLimitExceededError):
        return ToolHandlerError(
            "business_rule",
            "Konfirmasi ditolak: limit kredit terlampaui.",
            {
                "customer_id": str(exc.customer_id),
                "credit_limit": str(exc.credit_limit),
                "projected_exposure": str(exc.projected_exposure),
            },
        )
    if isinstance(exc, InsufficientStockError):
        return ToolHandlerError(
            "business_rule",
            "Konfirmasi ditolak: stok tidak mencukupi.",
            {
                "product_id": str(exc.product_id),
                "requested": exc.requested,
                "available": exc.available,
            },
        )
    return ToolHandlerError("execution_error", "Operasi bisnis gagal.")
