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
