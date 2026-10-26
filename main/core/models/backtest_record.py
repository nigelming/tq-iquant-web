from sqlalchemy import Column, Integer, String, Date, DateTime, Text, ForeignKey, Boolean, Numeric
from sqlalchemy.sql import func, text

from .base import Base


class BacktestRecord(Base):
    __tablename__ = "backtest_records"

    id = Column(Integer, primary_key=True)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(10), nullable=False)
    error_message = Column(Text, nullable=True)
    params_snapshot = Column(Text, nullable=True)
    # 信号预检警告（JSON list[str]）：公式报错/输出线缺失/区间内零信号等
    # 非致命问题——回测仍 completed，但前端详情页置顶提示"为什么没有交易"。
    warnings_json = Column(Text, nullable=True)
    # 验证结论（JSON dict）：容量健康(自动预警) + 危机防御机制(固定窗口逐窗判定)
    # + 样本外引用(该组合最近 walk-forward 摘要)。回测完成时冻结，随结果审计。
    validation_json = Column(Text, nullable=True)
    # B6 考核口径（默认 absolute 绝对收益）：absolute=绝对收益+回撤+卡玛；
    # relative=相对收益(IR/超额/下行捕获)。详情 API 据此出 acceptance 验收块。
    evaluation_profile = Column(String(10), nullable=False, default="absolute", server_default="absolute")
    # C7 IS/OOS 采纳门槛（默认关）：启用后回测完成时按稳健性检验 oos 块评估
    # OOS CAGR ≥ oos_gate_min_ratio × IS CAGR 且 OOS 不跑输基准。
    # gate_status: None=未启用/未评估，passed/rejected；gate_reasons: JSON list[str] 人读原因。
    # rejected 仅标记不删除记录（审计友好）。
    is_oos_gate_enabled = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    oos_gate_min_ratio = Column(Numeric(4, 2), nullable=False, default=0.5, server_default="0.50")
    gate_status = Column(String(10), nullable=True)
    gate_reasons = Column(Text, nullable=True)
    # C8 walk-forward 子回测关联：非空表示该记录是某次 walk-forward run 的窗口子回测
    # （列表接口默认过滤，仅 run 详情内可见）；空 = 普通回测（历史行为）。
    walk_forward_run_id = Column(Integer, ForeignKey("walk_forward_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    # P2b 参数微扰子回测关联：非空表示该记录是某次 param-perturb run 的扰动/基准
    # 子回测（列表接口默认过滤，仅 run 详情内可见）；空 = 普通回测（历史行为）。
    param_perturb_run_id = Column(Integer, ForeignKey("param_perturb_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
