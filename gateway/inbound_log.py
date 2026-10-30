"""Privacy-conscious logging for accepted inbound gateway messages."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def log_inbound_message(event: Any, source: Any) -> None:
    """Log routing metadata and text lengths without retaining message content."""
    platform = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
    message_text = event.text or ""
    reply_text = getattr(event, "reply_to_text", None) or ""
    logger.info(
        "inbound message: platform=%s user=%s chat=%s msg_chars=%d reply_to_id=%s reply_to_chars=%d",
        platform,
        source.user_name or source.user_id or "unknown",
        source.chat_id or "unknown",
        len(message_text),
        getattr(event, "reply_to_message_id", None),
        len(reply_text),
    )
