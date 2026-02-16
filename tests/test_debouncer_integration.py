"""Integration tests for debouncer with AgentLoop."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.bus.debouncer import InboundDebouncer


@pytest.mark.asyncio
async def test_three_rapid_messages_one_response():
    """Simulate 3 rapid Telegram messages → agent receives 1 concatenated message."""
    bus = MessageBus()
    debouncer = InboundDebouncer(bus=bus, debounce_overrides={"telegram": 50})

    task = asyncio.create_task(debouncer.run())
    try:
        # Simulate 3 rapid messages
        for text in ["Ciao!", "Come stai?", "Dimmi le attivita"]:
            await bus.publish_inbound(InboundMessage(
                channel="telegram",
                sender_id="user1",
                chat_id="123",
                content=text,
            ))
            await asyncio.sleep(0.01)

        # Agent should receive one merged message
        msg = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert "Ciao!" in msg.content
        assert "Come stai?" in msg.content
        assert "Dimmi le attivita" in msg.content
        assert msg.content.count("\n") == 2  # 3 lines joined by \n
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_message_during_processing_collected():
    """Messages arriving during agent processing should be collected and released after."""
    bus = MessageBus()
    debouncer = InboundDebouncer(bus=bus, debounce_overrides={"telegram": 30})

    task = asyncio.create_task(debouncer.run())
    try:
        # First message — agent free
        await bus.publish_inbound(InboundMessage(
            channel="telegram", sender_id="u1", chat_id="42", content="Task 1",
        ))
        msg1 = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg1.content == "Task 1"

        # Simulate agent busy
        debouncer.notify_agent_busy()

        # Send follow-up while busy
        await bus.publish_inbound(InboundMessage(
            channel="telegram", sender_id="u1", chat_id="42", content="Also do this",
        ))
        await asyncio.sleep(0.1)  # Let debounce fire

        # Not available yet
        assert debouncer._ready.empty()

        # Agent finishes
        debouncer.notify_agent_free()
        msg2 = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg2.content == "Also do this"
    finally:
        debouncer.stop()
        task.cancel()
