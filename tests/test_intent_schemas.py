"""IntentClassification schema validation — the classifier output contract."""

import pytest
from pydantic import ValidationError

from app.intent.schemas import IntentClassification, IntentContextMessage


def test_valid_classification() -> None:
    result = IntentClassification.model_validate(
        {"category": "order_related", "confidence": 0.9, "reason": "menanyakan stok"}
    )
    assert result.category == "order_related"
    assert result.confidence == 0.9


def test_invalid_category_rejected() -> None:
    with pytest.raises(ValidationError):
        IntentClassification.model_validate(
            {"category": "chitchat", "confidence": 0.9, "reason": "x"}
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0])
def test_confidence_out_of_range_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        IntentClassification.model_validate(
            {"category": "ambiguous", "confidence": confidence, "reason": "x"}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"category": "human_request"},  # missing confidence + reason
        {"confidence": 0.5, "reason": "x"},  # missing category
        {
            "category": "out_of_scope",
            "confidence": 0.5,
            "reason": "x",
            "extra": 1,
        },  # unknown field
        {
            "category": "out_of_scope",
            "confidence": 0.5,
            "reason": "   ",
        },  # blank reason
    ],
)
def test_incomplete_or_extra_fields_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        IntentClassification.model_validate(payload)


def test_context_message_roles() -> None:
    assert IntentContextMessage(role="user", content="iya").role == "user"
    with pytest.raises(ValidationError):
        IntentContextMessage(role="tool", content="data")
