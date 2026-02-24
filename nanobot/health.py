"""Lightweight HTTP health check server (no external dependencies).

Exposes GET /health returning JSON with:
- agent_loop running status
- channel connection states
- queue depths
- last_processed_at timestamp
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import TYPE_CHECKING

from nanobot.logging import get_logger

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager

logger = get_logger(__name__)

_HTTP_200 = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"
_HTTP_404 = b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nNot Found"
_HTTP_405 = b"HTTP/1.1 405 Method Not Allowed\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nMethod Not Allowed"


class HealthServer:
    """Minimal asyncio HTTP server exposing /health."""

    def __init__(
        self,
        agent: AgentLoop,
        bus: MessageBus,
        channels: ChannelManager,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.agent = agent
        self.bus = bus
        self.channels = channels
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self.last_processed_at: str | None = None

    def record_processed(self) -> None:
        """Call after each successfully processed message to update timestamp."""
        self.last_processed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _build_payload(self) -> bytes:
        channel_status = self.channels.get_status()
        payload = {
            "status": "ok",
            "agent_loop": {"running": self.agent._running},
            "channels": channel_status,
            "queue": {
                "inbound_depth": self.bus.inbound_size,
                "outbound_depth": self.bus.outbound_size,
            },
            "last_processed_at": self.last_processed_at,
        }
        return json.dumps(payload, indent=2).encode()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = request_line.decode(errors="replace").split()
            if len(parts) < 2:
                writer.write(_HTTP_404)
                await writer.drain()
                return

            method, path = parts[0], parts[1].split("?")[0]

            # Drain remaining headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                writer.write(_HTTP_405)
            elif path == "/health":
                body = self._build_payload()
                writer.write(_HTTP_200 + body)
            else:
                writer.write(_HTTP_404)

            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port
        )
        logger.info("Health server started", host=self.host, port=self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Health server stopped")
