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
from urllib.parse import parse_qs, urlsplit
from typing import TYPE_CHECKING

from nanobot.agent.turn_events import turn_event_capabilities
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

    def _stream_diagnostics(self) -> dict[str, object]:
        channels_cfg = getattr(self.agent, "channels_config", None)
        stream_mode = str(getattr(channels_cfg, "stream_mode", "auto") or "auto").strip().lower()
        stream_enabled_flag = bool(getattr(channels_cfg, "stream_enabled", False))
        legacy_edit_flag = bool(getattr(channels_cfg, "progress_edit_streaming_enabled", False))
        send_progress = bool(getattr(channels_cfg, "send_progress", True))

        if stream_mode == "off":
            effective_stream_enabled = False
        elif stream_mode == "force":
            effective_stream_enabled = True
        else:
            effective_stream_enabled = bool(stream_enabled_flag or legacy_edit_flag)

        provider = getattr(self.agent, "provider", None)
        provider_stream_chat = getattr(provider, "stream_chat", None)
        provider_stream_supported = callable(provider_stream_chat)

        channels_map = getattr(self.channels, "channels", {}) or {}
        editable_channels = sorted(
            name for name, ch in channels_map.items()
            if bool(getattr(ch, "supports_progress_message_editing", False))
        )

        llm_stream_ready = bool(effective_stream_enabled and provider_stream_supported)
        progress_path_ready = bool(send_progress or effective_stream_enabled)
        stream_effective = bool(llm_stream_ready and progress_path_ready)

        reasons: list[str] = []
        if effective_stream_enabled and not provider_stream_supported:
            reasons.append("provider_missing_stream_chat")
        if effective_stream_enabled and not progress_path_ready:
            reasons.append("progress_path_disabled")
        if not effective_stream_enabled:
            reasons.append("stream_disabled")
        if stream_effective:
            reasons.append("ok")

        return {
            "stream_mode": stream_mode,
            "stream_enabled": stream_enabled_flag,
            "legacy_progress_edit_enabled": legacy_edit_flag,
            "effective_stream_enabled": effective_stream_enabled,
            "send_progress": send_progress,
            "provider_stream_supported": provider_stream_supported,
            "llm_stream_ready": llm_stream_ready,
            "editable_channels": editable_channels,
            "progress_path_ready": progress_path_ready,
            "stream_effective": stream_effective,
            "reason": reasons,
        }

    def _build_payload(self, *, debug: str | None = None) -> bytes:
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
        if debug == "events":
            payload["debug"] = {
                "turn_event_capabilities": turn_event_capabilities(),
                "stream_diagnostics": self._stream_diagnostics(),
            }
        elif debug == "stream":
            payload["debug"] = {"stream_diagnostics": self._stream_diagnostics()}
        return json.dumps(payload, indent=2).encode()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = request_line.decode(errors="replace").split()
            if len(parts) < 2:
                writer.write(_HTTP_404)
                await writer.drain()
                return

            method = parts[0]
            target = parts[1]
            parsed = urlsplit(target)
            path = parsed.path
            query = parse_qs(parsed.query)

            # Drain remaining headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                writer.write(_HTTP_405)
            elif path == "/health":
                debug_param = query.get("debug", [None])[0]
                body = self._build_payload(debug=debug_param)
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
