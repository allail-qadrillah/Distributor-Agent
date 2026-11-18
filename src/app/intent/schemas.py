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

    category: IntentCategory
    confidence: StrictConfidence = Field(ge=0.0, le=1.0)
    reason: NonBlankStr


class IntentContextMessage(BaseSchema):

    role: Literal["user", "assistant"]
    content: NonBlankStr


class IntentCaseContextMessage(BaseSchema):

    role: Literal["user", "assistant"]
    content: NonBlankStr


class IntentCase(BaseSchema):

    id: NonBlankStr
    message: NonBlankStr
    expected: IntentCategory
    context: list[IntentCaseContextMessage] | None = None
    description: str | None = None
    tags: list[NonBlankStr] | None = None
