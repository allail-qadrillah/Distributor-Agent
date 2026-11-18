"""Intent regression dataset tests: the dataset is valid, complete, and
consistent with the gate policy.

The dataset (tests/fixtures/intent_cases.jsonl) drives both these
offline tests and scripts/evaluate_intent.py against the real SLM.
Every case expected to be a handoff category is proven (offline, with a
stub classifier echoing the dataset) to stop at the gate: the main
agent is never invoked for them.
"""

import json
from pathlib import Path

import pytest

from app.intent.gate import IntentGate
from app.intent.schemas import INTENT_CATEGORIES, IntentCase, IntentContextMessage
from tests.messaging_fakes import StubClassifier

DATASET = Path(__file__).parent / "fixtures" / "intent_cases.jsonl"
THRESHOLD = 0.85

REQUIRED_TAGS = (
    "informal",
    "typo",
    "abbreviation",
    "mixed_language",
    "short",
    "warehouse_word",
    "human_during_order",
    "prompt_injection",
    "context_dependent",
)


def _load() -> list[IntentCase]:
    cases = []
    with DATASET.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(IntentCase.model_validate(json.loads(line)))
    return cases


CASES = _load()


def test_dataset_loads_and_each_line_validates() -> None:
    assert len(CASES) >= 20


def test_ids_are_unique() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_category_has_at_least_two_cases() -> None:
    for category in INTENT_CATEGORIES:
        matching = [c for c in CASES if c.expected == category]
        assert len(matching) >= 2, f"category {category} underrepresented"


def test_required_tags_present() -> None:
    tags = {t for case in CASES for t in (case.tags or [])}
    for required in REQUIRED_TAGS:
        assert required in tags, f"no case tagged {required}"


def test_context_cases_use_only_user_and_assistant() -> None:
    for case in CASES:
        if case.context:
            assert {m.role for m in case.context} <= {"user", "assistant"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_policy_replay_matches_dataset(case: IntentCase) -> None:
    """Replay each case through the gate with a classifier echoing the
    dataset label at high confidence: order_related passes, every other
    category is a handoff (never the agent)."""
    from app.intent.schemas import IntentClassification

    classification = IntentClassification(
        category=case.expected, confidence=0.99, reason="dataset replay"
    )
    classifier = StubClassifier(classification)
    gate = IntentGate(classifier, THRESHOLD)
    context = [
        IntentContextMessage(role=m.role, content=m.content)
        for m in (case.context or [])
    ]
    decision = gate.evaluate(case.message, context)
    if case.expected == "order_related":
        assert decision.allow_agent is True
    else:
        assert decision.allow_agent is False
        assert decision.reason != "order_related"
    # the classifier saw exactly the dataset message and context
    assert classifier.asked[0][0] == case.message
    assert len(classifier.asked[0][1]) == len(context)


def test_low_confidence_order_related_is_handoff() -> None:
    from app.intent.schemas import IntentClassification

    classifier = StubClassifier(
        IntentClassification(category="order_related", confidence=0.5, reason="unsure")
    )
    assert IntentGate(classifier, THRESHOLD).evaluate("pesan").allow_agent is False
