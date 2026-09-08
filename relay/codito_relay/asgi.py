from __future__ import annotations

import os
from typing import cast

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "codito_relay.settings")

from django.core.asgi import get_asgi_application  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount, Route, WebSocketRoute  # noqa: E402
from starlette.types import ASGIApp  # noqa: E402

from codito_relay.core.mcp_server import DeviceMCPGateway, mcp_http_app, mcp_lifespan  # noqa: E402
from codito_relay.core.websocket import device_websocket  # noqa: E402
from codito_relay.health import live, metrics, ready  # noqa: E402

django_application = get_asgi_application()

application = Starlette(
    routes=[
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        WebSocketRoute("/ws/device", device_websocket),
        Mount("/mcp/d", app=cast(ASGIApp, DeviceMCPGateway(mcp_http_app))),
        Mount("/", app=cast(ASGIApp, django_application)),
    ],
    lifespan=mcp_lifespan,
)
