"""ToolContext tests, the application-owned trust anchor.

Under test: defaults (no approval by default), immutability, the
approval-granting copy, and that customer identity is a typed UUID the
application supplies, there is no path from model output into this
object.
"""

import uuid

import pytest

from app.tools.context import ToolContext

CUSTOMER_ID = uuid.uuid4()


def test_defaults_have_no_confirmation_approval():
    context = ToolContext(customer_id=CUSTOMER_ID)
    assert context.confirm_order_approved is False
    assert context.conversation_id is None
    assert context.metadata == {}


def test_context_is_frozen():
    context = ToolContext(customer_id=CUSTOMER_ID)
    with pytest.raises(AttributeError):
        context.confirm_order_approved = True  # type: ignore[misc]


def test_with_confirm_order_approval_returns_new_context():
    base = ToolContext(customer_id=CUSTOMER_ID, conversation_id="wa-123")
    approved = base.with_confirm_order_approval()
    assert approved is not base
    assert approved.confirm_order_approved is True
    assert approved.customer_id == CUSTOMER_ID
    assert approved.conversation_id == "wa-123"
    assert base.confirm_order_approved is False  # original stays unapproved


def test_customer_identity_is_application_supplied_uuid():
    context = ToolContext(customer_id=CUSTOMER_ID, conversation_id="wa-123")
    assert context.customer_id == CUSTOMER_ID
    # caller may pass the UUID-shaped string; it is coerced, typed identity
    coerced = ToolContext(customer_id=str(CUSTOMER_ID))
    assert coerced.customer_id == CUSTOMER_ID
    with pytest.raises(ValueError):
        ToolContext(customer_id="not-a-uuid")  # type: ignore[arg-type]
