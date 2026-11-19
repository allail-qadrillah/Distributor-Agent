from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.security import verify_operator_token
from app.messaging.exceptions import (
    ConversationNotFoundError,
    InvalidConversationStateError,
)
from app.messaging.locking import LockAcquisitionTimeoutError
from app.messaging.wiring import build_messaging_stack
from app.schemas.conversation import ConversationState

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(verify_operator_token)],
)


def _stack():
    # read-only routes build the lighter part of the stack (no agent
    # provider is needed); the same wiring with configured settings
    # raises 503-worthy errors, mapped here
    try:
        return build_messaging_stack()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"service_misconfigured: {type(exc).__name__}"
        ) from exc


class HandoffRequest(BaseModel):
    reason: str | None = None


@router.get("")
def list_conversations(
    state: ConversationState | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    conversations = _stack().conversations.list_conversations(state, limit, offset)
    return {
        "conversations": [c.model_dump(mode="json") for c in conversations],
        "limit": limit,
        "offset": offset,
    }


@router.get("/{conversation_id}")
def conversation_detail(conversation_id: UUID) -> dict:
    try:
        conversation = _stack().conversations.get_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    return conversation.model_dump(mode="json")


@router.get("/{conversation_id}/messages")
def conversation_messages(conversation_id: UUID) -> dict:
    try:
        messages = _stack().conversations.list_messages(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    return {"messages": [m.model_dump(mode="json") for m in messages]}


@router.post("/{conversation_id}/handoff")
def request_handoff(conversation_id: UUID, body: HandoffRequest | None = None) -> dict:
    reason = body.reason if body is not None else None
    try:
        conversation = _stack().handoff.request_handoff(conversation_id, reason)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except InvalidConversationStateError as exc:
        raise HTTPException(status_code=409, detail=_state_detail(exc)) from exc
    except LockAcquisitionTimeoutError as exc:
        # an agent run holds the phone lock; nothing was mutated — retry
        raise HTTPException(status_code=503, detail="conversation busy") from exc
    return conversation.model_dump(mode="json")


@router.post("/{conversation_id}/takeover")
def takeover(conversation_id: UUID) -> dict:
    try:
        conversation = _stack().handoff.takeover(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except InvalidConversationStateError as exc:
        raise HTTPException(status_code=409, detail=_state_detail(exc)) from exc
    except LockAcquisitionTimeoutError as exc:
        raise HTTPException(status_code=503, detail="conversation busy") from exc
    return conversation.model_dump(mode="json")


@router.post("/{conversation_id}/resume")
def resume(conversation_id: UUID) -> dict:
    try:
        conversation = _stack().handoff.resume(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except InvalidConversationStateError as exc:
        raise HTTPException(status_code=409, detail=_state_detail(exc)) from exc
    except LockAcquisitionTimeoutError as exc:
        raise HTTPException(status_code=503, detail="conversation busy") from exc
    return conversation.model_dump(mode="json")


def _state_detail(exc: InvalidConversationStateError) -> str:
    return f"conversation is {exc.current_state}, cannot go to {exc.target_state}"
