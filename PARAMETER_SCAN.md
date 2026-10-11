# 参数核查清单（初步扫描）

| 文件 | 参数/字段 | 默认/常量值（片段） | 来源路径（推测） | 是否硬编码 | 是否需暴露 |
|---|---|---|---|---|---|
| D:/project/tq-iquant-web/main/core/engine/execution_engine.py | min_commission | min_commission | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/execution_engine.py | buy_commission_rate | buy_commission_rate | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/execution_engine.py | sell_commission_rate | sell_commission_rate | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/execution_engine.py | stamp_duty_rate | stamp_duty_rate | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/execution_engine.py | slippage | slippage | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | stop_loss_ratio | stop_loss_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | take_profit_ratio | take_profit_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | trailing_stop_ratio | trailing_stop_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | max_drawdown | max_drawdown | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | daily_loss_limit | daily_loss_limit | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | consecutive_drawdown_triggers | 0 | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | circuit_breaker_active | False | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | peak_value | Decimal("0") | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | manual_recovery | False | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | daily_pause_active | False | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | prev_close_value | self._last_bar_total_value | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | peak_value | total_value | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | circuit_breaker_active | True | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | breaker_trigger_date | current_date | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | daily_pause_active | True | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | daily_loss_trigger_date | current_date | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/risk_manager.py | manual_recovery | True | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | capital_ratio | capital_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | max_positions | max_positions | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | single_open_ratio | single_open_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | add_position_threshold | add_position_threshold | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | max_add_count | max_add_count | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | add_position_ratio | add_position_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/engine/strategy_context.py | reduce_position_ratio | reduce_position_ratio | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | max_drawdown | Column(Numeric(5, 4), default=0.2000, server_default="0.2000", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | daily_loss_limit | Column(Numeric(5, 4), default=0.0500, server_default="0.0500", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | min_commission | Column(Numeric(10, 2), default=5, server_default="5", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | buy_commission_rate | Column(Numeric(8, 6), default=0.000250, server_default="0.000250", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | sell_commission_rate | Column(Numeric(8, 6), default=0.000250, server_default="0.000250", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | stamp_duty_rate | Column(Numeric(8, 6), default=0.000500, server_default="0.000500", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/portfolio_strategy.py | slippage | Column(Numeric(8, 6), default=0, server_default="0", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | capital_ratio | Column(Numeric(5, 4), default=0.6000, server_default="0.6000", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | max_positions | Column(Integer, default=5, server_default="5", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | single_open_ratio | Column(Numeric(5, 4), default=0.1000, server_default="0.1000", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | stop_loss_ratio | Column(Numeric(5, 4), default=0.0500, server_default="0.0500", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | take_profit_ratio | Column(Numeric(5, 4), default=0.1500, server_default="0.1500", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | trailing_stop_ratio | Column(Numeric(5, 4), default=0.0300, server_default="0.0300", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | add_position_threshold | Column(Numeric(5, 4), default=0.0500, server_default="0.0500", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | max_add_count | Column(Integer, default=2, server_default="2", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | add_position_ratio | Column(Numeric(5, 4), default=0.1000, server_default="0.1000", nullable=False) | - | 待判 | 待判 |
| D:/project/tq-iquant-web/main/core/models/strategy.py | reduce_position_ratio | Column(Numeric(5, 4), default=0.3000, server_default="0.3000", nullable=False) | - | 待判 | 待判 |
