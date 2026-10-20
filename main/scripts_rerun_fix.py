import sys
from datetime import date
sys.path.insert(0, r'D:\project\tq-iquant-web\main')
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.services import backtest_service
from core.models.portfolio_strategy import PortfolioStrategy
engine = create_engine('sqlite:///data/dev.db')
Session = sessionmaker(bind=engine)
db = Session()
ps = db.get(PortfolioStrategy, 12)
req = type('R', (), {
  'portfolio_strategy_id': 12,
  'name': '29-rerun3-dailyfix',
  'start_date': date(2026,1,1),
  'end_date': date(2026,10,10),
})()
res = backtest_service.run_backtest(db, ps, req)
print(res['record_id'])
db.close()
