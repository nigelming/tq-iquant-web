from enum import Enum


class SignalType(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"


class TradeType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class StrategyRole(str, Enum):
    INDEPENDENT = "independent"
    MASTER = "master"
    SLAVE = "slave"


class LiveMode(str, Enum):
    SIMULATION = "simulation"
    LIVE = "live"


class SessionStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class BacktestStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
