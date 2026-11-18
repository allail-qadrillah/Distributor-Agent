"""Intent classifier schemas: categories, validated output, dataset cases.

IntentCategory and the category list are the single source of truth for
both the classifier prompt and the regression dataset. The four values
are the agreed Phase 3 vocabulary; adding one means updating
CATEGORY_PROMPT_LINES in classifier.py and the dataset together.
"""

from typing import Literal

from pydantic import Field, StrictFloat, StrictInt

from app.schemas.common import BaseSchema, NonBlankStr

# strict number: a string "0.9" in the model output is a type violation,
# not a value to coerce (external data is validated, never repaired)
StrictConfidence = StrictInt | StrictFloat

IntentCategory = Literal["order_related", "human_request", "out_of_scope", "ambiguous"]

INTENT_CATEGORIES: tuple[str, ...] = (
    "order_related",
    "human_request",
    "out_of_scope",
    "ambiguous",
)


class IntentClassification(BaseSchema):
    """Validated classifier output. Extra fields are rejected so a drifting
    provider response fails validation instead of silently passing."""

    category: IntentCategory
    confidence: StrictConfidence = Field(ge=0.0, le=1.0)
    reason: NonBlankStr


class IntentContextMessage(BaseSchema):
    """One conversation-context message sent to the classifier. Only user
    and assistant roles: the classifier never needs tool protocol detail."""

    role: Literal["user", "assistant"]
    content: NonBlankStr


class IntentCaseContextMessage(BaseSchema):
    """One conversation-context message in a regression dataset case."""

    role: Literal["user", "assistant"]
    content: NonBlankStr


class IntentCase(BaseSchema):
    """One regression dataset case (JSONL line).

    id must be unique within the dataset. context is the conversation
    history BEFORE the message (oldest first), used to test
    context-dependent classification. tags are free-form labels
    (informal, typo, mixed_language, ...).
    """

    id: NonBlankStr
    message: NonBlankStr
    expected: IntentCategory
    context: list[IntentCaseContextMessage] | None = None
    description: str | None = None
    tags: list[NonBlankStr] | None = None
