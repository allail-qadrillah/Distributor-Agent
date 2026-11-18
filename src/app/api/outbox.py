from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.security import verify_operator_token
from app.messaging.wiring import build_messaging_stack

router = APIRouter(
    prefix="/outbox",
    tags=["outbox"],
    dependencies=[Depends(verify_operator_token)],
)


@router.post("/dispatch")
def dispatch(limit: int = Query(default=10, ge=1, le=100)) -> dict:
    try:
        stack = build_messaging_stack()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"service_misconfigured: {type(exc).__name__}"
        ) from exc
    reports = stack.outbox.dispatch_pending(limit=limit)
    # M1: any approval that just became active may end a deferral window
    for report in reports:
        if report.activated and report.conversation_id is not None:
            stack.processor.reprocess_deferred(report.conversation_id)
    return {
        "dispatched": len(reports),
        "results": [
            {"outbox_id": str(r.outbox_id), "status": r.status} for r in reports
        ],
    }
