import asyncio
import json
import logging

import nats

from tq_iquant_shared.nats_schemas import NATS_SUBJECTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def handle_place_order(data):
    logger.info("place_order: %s", data)
    return {"order_id": "mock_" + str(data.get("stock_code", "")), "status": "filled"}


def handle_query_order(data):
    return {"order_id": data.get("order_id"), "status": "filled"}


def handle_cancel_order(data):
    return {"success": True}


def handle_query_position(data):
    return {"positions": []}


def handle_status(data):
    return {"online": True, "version": "1.0"}


HANDLERS = {
    NATS_SUBJECTS["order_place"]: handle_place_order,
    NATS_SUBJECTS["order_query"]: handle_query_order,
    NATS_SUBJECTS["order_cancel"]: handle_cancel_order,
    NATS_SUBJECTS["position_query"]: handle_query_position,
    NATS_SUBJECTS["status"]: handle_status,
}


async def main():
    logger.info("iQuant Gateway starting...")
    nc = await nats.connect("nats://localhost:4222")

    async def handler(msg):
        subject = msg.subject
        data = json.loads(msg.data)
        request_id = data.get("request_id")
        payload = data.get("data", {})
        logger.info("received %s: %s", subject, payload)

        func = HANDLERS.get(subject)
        if func:
            result = func(payload)
            response = json.dumps({
                "request_id": request_id,
                "success": True,
                "data": result,
                "error": None,
            }).encode()
        else:
            response = json.dumps({
                "request_id": request_id,
                "success": False,
                "data": None,
                "error": "unknown subject",
            }).encode()
        await msg.respond(response)

    for subject in HANDLERS:
        await nc.subscribe(subject, cb=handler)
        logger.info("subscribed to %s", subject)

    logger.info("iQuant Gateway ready")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
