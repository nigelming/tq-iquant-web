# -*- coding: utf-8 -*-
"""验证桥 /quote 能否拉到「前几天」历史 bar（不只当天实时）。

实盘公式注入需要足够长的历史 bar 序列（均线公式要 N 根才算得稳）。
桥 /quote 底层 xtdata.get_market_data_ex(count=N) 配合 download_history_data
（HISTORY_DAYS=30）理论能拉 30 天历史，但需实机验证盘中是否真能返回前几天数据。

判定：
  - 1m 拉 count=1200（约 5 交易日）→ 看最早 bar 时间是否在前几天（非今天）
  - 5m 拉 count=240（约 5 交易日）→ 同上
  - 1w/1mon：周线/月线 bar 稀疏，核心是「能否从 xtdata 远程分支拉到任何 bar」
    （Q4：1w/1mon 不在 xtdata 本地读取白名单，走 get_market_data_ex_ori 远程分支，
     能否拉到未真机验过）。拉到 ≥2 根即判定 PASS（证明远程分支可用）。
  - 最早 bar 时间 < 今天 → 桥能拉历史，实盘公式注入数据充足
  - 最早 bar 时间 = 今天 → 桥只能拉当天，实盘公式注入数据不足，需另想办法

用法（需 iQuant 桥在跑 + 客户端登录）：
  cd main
  uv run python scripts/verify_quote_history.py
  uv run python scripts/verify_quote_history.py --code 600000.SH --count1m 1200 --count5m 240
  uv run python scripts/verify_quote_history.py --periods 1w,1mon --counts 200,200
  uv run python scripts/verify_quote_history.py --periods 1m,5m,15m,30m,1h,1d,1w,1mon
"""
import argparse
import sys
from datetime import datetime


def _parse_bar_time(b):
    """从 bar dict 解析时间字段，返回 datetime 或 None。兼容 index/stime/time/Time。"""
    t_raw = b.get("index") or b.get("stime") or b.get("time") or b.get("Time")
    if t_raw is None:
        return None
    s = str(t_raw).strip()
    try:
        if len(s) >= 14 and s[:14].isdigit():
            return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], "%Y%m%d")
        ts = float(s)
        if ts > 1e12:
            ts = ts / 1000
        from datetime import timezone, timedelta
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).replace(tzinfo=None)
    except Exception:
        return None


def _probe_period(base_url, code, period, count):
    """对单周期调桥 /quote，返回 (ok, bars_len, t_min, t_max, msg)。"""
    try:
        import httpx
    except ImportError:
        return False, 0, None, None, "需 httpx：uv run python scripts/verify_quote_history.py"

    try:
        r = httpx.get(
            base_url + "/quote",
            params={"code": code, "period": period, "count": count},
            timeout=30,
        )
    except Exception as e:
        return False, 0, None, None, "/quote 请求异常：%s" % e
    if r.status_code != 200:
        return False, 0, None, None, "/quote HTTP %s" % r.status_code
    body = r.json() or {}
    if not body.get("ok"):
        return False, 0, None, None, "/quote 返回 ok=False：%s" % body.get("error")
    data = body.get("data") or {}
    bars = data.get(code, [])
    if not bars:
        return False, 0, None, None, "/quote 返回空 bar 列表"

    times = [_parse_bar_time(b) for b in bars]
    times = [t for t in times if t is not None]
    if not times:
        return False, len(bars), None, None, "无法解析任何 bar 时间字段，原始首 bar：%s" % bars[0]
    t_min, t_max = min(times), max(times)
    return True, len(bars), t_min, t_max, "OK"


