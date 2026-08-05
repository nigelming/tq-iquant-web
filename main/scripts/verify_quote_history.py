# -*- coding: utf-8 -*-
"""验证桥 /quote 能否拉到「前几天」历史 bar（不只当天实时）。

实盘公式注入需要足够长的历史 bar 序列（均线公式要 N 根才算得稳）。
桥 /quote 底层 xtdata.get_market_data_ex(count=N) 配合 download_history_data
（HISTORY_DAYS=30）理论能拉 30 天历史，但需实机验证盘中是否真能返回前几天数据。

判定：
  - 1m 拉 count=1200（约 5 交易日）→ 看最早 bar 时间是否在前几天（非今天）
  - 5m 拉 count=240（约 5 交易日）→ 同上
  - 最早 bar 时间 < 今天 → 桥能拉历史，实盘公式注入数据充足
  - 最早 bar 时间 = 今天 → 桥只能拉当天，实盘公式注入数据不足，需另想办法

用法（需 iQuant 桥在跑 + 客户端登录）：
  cd main
  uv run python scripts/verify_quote_history.py
  uv run python scripts/verify_quote_history.py --code 600000.SH --count1m 1200 --count5m 240
"""
import argparse
import sys
from datetime import datetime


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600000.SH")
    ap.add_argument("--count1m", type=int, default=1200, help="1m 拉取根数（1200≈5交易日）")
    ap.add_argument("--count5m", type=int, default=240, help="5m 拉取根数（240≈5交易日）")
    ap.add_argument("--base_url", default="http://127.0.0.1:8790")
    args = ap.parse_args()

    try:
        import httpx
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

    today = datetime.now().date()
    overall = True
    for period, count in (("1m", args.count1m), ("5m", args.count5m)):
        print("\n--- %s count=%d ---" % (period, count))
        try:
            r = httpx.get(
                args.base_url + "/quote",
                params={"code": args.code, "period": period, "count": count},
                timeout=30,
            )
        except Exception as e:
            print("[FAIL] /quote 请求异常：", e)
            overall = False
            continue
        if r.status_code != 200:
            print("[FAIL] /quote HTTP %s" % r.status_code)
            overall = False
            continue
        body = r.json() or {}
        if not body.get("ok"):
            print("[FAIL] /quote 返回 ok=False：", body.get("error"))
            overall = False
            continue
        data = body.get("data") or {}
        bars = data.get(args.code, [])
        if not bars:
            print("[FAIL] /quote 返回空 bar 列表")
            overall = False
            continue
        print("[OK] 返回 %d 根 bar" % len(bars))

        # 解析时间字段，找最早/最晚
        times = []
        for b in bars:
            # 字段名兼容：index(stime 等价,bar 结束时间 yyyymmddHHMMSS) / stime / time / Time
            t_raw = b.get("index") or b.get("stime") or b.get("time") or b.get("Time")
            if t_raw is None:
                continue
            s = str(t_raw).strip()
            try:
                if len(s) >= 14 and s[:14].isdigit():
                    times.append(datetime.strptime(s[:14], "%Y%m%d%H%M%S"))
                elif len(s) >= 8 and s[:8].isdigit():
                    times.append(datetime.strptime(s[:8], "%Y%m%d"))
                else:
                    ts = float(s)
                    if ts > 1e12:
                        ts = ts / 1000
                    from datetime import timezone, timedelta
                    times.append(datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).replace(tzinfo=None))
            except Exception:
                continue
        if not times:
            print("[WARN] 无法解析任何 bar 时间字段，原始首 bar：", bars[0])
            overall = False
            continue
        t_min, t_max = min(times), max(times)
        span_days = (t_max.date() - t_min.date()).days
        has_history = t_min.date() < today
        print("  最早 bar：%s" % t_min)
        print("  最晚 bar：%s" % t_max)
        print("  跨度：%d 天" % span_days)
        print("  最早 bar 是否在前几天（< 今天）：%s" % ("是 → 能拉历史" if has_history else "否 → 仅当天"))
        # 打印前 3 个 bar 看字段
        print("  首 3 bar 示例：")
        for b in bars[:3]:
            print("    %s" % b)
        if not has_history:
            overall = False

    print("\n" + "=" * 70)
    if overall:
        print("[总判定 PASS] 桥 /quote 能拉前几天历史 bar，实盘公式注入数据充足")
        print("  → 实盘可用 query_quote(count=200~1200) 拉历史 + 实时，注入算公式")
    else:
        print("[总判定 未通过] 桥 /quote 仅能拉当天 或 拉取失败")
        print("  → 若仅当天：实盘公式注入数据不足，需 (a) 增大 count (b) 检查 download_history_data")
        print("     (c) 或实盘公式改用 TQ 自取 .lc1 历史（盘后落盘的前几天）+ 桥拉当天实时拼接")
    return 0 if overall else 2


if __name__ == "__main__":
    sys.exit(main())
