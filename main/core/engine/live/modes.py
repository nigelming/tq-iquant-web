# -*- coding: utf-8 -*-
"""实盘执行模式谓词（0018）。

- alert：预警模式——TQ 行情算信号、落库 + SSE；send_warn 推送受
  ``live.alert_send_warn_enabled`` 开关控制（0017 默认关）。
- hermes（0018）：人工执行模式——行为同 alert（只算信号不下单），信号标记
  hermes，供另一台电脑经 ``GET /api/hermes/signals`` 拉取后人工下单。
- auto：全自动——按会话引擎（tdx/ths/…）自动下单。

alert 与 hermes 在引擎/服务层的所有分支语义一致（signal-only），统一用
``is_signal_only()`` 判定，不要散落 ``== "alert"`` 字面比较。
"""

SIGNAL_ONLY_MODES = ("alert", "hermes")


def is_signal_only(mode) -> bool:
    """该执行模式是否「只算信号不下单」（alert / hermes）。"""
    return mode in SIGNAL_ONLY_MODES
