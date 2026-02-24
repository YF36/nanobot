"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool, ToolExecutionResult
from nanobot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    @property
    def sent_in_turn(self) -> bool:
        """Whether this tool already sent a user-visible message in the current turn."""
        return self._sent_in_turn

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str | ToolExecutionResult:
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        message_id = message_id or self._default_message_id
        attachments = media or []

        if not channel or not chat_id:
            return ToolExecutionResult(
                text="Error: No target channel/chat specified",
                details={
                    "op": "message",
                    "channel": channel,
                    "chat_id": chat_id,
                    "attachment_count": len(attachments),
                    "sent": False,
                },
                is_error=True,
            )

        if not self._send_callback:
            return ToolExecutionResult(
                text="Error: Message sending not configured",
                details={
                    "op": "message",
                    "channel": channel,
                    "chat_id": chat_id,
                    "attachment_count": len(attachments),
                    "sent": False,
                },
                is_error=True,
            )

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=attachments,
            metadata={
                "message_id": message_id,
            }
        )

        try:
            await self._send_callback(msg)
            self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return ToolExecutionResult(
                text=f"Message sent to {channel}:{chat_id}{media_info}",
                details={
                    "op": "message",
                    "channel": channel,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "attachment_count": len(attachments),
                    "sent": True,
                },
            )
        except Exception as e:
            return ToolExecutionResult(
                text=f"Error sending message: {str(e)}",
                details={
                    "op": "message",
                    "channel": channel,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "attachment_count": len(attachments),
                    "sent": False,
                    "exception_type": type(e).__name__,
                },
                is_error=True,
            )
