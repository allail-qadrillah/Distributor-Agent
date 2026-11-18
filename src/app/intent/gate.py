"""Intent gate: classification policy, the only place that decides
whether a message may reach the main agent.

Policy (agreed, fail closed):
- order_related with confidence >= threshold → allow the agent;
- order_related below threshold → human handoff;
- human_request / out_of_scope / ambiguous → human handoff;
- classifier timeout, provider error, invalid output → human handoff.

The gate OWNS the threshold; the classifier stays policy-free. The gate
never raises: every classifier failure is caught here and becomes a
handoff decision with the error class in the reason, so the message
processor has one uniform contract.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.intent.classifier import MessageIntentClassifier
from app.intent.exceptions import IntentClassifierError
from app.intent.schemas import (
    IntentCategory,
    IntentClassification,
    IntentContextMessage,
)

ALLOW_REASON = "order_related"
BELOW_THRESHOLD_REASON = "order_related_below_threshold"
DISABLED_REASON = "intent_gate_disabled"


@dataclass(frozen=True)
class IntentDecision:
    """allow_agent=True: the message may reach the main agent.
    classification is None only when the classifier did not run (disabled
    or failed). reason is a stable machine token for logging."""

    allow_agent: bool
    classification: IntentClassification | None
    reason: str


class IntentGate:
    """Policy wrapper around one MessageIntentClassifier."""

    def __init__(
        self,
        classifier: MessageIntentClassifier | None,
        confidence_threshold: float = 0.85,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be within [0, 1]")
        # classifier None = gate disabled (INTENT_CLASSIFIER_ENABLED=false):
        # every message passes; documented dev/test mode, never a failure.
        self._classifier = classifier
        self._threshold = confidence_threshold

    @property
    def enabled(self) -> bool:
        return self._classifier is not None

    def evaluate(
        self, message: str, context: Sequence[IntentContextMessage] = ()
    ) -> IntentDecision:
        """Classify and decide. Never raises."""
        if self._classifier is None:
            return IntentDecision(
                allow_agent=True, classification=None, reason=DISABLED_REASON
            )
        try:
            classification = self._classifier.classify(message, context)
        except IntentClassifierError as exc:
            return IntentDecision(
                allow_agent=False,
                classification=None,
                reason=f"classifier_error:{type(exc).__name__}",
            )
        return self._decide(classification)

    def _decide(self, classification: IntentClassification) -> IntentDecision:
        category: IntentCategory = classification.category
        if category != "order_related":
            return IntentDecision(
                allow_agent=False,
                classification=classification,
                reason=f"category:{category}",
            )
        if classification.confidence < self._threshold:
            return IntentDecision(
                allow_agent=False,
                classification=classification,
                reason=BELOW_THRESHOLD_REASON,
            )
        return IntentDecision(
            allow_agent=True, classification=classification, reason=ALLOW_REASON
        )
