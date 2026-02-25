"""Tests for health check server (6.2)."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from nanobot.health import HealthServer


def _make_server(port: int) -> HealthServer:
    agent = MagicMock()
    agent._running = True

    bus = MagicMock()
    bus.inbound_size = 0
    bus.outbound_size = 1

    channels = MagicMock()
    channels.get_status.return_value = {"telegram": {"connected": True}}

    return HealthServer(agent=agent, bus=bus, channels=channels, host="127.0.0.1", port=port)


class TestHealthServer:
    @pytest.mark.asyncio
    async def test_health_returns_200_json(self) -> None:
        """GET /health returns 200 with valid JSON payload."""
        server = _make_server(port=18765)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18765)
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            writer.close()

            assert b"200 OK" in response
            body = response.split(b"\r\n\r\n", 1)[1]
            data = json.loads(body)
            assert data["status"] == "ok"
            assert data["agent_loop"]["running"] is True
            assert "channels" in data
            assert "queue" in data
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_unknown_path_returns_404(self) -> None:
        """GET /unknown returns 404."""
        server = _make_server(port=18766)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18766)
            writer.write(b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(1024), timeout=3.0)
            writer.close()

            assert b"404" in response
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_post_returns_405(self) -> None:
        """POST /health returns 405."""
        server = _make_server(port=18767)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18767)
            writer.write(b"POST /health HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(1024), timeout=3.0)
            writer.close()

            assert b"405" in response
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_queue_depths_in_payload(self) -> None:
        """Queue depths are reflected in /health response."""
        server = _make_server(port=18768)
        server.bus.inbound_size = 3
        server.bus.outbound_size = 7
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18768)
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            writer.close()

            body = response.split(b"\r\n\r\n", 1)[1]
            data = json.loads(body)
            assert data["queue"]["inbound_depth"] == 3
            assert data["queue"]["outbound_depth"] == 7
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_last_processed_at_updated(self) -> None:
        """`record_processed()` updates last_processed_at in response."""
        server = _make_server(port=18769)
        assert server.last_processed_at is None

        server.record_processed()
        assert server.last_processed_at is not None

        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18769)
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            writer.close()

            body = response.split(b"\r\n\r\n", 1)[1]
            data = json.loads(body)
            assert data["last_processed_at"] is not None
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_health_debug_events_includes_turn_event_capabilities(self) -> None:
        """GET /health?debug=events includes turn-event capabilities manifest."""
        server = _make_server(port=18770)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 18770)
            writer.write(b"GET /health?debug=events HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()

            response = await asyncio.wait_for(reader.read(8192), timeout=3.0)
            writer.close()

            body = response.split(b"\r\n\r\n", 1)[1]
            data = json.loads(body)
            assert "debug" in data
            manifest = data["debug"]["turn_event_capabilities"]
            assert manifest["namespace"] == "nanobot.turn"
            assert manifest["version"] == 1
            assert any(e["kind"] == "turn.start" for e in manifest["events"])
        finally:
            await server.stop()
