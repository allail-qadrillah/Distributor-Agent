"""Intent classifier domain exceptions.

Same convention as app.agents.exceptions / app.services.exceptions: the
boundary raises domain errors, never raw pydantic or httpx exceptions.
The gate catches IntentClassifierError and fails closed (human handoff),
so an exception here is a classification outcome, not a crash.
"""


class IntentClassifierError(Exception):
    """Base for all intent classifier failures."""


class IntentClassifierConfigurationError(IntentClassifierError):
    """Missing or invalid classifier configuration (api key, model, base URL)."""


class IntentClassifierTimeoutError(IntentClassifierError):
    """The classifier provider did not answer within the configured timeout."""


class IntentClassifierProviderError(IntentClassifierError):
    """Classifier provider communication failure (transport, HTTP, error body)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class IntentClassifierInvalidOutputError(IntentClassifierError):
    """The classifier output could not be parsed or failed validation
    (non-JSON, invalid category, confidence out of range, missing fields)."""
