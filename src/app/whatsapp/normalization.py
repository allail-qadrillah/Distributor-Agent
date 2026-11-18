"""Inbound event normalization: raw Baileys messages.upsert -> strict
WhatsAppInboundMessage list.

Verified Baileys contract (WhiskeySockets/Baileys, checked 2026-11-18):
- event payload {"messages": WAMessage[], "type": "notify" | "append"}:
  "notify" = live delivery (process), "append" = history backfill
  (skip: old messages must never be replayed into the agent).
- WAMessage.key: {remoteJid, id, fromMe, participant?}; 1:1 chats use
  "<digits>@s.whatsapp.net", groups "@g.us", broadcasts
  "status@broadcast", newsletters "@newsletter".
- text content: message.conversation or
  message.extendedTextMessage.text; other content types carry no usable
  text for this milestone.

This module is a pure function over dicts: tolerant of extra protocol
fields, strict about what it accepts (E.164 phone after JID
conversion, non-blank text, non-blank external id). Skipped messages
carry a reason so the webhook can answer the bridge precisely.
"""

from dataclasses import dataclass

from app.whatsapp.schemas import WhatsAppInboundMessage

_SKIP_HISTORY = "history_backfill"
_SKIP_FROM_ME = "own_outgoing_echo"
_SKIP_GROUP = "group_chat"
_SKIP_BROADCAST = "status_broadcast"
_SKIP_NEWSLETTER = "newsletter"
_SKIP_LID = "lid_jid_no_phone"
_SKIP_NO_TEXT = "no_text_content"
_SKIP_INVALID = "invalid_event_shape"


@dataclass(frozen=True)
class NormalizedInbound:
    """One event entry: either a usable customer message or a skip
    reason. The webhook reports both; only usable messages are
    processed."""

    message: WhatsAppInboundMessage | None
    skip_reason: str | None


def normalize_inbound_event(raw: object) -> list[NormalizedInbound]:
    """Normalize one raw messages.upsert payload. Never raises on event
    shape: a malformed event is reported as a skip, not an error."""
    if not isinstance(raw, dict):
        return [NormalizedInbound(message=None, skip_reason=_SKIP_INVALID)]
    event_type = raw.get("type")
    messages = raw.get("messages")
    if event_type != "notify" or not isinstance(messages, list):
        # append = history sync: stored nowhere, replayed nowhere
        return [NormalizedInbound(message=None, skip_reason=_SKIP_HISTORY)]
    return [_normalize_message(entry) for entry in messages]


def _normalize_message(entry: object) -> NormalizedInbound:
    if not isinstance(entry, dict):
        return NormalizedInbound(message=None, skip_reason=_SKIP_INVALID)
    key = entry.get("key")
    if not isinstance(key, dict):
        return NormalizedInbound(message=None, skip_reason=_SKIP_INVALID)
    if key.get("fromMe") is True:
        return NormalizedInbound(message=None, skip_reason=_SKIP_FROM_ME)
    remote_jid = key.get("remoteJid")
    external_id = key.get("id")
    if (
        not isinstance(remote_jid, str)
        or not isinstance(external_id, str)
        or not external_id.strip()
    ):
        return NormalizedInbound(message=None, skip_reason=_SKIP_INVALID)
    if remote_jid.endswith("@g.us"):
        return NormalizedInbound(message=None, skip_reason=_SKIP_GROUP)
    if remote_jid == "status@broadcast":
        return NormalizedInbound(message=None, skip_reason=_SKIP_BROADCAST)
    if remote_jid.endswith("@newsletter"):
        return NormalizedInbound(message=None, skip_reason=_SKIP_NEWSLETTER)
    if not remote_jid.endswith("@s.whatsapp.net"):
        # LID-style or unknown jids cannot be mapped to an E.164 phone
        return NormalizedInbound(message=None, skip_reason=_SKIP_LID)
    phone = f"+{remote_jid.split('@', 1)[0]}"
    if not _is_e164(phone):
        return NormalizedInbound(message=None, skip_reason=_SKIP_INVALID)

    text = _extract_text(entry.get("message"))
    if text is None:
        return NormalizedInbound(message=None, skip_reason=_SKIP_NO_TEXT)
    timestamp = entry.get("messageTimestamp")
    received_at = None
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        from datetime import UTC, datetime

        received_at = datetime.fromtimestamp(timestamp, tz=UTC)
    return NormalizedInbound(
        message=WhatsAppInboundMessage(
            external_message_id=external_id,
            phone=phone,
            text=text,
            received_at=received_at,
        ),
        skip_reason=None,
    )


def _extract_text(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    conversation = message.get("conversation")
    if isinstance(conversation, str) and conversation.strip():
        return conversation.strip()
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        text = extended.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _is_e164(phone: str) -> bool:
    digits = phone[1:]
    return (
        phone.startswith("+")
        and len(digits) <= 15
        and digits.isdigit()
        and digits[0] not in "0"
    )
