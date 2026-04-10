import asyncio
import json
import logging
from collections import deque
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)


class StreamConsumer:
    """Consumes SSE stream and stores latest values."""

    def __init__(self, url: str = "http://127.0.0.1:8000/stream"):
        self.url = url
        self.value: float | None = None
        self.timestamp: datetime | None = None
        self.connected = False
        self.history = deque(maxlen=100)
        self._running = False

    async def start(self):
        """Connect to SSE stream and consume events.

        Note: This is an optional demo feature. Connection failures are logged
        but do not affect core functionality.
        """
        self._running = True
        retry_delay = 1.0
        max_retries = 3
        retry_count = 0

        try:
            while self._running and retry_count < max_retries:
                try:
                    logger.info(f"Connecting to {self.url}")
                    async with httpx.AsyncClient(timeout=None) as client:
                        async with client.stream("GET", self.url) as response:
                            self.connected = True
                            logger.info("Connected to stream")
                            retry_count = 0  # Reset on successful connection

                            buffer = ""
                            async for chunk in response.aiter_text():
                                if not self._running:
                                    break
                                buffer += chunk
                                while "\n\n" in buffer:
                                    event, buffer = buffer.split("\n\n", 1)
                                    for line in event.split("\n"):
                                        if line.startswith("data: "):
                                            data = json.loads(line[6:])
                                            self.value = data["value"]
                                            self.timestamp = datetime.now(UTC)
                                            self.history.append((self.value, self.timestamp))
                except Exception as e:
                    self.connected = False
                    retry_count += 1
                    if retry_count < max_retries:
                        logger.warning(
                            f"Stream connection attempt {retry_count}/{max_retries} failed: {e}"
                        )
                        if self._running:
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(retry_delay * 2, 30)
                    else:
                        logger.info(
                            f"Stream consumer: Could not connect to {self.url} (optional demo feature - skipping)"
                        )
                        break
        finally:
            # Ensure state is consistent when exiting
            self._running = False
            self.connected = False
            logger.debug("Stream consumer stopped")

    async def stop(self):
        """Stop consuming stream."""
        self._running = False
        self.connected = False
