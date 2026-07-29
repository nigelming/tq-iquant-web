from typing import Optional


NATS_SUBJECTS = {
    "order_place": "iquant.iguant.order.place",
    "order_query": "iquant.iguant.order.query",
    "order_cancel": "iquant.iguant.order.cancel",
    "position_query": "iquant.iguant.position.query",
    "status": "iquant.iguant.status",
}


NATS_REQUEST_TIMEOUT = 10.0
