import hmac

from fastapi import Header, HTTPException

from app.config import settings


def _credential_matches_or_dev_bypass(presented: str | None, expected: str) -> bool:
    if not expected:
        return settings.app_environment == "development"
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def verify_webhook_secret(
    x_webhook_secret: str | None = Header(default=None),
) -> None:
    if not _credential_matches_or_dev_bypass(x_webhook_secret, settings.whatsapp_webhook_secret):
        raise HTTPException(status_code=401, detail="webhook unauthorized")


def verify_operator_token(
    x_operator_token: str | None = Header(default=None),
) -> None:
    if not _credential_matches_or_dev_bypass(x_operator_token, settings.operator_api_token):
        raise HTTPException(status_code=401, detail="operator unauthorized")
