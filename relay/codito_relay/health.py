from __future__ import annotations

import asyncio
import secrets

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import from_url
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .core.models import Device


async def live(request: Request) -> JSONResponse:
    return JSONResponse({"status": "live"})


def _database_ready() -> None:
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
    if Device._meta.db_table not in tables:
        raise RuntimeError("Codito core migrations have not created the device table")


async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        await asyncio.wait_for(sync_to_async(_database_ready, thread_sensitive=True)(), timeout=2)
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    if settings.HEALTH_READY_CHECK_REDIS:
        redis = from_url(settings.REDIS_URL)
        try:
            await asyncio.wait_for(redis.ping(), timeout=2)
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"
        finally:
            await redis.close()
    checks["oidc_signing_key"] = (
        "ok" if settings.OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] else "unavailable"
    )
    status = 200 if all(value == "ok" for value in checks.values()) else 503
    return JSONResponse(
        {"status": "ready" if status == 200 else "not_ready", "checks": checks}, status_code=status
    )


async def metrics(request: Request) -> Response:
    expected = settings.METRICS_BEARER_TOKEN
    if expected:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(expected, supplied):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
