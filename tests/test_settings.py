"""Settings production-safety validators (C2, M4).

Production is the DEFAULT environment: the fail-closed rules apply unless
development is set explicitly. Instantiated through model_validate with
_env_file=None so the developer's .env never leaks into these tests.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

PROD = {"app_environment": "production"}
DEV = {"app_environment": "development"}


def test_defaults_are_production_and_fail_closed() -> None:
    s = Settings(_env_file=None)
    assert s.app_environment == "production"


def test_production_rejects_disabled_intent_gate() -> None:
    """C2: INTENT_CLASSIFIER_ENABLED=false in production must refuse to
    start — the intent gate may never be bypassed there."""
    with pytest.raises(ValidationError, match="INTENT_CLASSIFIER_ENABLED"):
        Settings(_env_file=None, **PROD, intent_classifier_enabled=False)


def test_development_allows_explicit_disabled_gate() -> None:
    """The bypass exists only as an explicit development setting."""
    s = Settings(_env_file=None, **DEV, intent_classifier_enabled=False)
    assert s.intent_classifier_enabled is False


def test_production_requires_bridge_token_with_url() -> None:
    """M4: a bridge URL without a token in production would send
    unauthenticated bridge requests — refused at startup."""
    with pytest.raises(ValidationError, match="WHATSAPP_BRIDGE_TOKEN"):
        Settings(_env_file=None, **PROD, whatsapp_bridge_url="http://bridge:3000")


def test_production_accepts_bridge_with_token() -> None:
    s = Settings(
        _env_file=None,
        **PROD,
        whatsapp_bridge_url="http://bridge:3000",
        whatsapp_bridge_token="t",
    )
    assert s.whatsapp_bridge_token == "t"


def test_bridge_url_without_token_allowed_in_development() -> None:
    s = Settings(_env_file=None, **DEV, whatsapp_bridge_url="http://bridge:3000")
    assert s.whatsapp_bridge_token == ""
