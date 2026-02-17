"""Tests for InboundDebouncer."""

import asyncio
import pytest
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.bus.debouncer import InboundDebouncer


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def debouncer(bus):
    """Debouncer with fast timeouts for testing."""
    return InboundDebouncer(bus=bus, debounce_overrides={"telegram": 50})


async def _publish(bus, content, channel="telegram", chat_id="123", media=None):
    """Helper to publish a test message."""
    await bus.publish_inbound(InboundMessage(
        channel=channel,
        sender_id="user1",
        chat_id=chat_id,
        content=content,
        media=media or [],
    ))


@pytest.mark.asyncio
async def test_single_message_delivered_after_timeout(bus, debouncer):
    """A single message should be delivered after the debounce timeout."""
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "Hello")
        msg = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg.content == "Hello"
        assert msg.channel == "telegram"
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_rapid_messages_batched(bus, debouncer):
    """Rapid messages from same session should be concatenated."""
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "Ciao!")
        await asyncio.sleep(0.01)  # Within debounce window
        await _publish(bus, "Come stai?")
        await asyncio.sleep(0.01)
        await _publish(bus, "Dimmi le attivita")

        msg = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg.content == "Ciao!\nCome stai?\nDimmi le attivita"
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_media_paths_merged(bus, debouncer):
    """Media paths from batched messages should be merged."""
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "Photo 1", media=["/tmp/a.jpg"])
        await asyncio.sleep(0.01)
        await _publish(bus, "Photo 2", media=["/tmp/b.jpg"])

        msg = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg.content == "Photo 1\nPhoto 2"
        assert msg.media == ["/tmp/a.jpg", "/tmp/b.jpg"]
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_no_debounce_for_unknown_channel(bus):
    """Channels not in defaults should pass through immediately (0ms)."""
    debouncer = InboundDebouncer(bus=bus, debounce_overrides={})
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "Hello", channel="email")
        msg = await asyncio.wait_for(debouncer.consume(), timeout=0.5)
        assert msg.content == "Hello"
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_config_override_respected(bus):
    """Config override should take precedence over hardcoded default."""
    debouncer = InboundDebouncer(bus=bus, debounce_overrides={"telegram": 30})
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "Fast")
        msg = await asyncio.wait_for(debouncer.consume(), timeout=0.5)
        assert msg.content == "Fast"
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_timer_reset_on_new_message(bus, debouncer):
    """New message should reset the debounce timer."""
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "First")
        await asyncio.sleep(0.03)  # 30ms — within 50ms window
        await _publish(bus, "Second")
        # Timer resets, so both should arrive together after another 50ms
        msg = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg.content == "First\nSecond"
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_independent_sessions(bus, debouncer):
    """Different sessions should have independent buffers."""
    task = asyncio.create_task(debouncer.run())
    try:
        await _publish(bus, "User A", chat_id="aaa")
        await asyncio.sleep(0.01)
        await _publish(bus, "User B", chat_id="bbb")

        # Both should arrive as separate messages
        msgs = []
        for _ in range(2):
            msg = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
            msgs.append(msg)

        contents = {m.content for m in msgs}
        assert contents == {"User A", "User B"}
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_collect_when_agent_busy(bus, debouncer):
    """Messages arriving when agent is busy should go to collect queue."""
    task = asyncio.create_task(debouncer.run())
    try:
        # First message — agent is free
        await _publish(bus, "First")
        msg1 = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg1.content == "First"

        # Agent starts processing
        debouncer.notify_agent_busy()

        # Second message while agent is busy
        await _publish(bus, "Second")
        await asyncio.sleep(0.1)  # Let debounce timer fire

        # Should NOT be available on ready queue
        assert debouncer._ready.empty()

        # Agent finishes — collected message should become available
        debouncer.notify_agent_free()
        msg2 = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg2.content == "Second"
    finally:
        debouncer.stop()
        task.cancel()


@pytest.mark.asyncio
async def test_collect_preserves_order(bus, debouncer):
    """Multiple collected messages should be released in FIFO order."""
    task = asyncio.create_task(debouncer.run())
    try:
        debouncer.notify_agent_busy()

        await _publish(bus, "A", chat_id="aaa")
        await asyncio.sleep(0.1)
        await _publish(bus, "B", chat_id="bbb")
        await asyncio.sleep(0.1)

        # Release first
        debouncer.notify_agent_free()
        msg1 = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg1.content == "A"

        # Simulate busy again, then release
        debouncer.notify_agent_busy()
        debouncer.notify_agent_free()
        msg2 = await asyncio.wait_for(debouncer.consume(), timeout=1.0)
        assert msg2.content == "B"
    finally:
        debouncer.stop()
        task.cancel()
