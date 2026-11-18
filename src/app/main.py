from fastapi import FastAPI

from app.api.conversations import router as conversations_router
from app.api.outbox import router as outbox_router
from app.api.whatsapp import router as whatsapp_router


def create_app() -> FastAPI:
    app = FastAPI(title="Distributor Operations Agent")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(whatsapp_router)
    app.include_router(conversations_router)
    app.include_router(outbox_router)

    return app


app = create_app()
