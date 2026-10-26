from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func

from core.models.base import Base


class ParamPerturbRun(Base):
    """P2b 参数微扰敏感性分析（验证纪律层，新功能，默认不使用）。

    对组合下各子策略的数值参数逐一做 ±perturb_pct 扰动：每参数低/高各跑
    一轮子回测（外加 1 轮基准），子回测经 backtest_records.param_perturb_run_id
    关联（列表接口默认过滤）。summary_json 落基准/各轮指标与最敏感参数。

    扰动值只作用于内存装配（run_backtest param_overrides），不回写
    strategies 表——原参数与默认回测行为完全不变。
    """
    __tablename__ = "param_perturb_runs"

    id = Column(Integer, primary_key=True)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    # 扰动幅度（0, 0.5]，默认 0.10 = ±10%
    perturb_pct = Column(Numeric(4, 2), nullable=False, default=0.10, server_default="0.10")
    status = Column(String(10), nullable=False)  # running/completed/failed
    summary_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
