import os
import sys
import threading
from pathlib import Path
from typing import Optional

import yaml


def is_frozen_app() -> bool:
    """是否为打包后的独立程序（exe）。

    Nuitka 与 PyInstaller 不同：standalone 编译后**不会**设置 sys.frozen，
    必须额外用 argv[0] 是否为 .exe 判断；否则所有"包内资源定位"都会错误
    回退到 __file__ 推算路径（实测导致读到空配置、tdx_path 回退错误目录）。

    pytest 下 argv[0] 可能是 pytest.exe，需显式排除，避免测试误判为打包环境。
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is not None or "pytest" in sys.modules:
        return False
    return hasattr(sys, "frozen") or Path(sys.argv[0]).suffix.lower() == ".exe"


def _default_config_path() -> Path:
    if is_frozen_app():
        return Path(sys.argv[0]).resolve().parent / "config.yaml"
    return Path(__file__).resolve().parent.parent.parent / "config.yaml"


def get_config_path() -> Path:
    return _default_config_path()


def _defaults() -> dict:
    return {
        "tdx_path": "",
        "iquant_path": "",
        "max_concurrent_backtest": 1,
        "database": {
            "sqlite_path": "data/dev.db",
        },
        "iquant_bridge": {
            "simulation": {"base_url": "http://127.0.0.1:8790"},
            "live": {"base_url": "http://127.0.0.1:8791"},
            # 桥请求超时（秒）：桥在本机 loopback，正常毫秒~秒级；超时按在途处理，
            # 不判拒单。缺省/非法回退该值（live_service.iquant_config）。
            "request_timeout_seconds": 10.0,
            # 慢响应告警阈值（秒）：须明显小于请求超时；超阈值记 WARNING + risk/bridge_slow。
            "slow_response_seconds": 5.0,
            # 同一会话慢告警冷却窗（秒），避免轮询刷屏。
            "slow_warn_interval_seconds": 300.0,
        },
        # 掘金量化桥（2026-11-04 替代 QMT 桥）：桥策略跑在掘金终端内
        # （live/bridge/goldminer_bridge.py），base_url 指向其本地 HTTP 服务。
        "goldminer": {
            "base_url": "http://127.0.0.1:7801",
            "token": "",
            "account_id": "",
            # 桥请求超时（秒）：桥内全局锁串行，实测单次 4~10s；下单超时须
            # ≥30s 且小于主循环周期 60s（超时后按在途处理，不判拒单）。
            "request_timeout_seconds": 30.0,
            # 慢响应告警阈值（秒）：须明显小于请求超时；超阈值记 WARNING +
            # risk/bridge_slow 事件（按 endpoint 冷却去重）。
            "slow_response_seconds": 15.0,
            # 同一 endpoint 慢告警冷却窗（秒），避免 5s 轮询刷屏。
            "slow_warn_interval_seconds": 300.0,
        },
        "ths_ui": {
            "enabled": False,
            "window_title": "网上股票交易系统5.0",
            "client_exe_path": "D:/同花顺远航版/transaction/xiadan.exe",
            "driver": "clipboard",
            "captcha_ocr": False,
            # 委托价模式：approval=键入审批价（信号价）限价，委托价≠审批价自动撤单；
            # latest=不键价格，让 THS 代码键入后自动跟出最新价（日线信号跨日跳空补救）。
            "price_mode": "approval",
            # 文件交接层：file_builtin=信号/风控/指令落 JSONL + 内置后台执行器按
            # 频率执行（place_order 秒回）；direct=同步 UI 自动化直呼（原行为）。
            "execute_mode": "file_builtin",
            "execute_interval_seconds": 5,
            # 交接目录（相对路径按服务进程 CWD=main/ 解析），实际文件在
            # {work_dir}/{session_id}/ 下。
            "work_dir": "data/ths",
        },
        # 实盘引擎存活监控（进程内 EngineWatchdog，单位秒；非法/非正回退默认）。
        # heartbeat_timeout_seconds：引擎 tick/账户快照双信号静默阈值；
        # execution_stall_seconds：ths 文件交接层积压/cursor 停滞阈值；
        # bridge_timeout_seconds：HTTP 桥持续离线阈值；
        # market_data_stall_seconds：tdx 最后一根 bar 停滞阈值。
        "live": {
            "heartbeat_timeout_seconds": 90,
            "execution_stall_seconds": 180,
            "bridge_timeout_seconds": 90,
            "market_data_stall_seconds": 180,
            # 行情轮询窗口（Asia/Shanghai，左闭右开）：窗口外（交易日 15:05 之后 /
            # 次日 09:15 之前 / 非交易日）完全停 BarPoller 行情拉取，消除收盘后对
            # 掘金桥 /quote 与通达信 TQ 的无谓持续压力；账户快照/成交回填/收盘清扫
            # 不受影响。enabled=false 恢复旧行为（全天轮询）。缺该键 → 安全默认
            # （即下列值：收盘后停轮询）。
            "quote_polling": {
                "enabled": True,
                "resume_time": "09:15",
                "pause_time": "15:05",
            },
        },
        "tq_trade": {
            "account": "",
            "account_type": "stock",
            "price_mode": "my",
            "client_path": "",
            # A-6：新建实盘会话未显式选引擎时的默认交易引擎（用户主通道 tdx）。
            "default_engine": "tdx",
            # A-2：在途单超时未成交自动撤单（分钟）；<=0 = 禁用。
            "cancel_unfilled_minutes": 3,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，嵌套 dict 逐层合并而非整体替换。"""
    merged = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else _default_config_path()
    if not path.exists():
        return _defaults()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return _deep_merge(_defaults(), cfg)


_CONFIG_WRITE_LOCK = threading.Lock()


def save_config(cfg: dict, config_path: Optional[str] = None) -> None:
    """原子写配置（临时文件 + os.replace）并串行化并发写。

    直接 open("w") 会先截断原文件：进程在写入中崩溃/被强杀，config.yaml
    变成空文件，下次加载静默回退全默认（tdx_path/iquant_path 等丢失）。
    保存路由跑在线程池，并发请求互相截断还会写出错乱内容 → 进程内写锁。
    """
    path = Path(config_path) if config_path else _default_config_path()
    text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True)
    with _CONFIG_WRITE_LOCK:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
