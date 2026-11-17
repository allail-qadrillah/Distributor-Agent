"""ToolContext tests, the application-owned trust anchor.

Under test: defaults (no approval by default), immutability, the
approval-granting copy bound to exactly one order, boundary coercion,
and that customer identity is a typed UUID the application supplies,
there is no path from model output into this object.
"""

import uuid

import pytest

from app.tools.context import ToolContext

CUSTOMER_ID = uuid.uuid4()
ORDER_ID = uuid.uuid4()


def test_defaults_have_no_confirmation_approval():
    context = ToolContext(customer_id=CUSTOMER_ID)
    assert context.approved_order_id is None
    assert context.conversation_id is None
    assert context.metadata == {}


def test_context_is_frozen():
    context = ToolContext(customer_id=CUSTOMER_ID)
    with pytest.raises(AttributeError):
        context.approved_order_id = ORDER_ID  # type: ignore[misc]


def test_with_confirm_order_approval_binds_exactly_one_order():
    base = ToolContext(customer_id=CUSTOMER_ID, conversation_id="wa-123")
    approved = base.with_confirm_order_approval(ORDER_ID)
    assert approved is not base
    assert approved.approved_order_id == ORDER_ID  # approval names ONE order
    assert approved.customer_id == CUSTOMER_ID
    assert approved.conversation_id == "wa-123"
    assert base.approved_order_id is None  # original stays unapproved


def test_approved_order_id_is_coerced_like_customer_identity():
    context = ToolContext(customer_id=CUSTOMER_ID, approved_order_id=str(ORDER_ID))
    assert context.approved_order_id == ORDER_ID


def test_customer_identity_is_application_supplied_uuid():
    context = ToolContext(customer_id=CUSTOMER_ID, conversation_id="wa-123")
    assert context.customer_id == CUSTOMER_ID
    # caller may pass the UUID-shaped string; it is coerced, typed identity
    coerced = ToolContext(customer_id=str(CUSTOMER_ID))
    assert coerced.customer_id == CUSTOMER_ID
    with pytest.raises(ValueError):
        ToolContext(customer_id="not-a-uuid")  # type: ignore[arg-type]
