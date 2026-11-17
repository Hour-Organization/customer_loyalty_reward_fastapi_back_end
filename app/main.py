"""The FastAPI application: every route under /api, uploaded files under /storage, and Laravel-shaped errors."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.serial import base_url
from app.validation import ValidationError

log = logging.getLogger("app")


async def _expire_overdue_forever(every: int) -> None:
    """Refund redemptions whose reservation ran out, like the Laravel scheduler's redemptions:expire, and
    cancel orders whose KHQR was left unpaid, and catch KHQR payments that arrived after their order closed."""
    from app.database import SessionLocal
    from app.services import payments, redemptions

    def run_once(expire) -> int:
        with SessionLocal() as db:
            expired = expire(db)
            db.commit()
            return expired

    while True:
        await asyncio.sleep(every)
        jobs = (
            (redemptions.expire_overdue, "reservations expired"),
            (payments.expire_overdue, "KHQR payments expired"),
            (payments.catch_late, "late KHQR payments found (refund due)"),
        )
        for expire, what in jobs:
            try:
                expired = await asyncio.to_thread(run_once, expire)
                if expired:
                    log.info("%s %s.", expired, what)
            except Exception:  # noqa: BLE001
                log.exception("Background job failed: %s", what)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    every = get_settings().expire_redemptions_every
    task = asyncio.create_task(_expire_overdue_forever(every)) if every > 0 else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    from app.routers import (
        account,
        auth,
        catalog,
        loyalty,
        operations,
        points,
        staff,
        stock,
        storefront,
    )

    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="Customer-loyalty e-commerce API: catalog, orders, points, tiers, rewards and reward stock.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
    )

    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def remember_base_url(request: Request, call_next):
        token = base_url.set(str(request.base_url).rstrip("/"))
        try:
            return await call_next(request)
        finally:
            base_url.reset(token)

    @app.exception_handler(ValidationError)
    async def validation_failed(_request: Request, exception: ValidationError):
        return JSONResponse({"message": exception.message, "errors": exception.errors}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def request_invalid(_request: Request, exception: RequestValidationError):
        errors: dict[str, list[str]] = {}
        for error in exception.errors():
            field = ".".join(str(part) for part in error["loc"][1:]) or "request"
            errors.setdefault(field, []).append(error["msg"])
        return JSONResponse({"message": next(iter(errors.values()))[0], "errors": errors}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exception: StarletteHTTPException):
        message = exception.detail
        if exception.status_code == 404 and message == "Not Found":
            message = f"The route {request.url.path.lstrip('/')} could not be found."
        if exception.status_code == 405 and message == "Method Not Allowed":
            message = f"The {request.method} method is not supported for route {request.url.path.lstrip('/')}."
        return JSONResponse({"message": message}, status_code=exception.status_code, headers=exception.headers)

    api = "/api"
    for module in (auth, storefront, account, catalog, loyalty, staff, points, operations, stock):
        for router in module.routers:
            app.include_router(router, prefix=api)

    @app.get("/up", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "up"}

    storage_dir = settings.storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/storage", StaticFiles(directory=storage_dir), name="storage")
    return app


app = create_app()

__all__ = ["app", "create_app", "HTTPException"]
