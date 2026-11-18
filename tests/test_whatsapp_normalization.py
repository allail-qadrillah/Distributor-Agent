"""Baileys event normalization tests: pure function over raw
messages.upsert payloads, verified against the bridge contract in
docs/notes/P3-whatsapp-adapter.md.

Every skip path must carry a reason (the webhook answers the bridge
precisely); every accepted path must yield a strict E.164 phone and
non-blank text/id. Extra protocol fields are tolerated.
"""

from datetime import UTC, datetime

from app.whatsapp.normalization import normalize_inbound_event


def _event(entry: dict, event_type: str = "notify") -> dict:
    return {"type": event_type, "messages": [entry]}


def _entry(jid: str, text: dict, external_id: str = "3A") -> dict:
    return {
        "key": {"remoteJid": jid, "id": external_id, "fromMe": False},
        "message": text,
    }


def test_conversation_text_accepted() -> None:
    result = normalize_inbound_event(
        _event(_entry("6281234567890@s.whatsapp.net", {"conversation": " stok Aqua? "}))
    )
    assert len(result) == 1
    message = result[0].message
    assert message is not None
    assert message.external_message_id == "3A"
    assert message.phone == "+6281234567890"
    assert message.text == "stok Aqua?"
    assert result[0].skip_reason is None


def test_extended_text_accepted() -> None:
    entry = _entry(
        "6281234567890@s.whatsapp.net",
        {
            "extendedTextMessage": {
                "text": "berapa harga?",
                "contextInfo": {"quotedStanzaId": "x"},
            }
        },
    )
    message = normalize_inbound_event(_event(entry))[0].message
    assert message is not None
    assert message.text == "berapa harga?"


def test_timestamp_mapped_to_utc() -> None:
    entry = _entry("6281234567890@s.whatsapp.net", {"conversation": "halo"})
    entry["messageTimestamp"] = 1765843200
    message = normalize_inbound_event(_event(entry))[0].message
    assert message is not None
    assert message.received_at == datetime.fromtimestamp(1765843200, tz=UTC)


def test_history_backfill_skipped() -> None:
    result = normalize_inbound_event(
        _event(
            _entry("6281234567890@s.whatsapp.net", {"conversation": "lama"}), "append"
        )
    )
    assert result[0].message is None
    assert result[0].skip_reason == "history_backfill"


def test_own_outgoing_echo_skipped() -> None:
    entry = _entry("6281234567890@s.whatsapp.net", {"conversation": "jawaban saya"})
    entry["key"]["fromMe"] = True
    result = normalize_inbound_event(_event(entry))
    assert result[0].message is None
    assert result[0].skip_reason == "own_outgoing_echo"


def test_group_chat_skipped() -> None:
    result = normalize_inbound_event(
        _event(_entry("1203630@g.us", {"conversation": "grup"}))
    )
    assert result[0].skip_reason == "group_chat"


def test_status_broadcast_skipped() -> None:
    result = normalize_inbound_event(
        _event(_entry("status@broadcast", {"conversation": "status"}))
    )
    assert result[0].skip_reason == "status_broadcast"


def test_newsletter_skipped() -> None:
    result = normalize_inbound_event(
        _event(_entry("123@newsletter", {"conversation": "news"}))
    )
    assert result[0].skip_reason == "newsletter"


def test_lid_jid_cannot_map_to_phone() -> None:
    result = normalize_inbound_event(
        _event(_entry("1234567890@lid", {"conversation": "halo"}))
    )
    assert result[0].skip_reason == "lid_jid_no_phone"


def test_non_text_content_skipped() -> None:
    result = normalize_inbound_event(
        _event(_entry("6281234567890@s.whatsapp.net", {"imageMessage": {"url": "x"}}))
    )
    assert result[0].skip_reason == "no_text_content"


def test_blank_text_skipped() -> None:
    result = normalize_inbound_event(
        _event(_entry("6281234567890@s.whatsapp.net", {"conversation": "   "})),
    )
    assert result[0].skip_reason == "no_text_content"


def test_invalid_phone_rejected() -> None:
    leading_zero = normalize_inbound_event(
        _event(_entry("081234567890@s.whatsapp.net", {"conversation": "halo"}))
    )
    assert leading_zero[0].skip_reason == "invalid_event_shape"
    too_long = normalize_inbound_event(
        _event(_entry("6281234567890123456@s.whatsapp.net", {"conversation": "halo"}))
    )
    assert too_long[0].skip_reason == "invalid_event_shape"


def test_blank_external_id_rejected() -> None:
    result = normalize_inbound_event(
        _event(
            _entry(
                "6281234567890@s.whatsapp.net",
                {"conversation": "halo"},
                external_id="  ",
            )
        )
    )
    assert result[0].skip_reason == "invalid_event_shape"


def test_malformed_shapes_reported_not_raised() -> None:
    assert normalize_inbound_event("not a dict")[0].skip_reason == "invalid_event_shape"
    entry = _entry("6281234567890@s.whatsapp.net", {"conversation": "halo"})
    del entry["key"]
    assert (
        normalize_inbound_event(_event(entry))[0].skip_reason == "invalid_event_shape"
    )
    keyless = {"type": "notify", "messages": [42]}
    assert normalize_inbound_event(keyless)[0].skip_reason == "invalid_event_shape"


def test_empty_and_multi_message_events() -> None:
    empty = normalize_inbound_event({"type": "notify", "messages": []})
    assert empty == []
    multi = normalize_inbound_event(
        {
            "type": "notify",
            "messages": [
                _entry("6281234567890@s.whatsapp.net", {"conversation": "satu"}, "3A"),
                _entry("6281234567890@s.whatsapp.net", {"conversation": "dua"}, "3B"),
            ],
        }
    )
    assert [m.message.external_message_id for m in multi] == ["3A", "3B"]
