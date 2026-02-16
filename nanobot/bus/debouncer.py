"""Inbound message debouncer for batching rapid consecutive messages."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


# Hardcoded defaults for conversational channels (milliseconds).
# Channels not listed here get 0ms (immediate passthrough).
DEBOUNCE_DEFAULTS: dict[str, int] = {
    "telegram": 2000,
    "whatsapp": 5000,
    "discord": 1500,
    "slack": 1500,
    "qq": 2000,
    "dingtalk": 2000,
    "feishu": 2000,
    "mochat": 2000,
}


@dataclass
class _Buffer:
    """Accumulates messages for a single session during the debounce window."""
    messages: list[InboundMessage] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


class InboundDebouncer:
    """
    Middleware that batches rapid inbound messages per session.

    Sits between MessageBus and AgentLoop:
      Channel → MessageBus → InboundDebouncer → AgentLoop

    When the agent is busy, debounced messages go to a collect queue
    and are released when the agent signals it is free.
    """

    def __init__(
        self,
        bus: MessageBus,
        debounce_overrides: dict[str, int] | None = None,
    ):
        self._bus = bus
        self._overrides = debounce_overrides or {}

        self._buffers: dict[str, _Buffer] = {}
        self._ready: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._collect: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._agent_busy = False
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Background task: consume from bus, manage buffers and timers."""
        self._running = True
        self._loop = asyncio.get_running_loop()
        logger.info("Debouncer started")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self._bus.consume_inbound(), timeout=0.5
                )
                self._on_message(msg)
            except asyncio.TimeoutError:
                continue

    async def consume(self) -> InboundMessage:
        """Drop-in replacement for bus.consume_inbound().

        Blocks until a debounced message is ready.
        """
        return await self._ready.get()

    def notify_agent_busy(self) -> None:
        """Called by AgentLoop when it starts processing a message."""
        self._agent_busy = True

    def notify_agent_free(self) -> None:
        """Called by AgentLoop when it finishes processing.

        Moves the next collected message (if any) to the ready queue.
        """
        self._agent_busy = False
        if not self._collect.empty():
            try:
                msg = self._collect.get_nowait()
                self._ready.put_nowait(msg)
            except asyncio.QueueEmpty:
                pass

    def stop(self) -> None:
        """Stop the debouncer loop."""
        self._running = False
        # Cancel pending timers
        for buf in self._buffers.values():
            if buf.timer is not None:
                buf.timer.cancel()
        self._buffers.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_debounce_ms(self, channel: str) -> int:
        """Resolve debounce delay for a channel."""
        if channel in self._overrides:
            return self._overrides[channel]
        return DEBOUNCE_DEFAULTS.get(channel, 0)

    def _on_message(self, msg: InboundMessage) -> None:
        """Handle a new inbound message: buffer + reset timer."""
        key = msg.session_key
        debounce_ms = self._get_debounce_ms(msg.channel)

        if debounce_ms <= 0:
            # No debounce — deliver immediately
            self._deliver(msg)
            return

        buf = self._buffers.get(key)
        if buf is None:
            buf = _Buffer()
            self._buffers[key] = buf

        # Cancel existing timer
        if buf.timer is not None:
            buf.timer.cancel()

        buf.messages.append(msg)

        # Schedule flush after debounce window
        assert self._loop is not None
        buf.timer = self._loop.call_later(
            debounce_ms / 1000.0,
            self._flush,
            key,
        )

    def _flush(self, key: str) -> None:
        """Flush a buffer: merge messages and deliver."""
        buf = self._buffers.pop(key, None)
        if not buf or not buf.messages:
            return

        merged = self._merge(buf.messages)
        self._deliver(merged)

    def _deliver(self, msg: InboundMessage) -> None:
        """Put a message on the ready or collect queue."""
        if self._agent_busy:
            self._collect.put_nowait(msg)
            logger.debug(f"Debouncer: collected message for {msg.session_key} (agent busy)")
        else:
            self._ready.put_nowait(msg)

    @staticmethod
    def _merge(messages: list[InboundMessage]) -> InboundMessage:
        """Merge multiple messages into one."""
        if len(messages) == 1:
            return messages[0]

        first = messages[0]
        content = "\n".join(m.content for m in messages)
        media: list[str] = []
        for m in messages:
            media.extend(m.media)

        # Merge metadata (later messages override earlier keys)
        metadata: dict[str, Any] = {}
        for m in messages:
            metadata.update(m.metadata)

        return InboundMessage(
            channel=first.channel,
            sender_id=first.sender_id,
            chat_id=first.chat_id,
            content=content,
            timestamp=first.timestamp,
            media=media,
            metadata=metadata,
        )
