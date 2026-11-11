"""Shared annotated types and base config for all schemas.

Validation mirrors the database constraints in
src/app/database/migrations/001_initial_schema.sql — single source, no
duplicated rules in domain modules.
"""
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# numeric(14,2), CHECK (>= 0) — one definition for all money fields
Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]

# text with CHECK (length(btrim(x)) > 0) — strips whitespace like btrim
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# E.164: '+' then country code [1-9], max 15 digits total — same regex as
# the customers_phone_e164 CHECK constraint
PhoneStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True),
    Field(pattern=r"^\+[1-9][0-9]{1,14}$"),
]


class BaseSchema(BaseModel):
    """Base for all schemas.

    extra="forbid": unknown input fields are rejected, so DB-generated or
    system-managed values (subtotal, outstanding_balance, status...) can
    never be smuggled in as user input.

    from_attributes=True: response schemas can be built from DB row objects.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)
