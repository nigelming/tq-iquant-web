# -*- coding: utf-8 -*-
"""回填老回测记录的每日持仓明细（positions_json），并可选择重算市场状态分析。

背景
----
持仓明细落库功能在提交 b7bc21c（2026-09-27）才落地，当前库中所有回测记录
的组合快照 positions_json 均为 NULL（早于该功能），导致「机器学习alpha +
栏目八」二节（行业归因）对所有记录显示「需重新回测」。重跑长周期回测代价过高，
用成交流水 + 存量市值 + 事后可取的前复权价即可重建持仓明细。

判定标准
--------
[OK]   status=ok 且 written=True：现金重放逐日与库中 cash 自洽（零漂移校验），
       且聚合偏离 |k-1| <= 5%
[SKIP] status=skipped：|k-1| > 5%，重建市值与存量市值偏离过大，未写入库
       （事后复权价与回测当时有除权漂移；确认无误可用 --force 覆盖）
[FAIL] status=failed：现金不自洽（重放数量或时序有误），一行未写
默认 dry-run：只打印对账表，不写库。--apply 才真正写入。
用法
----
    cd main
    uv run python scripts/backfill_positions_json.py --all
    uv run python scripts/backfill_positions_json.py --record 11
    uv run python scripts/backfill_positions_json.py --all --apply
    uv run python scripts/backfill_positions_json.py --all --apply --recompute
    uv run python scripts/backfill_positions_json.py --all --apply --force

注意：脚本须 import core.*，故自行把 main/ 插入 sys.path（main/scripts/ 与
其余脚本不 import core.*，无此先例）。取价依赖通达信客户端在运行。
"""
import argparse
import sys
from pathlib import Path

_MAIN = Path(__file__).resolve().parent.parent
if str(_MAIN) not in sys.path:
    sys.path.insert(0, str(_MAIN))

from core.db import SessionLocal                                  # noqa: E402
from core.models import (BacktestMarketRegimeAnalysis,            # noqa: E402
                         BacktestRecord)
from core.services.backtest_service import recompute_market_regime  # noqa: E402
from core.services.positions_backfill import backfill_record        # noqa: E402

_TAG = {"ok": "[OK]", "skipped": "[SKIP]", "failed": "[FAIL]"}


def _section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _print_report(r):
    if "cash_check" not in r:
        print("[%s] record %s: %s"
              % (_TAG.get(r.get("status"), "[?]"), r.get("record_id"),
                 r.get("error")))
        return
    if "scale" not in r:
        print("[FAIL] record %s  现金校验 ok=%s 不符 %s 天  说明：%s"
              % (r["record_id"], r["cash_check"]["ok"],
                 r["cash_check"]["mismatches"], r.get("error")))
        return
    cc, sc = r["cash_check"], r["scale"]
    print("%s record %s  快照 %s  持仓代码 %s  已写入 %s"
          % (_TAG[r["status"]], r["record_id"], r["snapshots"],
             r["held_codes"], r["written"]))
    print("       现金校验 ok=%s 不符 %s 天%s"
          % (cc["ok"], cc["mismatches"],
             "" if cc["first_mismatch"] is None
             else "（首日：%s）" % cc["first_mismatch"]))
    print("       缩放 |k-1| 均值 %s 最大 %s（%s）未缩放 %s 天"
          % (sc["mean_dev"][:8], sc["max_dev"][:8], sc["max_dev_date"],
             sc["no_scale_days"]))
    print("       结转 %s 天  avg_cost 兜底 %s 次  缺价代码 %s"
          % (r["carry_forward_days"], r["avg_cost_fallback"],
             r["missing_price_codes"] or "无"))
    if r.get("error"):
        print("       说明：%s" % r["error"])


def _target_ids(db, args):
    q = db.query(BacktestRecord).order_by(BacktestRecord.id)
    if args.record:
        return [r.id for r in q.filter(BacktestRecord.id == args.record).all()]
    # --all 与 --all-including-done 等价：处理全部记录。
    # 用户选定「全部 10 条」，不再按 market_regime 结果过滤。
    return [r.id for r in q.all()]


def main():
    p = argparse.ArgumentParser(
        description="回填老回测记录的 positions_json（默认 dry-run）")
    p.add_argument("--record", type=int, help="只处理指定记录 id")
    p.add_argument("--all", action="store_true",
                   help="处理全部记录（含已有市场态结果的）")
    p.add_argument("--all-including-done", action="store_true",
                   help="（兼容别名，等价于 --all）")
    p.add_argument("--apply", action="store_true",
                   help="真正写库（缺省只打印对账表）")
    p.add_argument("--force", action="store_true",
                   help="缩放偏离超 5%% 时仍写入")
    p.add_argument("--recompute", action="store_true",
                   help="回填后逐条重算市场态分析（需通达信在运行，较慢）")
    args = p.parse_args()
    if not (args.record or args.all or args.all_including_done):
        p.print_help()
        return 2

    db = SessionLocal()
    try:
        ids = _target_ids(db, args)
        if not ids:
            print("没有符合条件的记录")
            return 0
        _section("目标记录：%s" % ids)
        reports = []
        for rid in ids:
            rec = db.get(BacktestRecord, rid)
            _section("record %s  %s ~ %s" % (rid, rec.start_date, rec.end_date))
            r = backfill_record(db, rid, force=args.force, apply=args.apply)
            _print_report(r)
            reports.append((rid, r))

        _section("汇总")
        for status in ("ok", "skipped", "failed"):
            hit = [rid for rid, r in reports if r["status"] == status]
            print("%-8s %s" % (status, hit or "无"))
        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply 重跑。")
            return 0

        if args.recompute:
            _section("重算市场态分析（栏目八 行业归因）")
            for rid, r in reports:
                if r["status"] != "ok" or not r["written"]:
                    print("[SKIP] record %s 未写入，跳过重算" % rid)
                    continue
                out = recompute_market_regime(db, rid)
                status = (out or {}).get("status")
                print("[%s] record %s 市场态重算：%s %s"
                      % ("OK" if status == "completed" else "FAIL", rid,
                         status, (out or {}).get("error") or ""))
        return 0 if all(r["status"] == "ok" for _, r in reports) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
