"""Synthetic pixels traverse actual MCP SDK HTTP serialization as an image."""

import base64
import binascii
import hashlib
import struct
import zlib

from django.utils import timezone
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from codito_relay.core import mcp_server
from codito_relay.core.dispatch import DispatchReceipt

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAADElEQVQImWNgYGAAAAAEAAGjChXjAAAAAElFTkSuQmCC"
)


def make_png(width: int, height: int) -> str:
    """Create a deterministic RGB PNG using only the standard library."""

    def chunk(name: bytes, data: bytes) -> bytes:
        payload = name + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", binascii.crc32(payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\x00" + (b"\x00" * width * 3) for _ in range(height))
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    raw += chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    return base64.b64encode(raw).decode()


def assert_selected_screen_is_imagecontent_in_actual_sdk_response(
    client, oauth_token, link, monkeypatch
):
    # Mock dispatch only; use the authenticated gateway and actual SDK serialization.
    # No device action, local permission or capture is performed by this test.
    selected = "screen_" + "a" * 64

    async def dispatch(principal, name, arguments):
        assert name == "device_screenshot"
        assert arguments["display"] == selected and arguments["action"] == "capture"
        return DispatchReceipt(
            "synthetic_capture_123456789",
            {
                "ok": True,
                "text": "Synthetic image, no desktop was captured",
                "result": {
                    "display": selected,
                    "width": 1,
                    "height": 1,
                    "captured_at": timezone.now().isoformat(),
                    "sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
                    "image_base64": PNG,
                },
            },
        )

    raw, _ = oauth_token
    with monkeypatch.context() as patch:
        patch.setattr(mcp_server, "dispatch_tool", dispatch)
        response = client.post(
            f"/mcp/d/{link.link_id}",
            headers={
                "Authorization": f"Bearer {raw}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION,
            },
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "device_screenshot",
                    "arguments": {"purpose": "Synthetic image test", "display": selected},
                },
            },
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    image = result["content"][1]
    assert image["type"] == "image" and image["data"] == PNG and image["mimeType"] == "image/png"
    assert result["structuredContent"]["result"]["display"] == selected
    assert "image_base64" not in result["structuredContent"]["result"]
    assert "mcp/www_authenticate" not in str(result.get("_meta", {}))
