"""Product and product alias schemas. Columns mirror products/product_aliases."""
from datetime import datetime
from uuid import UUID

from app.schemas.common import BaseSchema, NonBlankStr


class ProductCreate(BaseSchema):
    sku: NonBlankStr
    name: NonBlankStr
    unit: NonBlankStr  # pcs / carton / dus ...
    is_active: bool = True


class ProductUpdate(BaseSchema):
    sku: NonBlankStr | None = None
    name: NonBlankStr | None = None
    unit: NonBlankStr | None = None
    is_active: bool | None = None


class ProductResponse(BaseSchema):
    id: UUID
    sku: str
    name: str
    unit: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProductAliasCreate(BaseSchema):
    product_id: UUID
    alias: NonBlankStr  # global case-insensitive uniqueness enforced by DB index


class ProductAliasResponse(BaseSchema):
    id: UUID
    product_id: UUID
    alias: str
    created_at: datetime
