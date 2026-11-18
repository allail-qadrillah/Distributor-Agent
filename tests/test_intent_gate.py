"""IntentGate policy table, the agreed classification policy, fail closed."""

from app.intent.exceptions import IntentClassifierTimeoutError
from app.intent.gate import IntentGate
from app.intent.schemas import IntentClassification

THRESHOLD = 0.85


def _gate_with(
    classification: IntentClassification | None, error: Exception | None = None
) -> IntentGate:
    class Stub:
        def classify(self, message, context=()):
            if error is not None:
                raise error
            assert classification is not None
            return classification

    return IntentGate(classifier=Stub(), confidence_threshold=THRESHOLD)  # type: ignore[arg-type]


def test_order_related_above_threshold_allows_agent() -> None:
    gate = _gate_with(
        IntentClassification(category="order_related", confidence=0.9, reason="stok")
    )
    assert gate.evaluate("Stok masih ada?").allow_agent is True


def test_order_related_at_threshold_allows_agent() -> None:
    gate = _gate_with(
        IntentClassification(
            category="order_related", confidence=THRESHOLD, reason="stok"
        )
    )
    assert gate.evaluate("Stok masih ada?").allow_agent is True


def test_order_related_below_threshold_hands_off() -> None:
    gate = _gate_with(
        IntentClassification(category="order_related", confidence=0.5, reason="mungkin")
    )
    decision = gate.evaluate("ambil 5")
    assert decision.allow_agent is False
    assert decision.reason == "order_related_below_threshold"


def test_order_related_zero_confidence_hands_off() -> None:
    gate = _gate_with(
        IntentClassification(category="order_related", confidence=0.0, reason="tebak")
    )
    assert gate.evaluate("ya").allow_agent is False


def test_human_request_hands_off() -> None:
    gate = _gate_with(
        IntentClassification(
            category="human_request", confidence=0.99, reason="minta admin"
        )
    )
    decision = gate.evaluate("Saya mau bicara sama admin")
    assert decision.allow_agent is False
    assert decision.reason == "category:human_request"


def test_out_of_scope_hands_off() -> None:
    gate = _gate_with(
        IntentClassification(category="out_of_scope", confidence=0.99, reason="gudang")
    )
    assert gate.evaluate("Gudang buka ga hari ini?").allow_agent is False


def test_ambiguous_hands_off() -> None:
    gate = _gate_with(
        IntentClassification(category="ambiguous", confidence=0.99, reason="rancu")
    )
    assert gate.evaluate("Boleh tanya sesuatu?").allow_agent is False


def test_timeout_fails_closed() -> None:
    gate = _gate_with(None, error=IntentClassifierTimeoutError("timeout"))
    decision = gate.evaluate("pesan")
    assert decision.allow_agent is False
    assert decision.reason == "classifier_error:IntentClassifierTimeoutError"


def test_unexpected_exception_propagates_as_a_bug() -> None:
    # the gate catches IntentClassifierError (the classifier contract).
    # A raw exception is a programming bug: it propagates and the message
    # processor fails before any agent run, so the system still fails
    # closed (no agent invocation happens without a gate decision).
    import pytest

    gate = _gate_with(None, error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        gate.evaluate("pesan")


def test_disabled_gate_allows_everything() -> None:
    gate = IntentGate(classifier=None, confidence_threshold=THRESHOLD)
    decision = gate.evaluate("pesan apa pun")
    assert decision.allow_agent is True
    assert decision.reason == "intent_gate_disabled"
    assert gate.enabled is False


def test_threshold_bounds_validated() -> None:
    import pytest

    with pytest.raises(ValueError):
        IntentGate(classifier=None, confidence_threshold=1.5)
    with pytest.raises(ValueError):
        IntentGate(classifier=None, confidence_threshold=-0.1)
