import asyncio

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


class _FakeChannel(BaseChannel):
    name = "fake"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_dispatch_outbound_sends_progress_done_marker_when_enabled() -> None:
    config = Config()
    config.channels.progress_done_marker_enabled = True
    config.channels.progress_done_marker_text = "stream done"
    bus = MessageBus()
    manager = ChannelManager(config, bus)

    fake = _FakeChannel(config.channels, bus)
    manager.channels = {"cli": fake}

    task = asyncio.create_task(manager._dispatch_outbound())
    try:
        await bus.publish_outbound(OutboundMessage(
            channel="cli",
            chat_id="chat_1",
            content="final content",
            metadata={"_progress_done": True},
        ))
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await task

    assert len(fake.sent) == 2
    assert fake.sent[0].content == "stream done"
    assert fake.sent[0].metadata.get("_progress_marker") is True
    assert fake.sent[1].content == "final content"
