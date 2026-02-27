"""Base channel interface for chat platforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from nanobot.logging import get_logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.channels.ratelimit import RateLimiter

logger = get_logger(__name__)
audit_log = get_logger("nanobot.audit")
from nanobot.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the nanobot message bus.
    """

    name: str = "base"
    supports_progress_message_editing: bool = False

    def __init__(self, config: Any, bus: MessageBus, rate_limiter: RateLimiter | None = None):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
            rate_limiter: Optional shared rate limiter instance.
        """
        self.config = config
        self.bus = bus
        self._running = False
        self._rate_limiter = rate_limiter
    
    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.
        
        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass
    
    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.
        
        Args:
            msg: The message to send.
        """
        pass
    
    def is_allowed(self, sender_id: str) -> bool:
        """
        Check if a sender is allowed to use this bot.
        
        Args:
            sender_id: The sender's identifier.
        
        Returns:
            True if allowed, False otherwise.
        """
        allow_list = getattr(self.config, "allow_from", [])
        
        # If no allow list, allow everyone
        if not allow_list:
            return True
        
        sender_str = str(sender_id)
        if sender_str in allow_list:
            return True
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False
    
    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None
    ) -> None:
        """
        Handle an incoming message from the chat platform.
        
        This method checks permissions and forwards to the bus.
        
        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender on channel",
                sender_id=sender_id, channel=self.name,
            )
            audit_log.warning(
                "channel_access_denied",
                sender_id=sender_id, channel=self.name,
            )
            return

        if self._rate_limiter and not self._rate_limiter.is_allowed(sender_id):
            audit_log.warning(
                "channel_rate_limited",
                sender_id=sender_id, channel=self.name,
            )
            return

        audit_log.info(
            "channel_message_accepted",
            sender_id=sender_id, channel=self.name, chat_id=chat_id,
        )

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {}
        )

        await self.bus.publish_inbound(msg)
    
    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
