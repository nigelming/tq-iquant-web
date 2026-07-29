from typing import List, Optional

from .portfolio import Portfolio
from .event import BarEvent


class LiveEngine:
    def __init__(self, session_id: int):
        self.session_id = session_id
        self.portfolios: List[Portfolio] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def on_bar(self, bar: BarEvent) -> None:
        pass

    async def recover(self) -> None:
        pass
