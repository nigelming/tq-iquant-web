from sqlalchemy import Column, Integer, String, ForeignKey

from .base import Base


class FormulaSignal(Base):
    __tablename__ = "formula_signals"

    id = Column(Integer, primary_key=True)
    formula_id = Column(Integer, ForeignKey("formulas.id", ondelete="CASCADE"), nullable=False)
    signal_name = Column(String(50), nullable=False)
    signal_type = Column(String(10), nullable=False)
    trigger_value = Column(Integer, nullable=False)
