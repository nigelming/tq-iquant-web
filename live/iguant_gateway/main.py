"""iQuant 网关入口（Python 3.7）

通过 NATS 接收 Core 的请求，调用 xquant(xtquant) 库执行交易操作。
"""

import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    logger.info("iQuant Gateway starting...")
    # NATS 连接 + 订阅 handler，后续阶段实现
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
