"""Intent classifier evaluation against the regression dataset (real SLM).

Usage:
    uv run python scripts/evaluate_intent.py [--dataset PATH]

Separate from the test suite on purpose: it calls the configured
INTENT_CLASSIFIER_* provider (needs .env credentials) and measures
classification quality; the offline policy tests live in
tests/test_intent_dataset.py.

Never prints secrets: model name, base URL, threshold, and metrics only.

Report:
- overall and per-category accuracy;
- per-category false positives / false negatives;
- ambiguous predictions (model returned ambiguous);
- DANGEROUS LEAKS: cases the model called order_related but the dataset
  expects a handoff category — those would reach the main agent;
- classifier errors (each is a fail-closed handoff in production);
- dataset version (sha256, first 12 hex chars) and timestamp.

Exit code 0 when the evaluation ran and accuracy >= MIN_ACCURACY,
1 otherwise (also on configuration problems).
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.intent.classifier import MessageIntentClassifier
from app.intent.exceptions import IntentClassifierError
from app.intent.schemas import INTENT_CATEGORIES, IntentCase, IntentContextMessage

DEFAULT_DATASET = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "intent_cases.jsonl"
)
MIN_ACCURACY = 0.9


def load_dataset(path: Path) -> list[IntentCase]:
    cases = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(IntentCase.model_validate(json.loads(line)))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()

    if not settings.intent_classifier_enabled:
        print("FAIL: INTENT_CLASSIFIER_ENABLED is false; nothing to evaluate.")
        return 1
    try:
        classifier = MessageIntentClassifier(
            api_key=settings.intent_classifier_api_key,
            model=settings.intent_classifier_model,
            base_url=settings.intent_classifier_base_url,
            timeout_seconds=settings.intent_classifier_timeout_seconds,
        )
    except IntentClassifierError as exc:
        print(f"FAIL: classifier misconfigured: {exc}")
        return 1

    cases = load_dataset(args.dataset)
    if not cases:
        print(f"FAIL: dataset {args.dataset} is empty or missing.")
        return 1

    correct = 0
    errors = 0
    predictions: Counter = Counter()
    category_total: Counter = Counter(c.expected for c in cases)
    category_correct: Counter = Counter()
    fp: Counter = Counter()  # predicted category, truth was another
    fn: Counter = Counter()  # truth category missed
    leaks: list[str] = []
    mismatches: list[str] = []

    for case in cases:
        context = [
            IntentContextMessage(role=m.role, content=m.content)
            for m in (case.context or [])
        ]
        try:
            got = classifier.classify(case.message, context).category
        except IntentClassifierError:
            errors += 1
            fn[case.expected] += 1
            mismatches.append(f"{case.id}: ERROR (expected {case.expected})")
            continue
        predictions[got] += 1
        if got == case.expected:
            correct += 1
            category_correct[case.expected] += 1
            continue
        fp[got] += 1
        fn[case.expected] += 1
        mismatches.append(f"{case.id}: expected {case.expected}, got {got}")
        if got == "order_related" and case.expected != "order_related":
            leaks.append(case.id)

    total = len(cases)
    accuracy = correct / total
    dataset_sha = hashlib.sha256(args.dataset.read_bytes()).hexdigest()[:12]

    print("INTENT CLASSIFIER EVALUATION")
    print(f"  model:      {settings.intent_classifier_model}")
    print(f"  base_url:   {settings.intent_classifier_base_url}")
    print(f"  threshold:  {settings.intent_classifier_confidence_threshold}")
    print(f"  dataset:    {args.dataset} ({total} cases, sha256 {dataset_sha})")
    print(f"  timestamp:  {datetime.now(tz=UTC).isoformat(timespec='seconds')}")
    print(f"  accuracy:   {correct}/{total} = {accuracy:.1%}")
    print("  per-category accuracy:")
    for category in INTENT_CATEGORIES:
        hits = category_correct[category]
        total_for = category_total[category]
        print(f"    {category}: {hits}/{total_for}")
    print("  predictions:", dict(predictions))
    print(f"  errors (fail-closed handoffs): {errors}")
    if fp:
        print(f"  false positives by predicted category: {dict(fp)}")
    if fn:
        print(f"  false negatives by expected category: {dict(fn)}")
    if leaks:
        print(f"  DANGEROUS LEAKS (handoff case sent to the agent): {leaks}")
    else:
        print("  dangerous leaks: none")
    for line in mismatches:
        print(f"    mismatch {line}")

    if accuracy < MIN_ACCURACY:
        print(f"FAIL: accuracy {accuracy:.1%} below required {MIN_ACCURACY:.0%}.")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
