"""Regression for inbound message text leaking into gateway logs."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_inbound_log_records_lengths_without_message_or_quote(caplog):
    runner = object.__new__(GatewayRunner)
    runner._hmwa_resolve_session = AsyncMock(return_value=None)
    event = SimpleNamespace(
        text="private-message-canary",
        reply_to_text="private-quoted-canary",
        reply_to_message_id="reply-17",
    )
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        user_name=None,
        user_id="user-7",
        chat_id="chat-9",
    )

    with caplog.at_level(logging.INFO):
        await runner._handle_message_with_agent(event, source, "test", 1)

    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "msg_chars=22" in output
    assert "reply_to_chars=21" in output
    assert "reply_to_id=reply-17" in output
    assert "private-message-canary" not in output
    assert "private-quoted-canary" not in output
