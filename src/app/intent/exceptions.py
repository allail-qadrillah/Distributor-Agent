class IntentClassifierError(Exception):
    pass


class IntentClassifierConfigurationError(IntentClassifierError):
    pass


class IntentClassifierTimeoutError(IntentClassifierError):
    pass


class IntentClassifierProviderError(IntentClassifierError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class IntentClassifierInvalidOutputError(IntentClassifierError):
    pass
