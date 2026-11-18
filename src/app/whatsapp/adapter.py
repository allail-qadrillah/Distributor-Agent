"""WhatsAppAdapter: the outbound transport client for the Baileys bridge.

Contract with the bridge (external Node service, see
docs/notes/P3-whatsapp-adapter.md):
- POST {bridge_url}/messages, Authorization: Bearer <token>
- request: {"outbox_id": "<uuid>", "to": "<E.164>", "text": "..."}
  outbox_id is the reconciliation key: the bridge echoes it in delivery
  reports. WhatsApp itself has no idempotency key for the recipient.
- response 2xx: {"status": "sent" | "failed", "error": "..."}; the
  bridge answers "sent" ONLY after sock.sendMessage resolves (the
  message was accepted and encrypted for delivery). Device-level
  delivery arrives later as a delivery report webhook.

Outcome mapping (the core safety rule of the outbox):
- "sent" -> sent (adapter confirmed successful delivery of the request)
- "failed" -> failed (bridge rejected or WhatsApp refused; retryable)
- connection never established (ConnectError/ConnectTimeout) -> failed
- response lost after the request left (read/write/pool timeouts,
  connection reset, unparseable 2xx body, HTTP 5xx) -> UNKNOWN: the
  message may have been delivered; it is never auto-resent, only
  reconciled.

HTTP status split (bridge contract, docs/notes/P3-whatsapp-adapter.md):
- 4xx: the bridge refused the REQUEST itself (auth, validation) — the
  message was never handed to WhatsApp; confirmed-not-sent, retryable.
- 5xx: the bridge failed while processing — the request may already have
  reached WhatsApp; outcome unknown, never auto-resent. Only an explicit
  bridge guarantee "not sent before 5xx" would justify mapping 5xx to
  failed; the contract carries no such guarantee.

Transport only: this class knows HTTP and the bridge wire format,
nothing about conversations, intents, approvals, or business rules.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import httpx

_MESSAGES_PATH = "/messages"

SendStatus = Literal["sent", "failed", "unknown"]


@dataclass(frozen=True)
class WhatsAppSendOutcome:
    status: SendStatus
    error: str | None = None


class WhatsAppAdapter:
    """Sends one outbound message per call. Constructed only when the
    bridge URL is configured (empty URL = no outbound transport; the
    messaging layer then leaves replies pending)."""

    def __init__(
        self,
        bridge_url: str,
        token: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not bridge_url:
            raise ValueError("bridge_url must be configured")
        self._bridge_url = bridge_url.rstrip("/")
        self._token = token
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(10.0, timeout_seconds)
        )
        self._transport = transport  # test seam: httpx.MockTransport

    def send(self, outbox_id: UUID, to: str, body: str) -> WhatsAppSendOutcome:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        payload = {"outbox_id": str(outbox_id), "to": to, "text": body}
        try:
            with httpx.Client(
                base_url=self._bridge_url,
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = client.post(_MESSAGES_PATH, json=payload)
        except httpx.ConnectError as exc:
            return WhatsAppSendOutcome(
                status="failed", error=f"bridge unreachable: {exc}"
            )
        except httpx.ConnectTimeout as exc:
            return WhatsAppSendOutcome(
                status="failed", error=f"bridge connect timeout: {exc}"
            )
        except httpx.TimeoutException as exc:
            # request may already be in flight: outcome unknown
            return WhatsAppSendOutcome(status="unknown", error=f"bridge timeout: {exc}")
        except httpx.TransportError as exc:
            # response lost mid-flight: outcome unknown
            return WhatsAppSendOutcome(
                status="unknown", error=f"bridge transport error: {exc}"
            )
        if 400 <= response.status_code < 500:
            # the bridge refused the request itself: never handed to
            # WhatsApp, confirmed-not-sent, safe to retry
            return WhatsAppSendOutcome(
                status="failed", error=f"bridge HTTP {response.status_code}"
            )
        if response.status_code >= 500:
            # the bridge failed while processing: the request may already
            # have reached WhatsApp; uncertain, never auto-resent
            return WhatsAppSendOutcome(
                status="unknown", error=f"bridge HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError:
            return WhatsAppSendOutcome(
                status="unknown", error="bridge returned a non-JSON body"
            )
        if not isinstance(result, dict) or result.get("status") not in (
            "sent",
            "failed",
        ):
            return WhatsAppSendOutcome(
                status="unknown", error="bridge returned an unusable body"
            )
        error = result.get("error")
        return WhatsAppSendOutcome(
            status=result["status"],
            error=str(error) if isinstance(error, str) else None,
        )