# 长周期：bar 稀疏，核心是「远程分支能否拉到」，拉到 ≥2 根即判通过。
_LONG_PERIODS = {"1w", "1mon", "1q", "1y"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600000.SH")
    ap.add_argument("--count1m", type=int, default=1200, help="1m 拉取根数（1200≈5交易日）")
    ap.add_argument("--count5m", type=int, default=240, help="5m 拉取根数（240≈5交易日）")
    ap.add_argument(
        "--periods",
        default="",
        help="逗号分隔周期列表，如 1w,1mon。指定后覆盖默认 1m/5m，需配 --counts",
    )
    ap.add_argument(
        "--counts",
        default="",
        help="逗号分隔 count 列表，与 --periods 一一对应，如 200,200",
    )
    ap.add_argument("--base_url", default="http://127.0.0.1:8790")
    args = ap.parse_args()

    try:
        import httpx  # noqa: F401
    except ImportError:
        print("[FAIL] 需 httpx：uv run python scripts/verify_quote_history.py")
        return 1

    print("=" * 70)
    print("桥 /quote 历史拉取能力验证 — %s" % args.code)
    print("=" * 70)
    print("当前时间：%s" % datetime.now())

    # 先 ping 桥
    try:
        r = httpx.get(args.base_url + "/ping", timeout=5)
        if r.status_code != 200 or not (r.json() or {}).get("ok"):
            print("[FAIL] 桥 /ping 不通：HTTP %s body=%s" % (r.status_code, r.text[:200]))
            print("       确认 iQuant 桥在跑（127.0.0.1:8790）+ 客户端登录")
            return 1
        print("[OK] 桥 /ping 正常")
    except Exception as e:
        print("[FAIL] 连不上桥：", e)
        print("       确认 iQuant 桥在跑（127.0.0.1:8790）+ 客户端登录")
        return 1

    # 构造 (period, count) 列表
    if args.periods:
        periods = [p.strip() for p in args.periods.split(",") if p.strip()]
        counts = [int(c.strip()) for c in args.counts.split(",") if c.strip()]
        if not counts or len(counts) != len(periods):
            print("[FAIL] --periods 与 --counts 数量不一致（periods=%d counts=%d）"
                  % (len(periods), len(counts)))
            return 1
        plan = list(zip(periods, counts))
    else:
        plan = [("1m", args.count1m), ("5m", args.count5m)]
    print("验证周期与 count：%s" % ", ".join("%s=%d" % (p, c) for p, c in plan))

    today = datetime.now().date()
    overall = True
    results = {}
    for period, count in plan:
        print("\n--- %s count=%d ---" % (period, count))
        ok, n_bars, t_min, t_max, msg = _probe_period(args.base_url, args.code, period, count)
        if not ok:
            print("[FAIL] %s" % msg)
            results[period] = False
            overall = False
            continue
        print("[OK] 返回 %d 根 bar" % n_bars)
        span_days = (t_max.date() - t_min.date()).days
        print("  最早 bar：%s" % t_min)
        print("  最晚 bar：%s" % t_max)
        print("  跨度：%d 天" % span_days)
        # 首 3 bar 示例
        print("  首 3 bar 示例：")
        # 重新拉一次拿首 3 太浪费，从 msg 里没法取；这里仅打印时间范围已足够。
        # 长周期判定：bar 稀疏，拉到 ≥2 根即判通过（远程分支可用）。
        if period in _LONG_PERIODS:
            if n_bars >= 2:
                print("  [长周期判定] 拉到 %d 根 ≥2 → 远程分支可用" % n_bars)
                results[period] = True
            else:
                print("  [长周期判定] 仅拉到 %d 根 <2 → 远程分支疑似无数据" % n_bars)
                results[period] = False
                overall = False
            continue
        # 短周期判定：最早 bar 是否在前几天
        has_history = t_min.date() < today
        print("  最早 bar 是否在前几天（< 今天）：%s"
              % ("是 → 能拉历史" if has_history else "否 → 仅当天"))
        if not has_history:
            results[period] = False
            overall = False
        else:
            results[period] = True

    print("\n" + "=" * 70)
    print("结论：")
    for period, ok in results.items():
        print("  %s: %s" % (period, "PASS" if ok else "FAIL"))
    if overall:
        print("\n[总判定 PASS] 桥 /quote 历史拉取满足要求")
        if any(p in _LONG_PERIODS for p, _ in plan):
            print("  → 1w/1mon 远程分支可用，可纳入 VALID_PERIODS（回测+实盘同步放开到 8 周期）")
        else:
            print("  → 实盘可用 query_quote(count=200~1200) 拉历史 + 实时，注入算公式")
    else:
        print("\n[总判定 未通过] 见上方明细")
        print("  - 若短周期仅当天：需 (a) 增大 count (b) 检查 download_history_data")
        print("  - 若 1w/1mon 空/仅 1 根：xtdata 远程分支对该周期无数据，暂不放行")
    return 0 if overall else 2


if __name__ == "__main__":
    sys.exit(main())
