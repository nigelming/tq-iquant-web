# -*- coding: utf-8 -*-
"""回写存量决策事件 message 的统一精度（默认 dry-run）。

背景
----
2026-10-11 22:37 前的回测/实盘记录，决策事件 message 内嵌的
价格/金额/百分比为原始浮点（如「最高 7.26000000」），显示不统一。
本脚本用 core.services.decision_message_backfill.reformat_message()
按闸门模板解析并重排为 fmt_*（价格 3 位、金额 2 位+千分位、百分比 2 位）。

仅改写 message 文本，不影响回测结果/事件结构。默认 dry-run 只打印统计
与抽样对比；--apply 才写库。
用法
----
    cd main
    uv run python scripts/backfill_decision_messages.py --record 24
    uv run python scripts/backfill_decision_messages.py --all
    uv run python scripts/backfill_decision_messages.py --all --apply
"""
import argparse
import sys
from pathlib import Path

_MAIN = Path(__file__).resolve().parent.parent
if str(_MAIN) not in sys.path:
    sys.path.insert(0, str(_MAIN))

from core.db import SessionLocal                                        # noqa: E402
from core.models import BacktestDecisionEvent, LiveDecisionEvent        # noqa: E402
from core.services.decision_message_backfill import reformat_message    # noqa: E402

_GATES = ("stop_loss", "take_profit", "trailing_stop", "daily_loss", "order_shrunk")


def _iter_rows(db, model, scope_id):
    q = db.query(model)
    if scope_id:
        fk = "backtest_record_id" if model is BacktestDecisionEvent else "live_session_id"
        q = q.filter(getattr(model, fk) == scope_id)
    return q.all()


def main():
    p = argparse.ArgumentParser(
        description="回写存量决策事件 message 精度（默认 dry-run）")
    p.add_argument("--record", type=int, help="只处理指定记录 id（回测）")
    p.add_argument("--session", type=int, help="只处理指定 session id（实盘）")
    p.add_argument("--all", action="store_true", help="处理全部")
    p.add_argument("--apply", action="store_true", help="真正写库（缺省只打印对比）")
    args = p.parse_args()
    if not (args.record or args.session or args.all):
        p.print_help()
        return 2

    db = SessionLocal()
    try:
        targets = []
        if args.record or args.all:
            targets.append((BacktestDecisionEvent, args.record))
        if args.session or args.all:
            targets.append((LiveDecisionEvent, args.session))

        changed = unchanged = 0
        samples = []
        for model, scope in targets:
            for ev in _iter_rows(db, model, scope):
                if ev.gate not in _GATES:
                    continue
                new = reformat_message(ev.gate, ev.message)
                if new is None or new == ev.message:
                    unchanged += 1
                    continue
                changed += 1
                if len(samples) < 8:
                    samples.append((model.__tablename__, ev.gate, ev.message, new))
                if args.apply:
                    ev.message = new
        if args.apply:
            db.commit()
        print("matched: %d  已回写: %d  无需改动: %d" % (changed + unchanged, changed, unchanged))
        if not args.apply:
            for tbl, gate, old, new in samples:
                print("  [%s] %s\n    OLD: %s\n    NEW: %s" % (tbl, gate, old, new))
            print("\n[dry-run] 未写库。确认无误后加 --apply 重跑。")
        else:
            print("已写库（--apply）。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
