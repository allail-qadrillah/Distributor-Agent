"""API authentication boundaries (shared-secret, constant-time).

Phase 3 has no operator identity system (no users, roles, or audit
trail). Until one exists, these dependencies are the minimal EXPLICIT
boundary the review required:

- verify_webhook_secret: the WhatsApp bridge authenticates with a shared
  secret in the X-Webhook-Secret header (both webhook routes).
- verify_operator_token: operator tooling authenticates with a shared
  token in the X-Operator-Token header (conversation and outbox routes).

Rules (fail closed):
- Secret configured → every request must present a matching value,
  compared with hmac.compare_digest (constant time, no early exit).
- Secret NOT configured:
  - production: every request is rejected (401). An internal network is
    never accepted as authentication.
  - development: requests are allowed. This is the documented development
    bypass, not a default — the default environment is production.

Known limitations (accepted, documented): a shared secret authenticates
the caller, not individual requests: no replay protection, no
per-operator identity, no rotation protocol. Upgrade path: per-bridge
mTLS or HMAC over the raw body with a timestamp, and real operator
accounts. No secret or presented value is ever logged or returned.
"""

import hmac

from fastapi import Header, HTTPException

from app.config import settings


def _authorized(presented: str | None, expected: str) -> bool:
    if not expected:
        return settings.app_environment == "development"
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def verify_webhook_secret(
    x_webhook_secret: str | None = Header(default=None),
) -> None:
    if not _authorized(x_webhook_secret, settings.whatsapp_webhook_secret):
        raise HTTPException(status_code=401, detail="webhook unauthorized")


def verify_operator_token(
    x_operator_token: str | None = Header(default=None),
) -> None:
    if not _authorized(x_operator_token, settings.operator_api_token):
        raise HTTPException(status_code=401, detail="operator unauthorized")
