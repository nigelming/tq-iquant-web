import json
import uuid
from typing import Optional, Callable

import nats
from nats.aio.msg import Msg


class NatsClient:
    def __init__(self):
        self._nc = None

    async def connect(self, url: str = "nats://localhost:4222") -> None:
        self._nc = await nats.connect(url)

    async def close(self) -> None:
        if self._nc:
            await self._nc.close()
            self._nc = None

    async def request(self, subject: str, data: dict, timeout: float = 10.0) -> Optional[dict]:
        if not self._nc:
            return None
        request_id = str(uuid.uuid4())
        payload = json.dumps({"request_id": request_id, "data": data}).encode()
        try:
            msg = await self._nc.request(subject, payload, timeout=timeout)
            return json.loads(msg.data)
        except nats.errors.TimeoutError:
            return None

    async def subscribe(self, subject: str, handler: Callable) -> None:
        if not self._nc:
            return

        async def wrapped(msg: Msg):
            data = json.loads(msg.data)
            await handler(data)

        await self._nc.subscribe(subject, cb=wrapped)
