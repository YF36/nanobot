from __future__ import annotations

import httpx
import pytest

from nanobot.agent.tools.web import WebSearchTool


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.request = httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, events: list[object], **kwargs):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, *args, **kwargs):
        event = self._events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


@pytest.mark.asyncio
async def test_web_search_retries_timeout_then_succeeds(monkeypatch) -> None:
    events: list[object] = [
        httpx.ReadTimeout("timeout"),
        _FakeResponse(
            payload={
                "web": {
                    "results": [
                        {
                            "title": "Result A",
                            "url": "https://example.com/a",
                            "description": "desc",
                        }
                    ]
                }
            }
        ),
    ]

    monkeypatch.setattr(
        "nanobot.agent.tools.web.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(events, **kwargs),
    )

    tool = WebSearchTool(api_key="test", max_retries=1, timeout_s=1.0)
    out = await tool.execute("test query")
    assert "Results for: test query" in out
    assert "Result A" in out
    assert events == []


@pytest.mark.asyncio
async def test_web_search_does_not_retry_non_retryable_http_400(monkeypatch) -> None:
    events: list[object] = [_FakeResponse(status_code=400)]
    monkeypatch.setattr(
        "nanobot.agent.tools.web.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(events, **kwargs),
    )

    tool = WebSearchTool(api_key="test", max_retries=2, timeout_s=1.0)
    out = await tool.execute("bad request")
    assert out.startswith("Error:")
    assert len(events) == 0  # exactly one attempt consumed
