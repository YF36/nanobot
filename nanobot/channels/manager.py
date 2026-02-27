"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.logging import get_logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.ratelimit import RateLimiter
from nanobot.config.schema import Config

logger = get_logger(__name__)


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.
    
    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """
    
    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        
        self._init_channels()
    
    def _init_channels(self) -> None:
        """Initialize channels based on config."""
        rl_cfg = self.config.channels.rate_limit
        rate_limiter: RateLimiter | None = None
        if rl_cfg.enabled:
            rate_limiter = RateLimiter(
                max_messages=rl_cfg.max_messages,
                window_seconds=rl_cfg.window_seconds,
            )
            logger.info(
                "Rate limiter enabled",
                max_messages=rl_cfg.max_messages,
                window_seconds=rl_cfg.window_seconds,
            )
        
        # Telegram channel
        if self.config.channels.telegram.enabled:
            try:
                from nanobot.channels.telegram import TelegramChannel
                self.channels["telegram"] = TelegramChannel(
                    self.config.channels.telegram,
                    self.bus,
                    groq_api_key=self.config.providers.groq.api_key,
                    rate_limiter=rate_limiter,
                )
                logger.info("Telegram channel enabled")
            except ImportError as e:
                logger.warning("Telegram channel not available", error=str(e))
        
        # WhatsApp channel
        if self.config.channels.whatsapp.enabled:
            try:
                from nanobot.channels.whatsapp import WhatsAppChannel
                self.channels["whatsapp"] = WhatsAppChannel(
                    self.config.channels.whatsapp, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("WhatsApp channel enabled")
            except ImportError as e:
                logger.warning("WhatsApp channel not available", error=str(e))

        # Discord channel
        if self.config.channels.discord.enabled:
            try:
                from nanobot.channels.discord import DiscordChannel
                self.channels["discord"] = DiscordChannel(
                    self.config.channels.discord, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("Discord channel enabled")
            except ImportError as e:
                logger.warning("Discord channel not available", error=str(e))
        
        # Feishu channel
        if self.config.channels.feishu.enabled:
            try:
                from nanobot.channels.feishu import FeishuChannel
                self.channels["feishu"] = FeishuChannel(
                    self.config.channels.feishu, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("Feishu channel enabled")
            except ImportError as e:
                logger.warning("Feishu channel not available", error=str(e))

        # Mochat channel
        if self.config.channels.mochat.enabled:
            try:
                from nanobot.channels.mochat import MochatChannel

                self.channels["mochat"] = MochatChannel(
                    self.config.channels.mochat, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("Mochat channel enabled")
            except ImportError as e:
                logger.warning("Mochat channel not available", error=str(e))

        # DingTalk channel
        if self.config.channels.dingtalk.enabled:
            try:
                from nanobot.channels.dingtalk import DingTalkChannel
                self.channels["dingtalk"] = DingTalkChannel(
                    self.config.channels.dingtalk, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("DingTalk channel enabled")
            except ImportError as e:
                logger.warning("DingTalk channel not available", error=str(e))

        # Email channel
        if self.config.channels.email.enabled:
            try:
                from nanobot.channels.email import EmailChannel
                self.channels["email"] = EmailChannel(
                    self.config.channels.email, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("Email channel enabled")
            except ImportError as e:
                logger.warning("Email channel not available", error=str(e))

        # Slack channel
        if self.config.channels.slack.enabled:
            try:
                from nanobot.channels.slack import SlackChannel
                self.channels["slack"] = SlackChannel(
                    self.config.channels.slack, self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("Slack channel enabled")
            except ImportError as e:
                logger.warning("Slack channel not available", error=str(e))

        # QQ channel
        if self.config.channels.qq.enabled:
            try:
                from nanobot.channels.qq import QQChannel
                self.channels["qq"] = QQChannel(
                    self.config.channels.qq,
                    self.bus,
                    rate_limiter=rate_limiter,
                )
                logger.info("QQ channel enabled")
            except ImportError as e:
                logger.warning("QQ channel not available", error=str(e))

    @staticmethod
    def _effective_stream_enabled(channels_cfg: Any) -> bool:
        mode = str(getattr(channels_cfg, "stream_mode", "auto") or "auto").strip().lower()
        if mode == "off":
            return False
        if mode == "force":
            return True
        return bool(
            getattr(channels_cfg, "stream_enabled", False)
            or getattr(channels_cfg, "progress_edit_streaming_enabled", False)
        )
    
    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel", channel=name, error=str(e))

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return
        
        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        
        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting channel", channel=name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))
        
        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")
        
        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        
        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped channel", channel=name)
            except Exception as e:
                logger.error("Error stopping channel", channel=name, error=str(e))
    
    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")
        
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                stream_enabled = self._effective_stream_enabled(self.config.channels)
                effective_send_progress = bool(self.config.channels.send_progress or stream_enabled)
                effective_progress_edit_streaming = bool(
                    self.config.channels.progress_edit_streaming_enabled or stream_enabled
                )
                
                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not effective_send_progress:
                        continue
                
                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        progress_edit_enabled = bool(
                            effective_progress_edit_streaming
                            and getattr(channel, "supports_progress_message_editing", False)
                        )
                        outbound = msg
                        if msg.metadata.get("_progress") and not msg.metadata.get("_tool_hint") and progress_edit_enabled:
                            meta = dict(msg.metadata or {})
                            meta["_progress_edit"] = True
                            outbound = OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=msg.content,
                                reply_to=msg.reply_to,
                                media=list(msg.media or []),
                                metadata=meta,
                            )
                        elif (
                            not msg.metadata.get("_progress")
                            and msg.metadata.get("_progress_done")
                            and progress_edit_enabled
                        ):
                            meta = dict(msg.metadata or {})
                            meta["_progress_finalize_edit"] = True
                            outbound = OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=msg.content,
                                reply_to=msg.reply_to,
                                media=list(msg.media or []),
                                metadata=meta,
                            )

                        if (
                            not msg.metadata.get("_progress")
                            and msg.metadata.get("_progress_done")
                            and self.config.channels.progress_done_marker_enabled
                            and not progress_edit_enabled
                        ):
                            marker = (self.config.channels.progress_done_marker_text or "").strip()
                            if marker:
                                await channel.send(OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=marker,
                                    metadata={"_progress_marker": True},
                                ))
                        await channel.send(outbound)
                    except Exception as e:
                        logger.error("Error sending to channel", channel=msg.channel, error=str(e))
                else:
                    logger.warning("Unknown channel", channel=msg.channel)
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
    
    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)
    
    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }
    
    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
