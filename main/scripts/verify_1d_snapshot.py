# -*- coding: utf-8 -*-
"""验证桥 /quote 拉到的 1d 最新 bar 是否为当天盘中实时快照。

背景：1d 在 xtdata 本地白名单内能拉到数据，但日线 bar 时间戳约定只标到
日（00:00:00）。需区分：
  (a) 最晚 1d bar 是「今日盘中实时快照」——日期=今天，Close 随盘中价变
  (b) 最晚 1d bar 是「昨日已收盘日线」——日期=昨天，Close 固定不变

实盘日线公式注入依赖此判定：若只到昨日，则盘中注入算的是昨日信号，
今日信号要等盘后；若是今日实时快照，盘中即可算今日信号。

判定：
  - 最晚 bar 日期 == 今天，且两次拉取 Close 有变化 → 当天实时快照
  - 最晚 bar 日期 == 今天，Close 不变 → 当天快照但盘中未刷新（或盘外）
  - 最晚 bar 日期 < 今天 → 仅到昨日收盘

用法（需 iQuant 桥在跑 + 客户端登录）：
  cd main
  uv run python scripts/verify_1d_snapshot.py
  uv run python scripts/verify_1d_snapshot.py --code 600000.SH --count 10 --interval 5
"""
import argparse
import sys
import time
from datetime import datetime


def _pull_1d(base_url, code, count):
    """调桥 /quote?period=1d，返回 (ok, bars, err)。"""
    try:
        import httpx
    except ImportError:
        return False, None, "需 httpx"
    try:
        r = httpx.get(
            base_url + "/quote",
            params={"code": code, "period": "1d", "count": count},
            timeout=30,
        )
    except Exception as e:
        return False, None, "/quote 请求异常：%s" % e
    if r.status_code != 200:
        return False, None, "/quote HTTP %s" % r.status_code
    body = r.json() or {}
    if not body.get("ok"):
        return False, None, "/quote ok=False：%s" % body.get("error")
    bars = (body.get("data") or {}).get(code, [])
    if not bars:
        return False, None, "/quote 返回空 bar 列表"
    return True, bars, "OK"


def _bar_summary(b):
    """从 bar dict 取时间 + OHLC，容错字段名（index/time/open/close 小写）。"""
    t_raw = b.get("index") or b.get("stime") or b.get("time") or b.get("Time")
    s = str(t_raw).strip() if t_raw is not None else ""
    # 1d bar 时间戳可能是 yyyymmdd 或 yyyymmddHHMMSS 或 float
    t_str = s
    try:
        if len(s) >= 8 and s[:8].isdigit():
            t_str = s[:8]
    except Exception:
        pass
    return {
        "time": t_str,
        "open": b.get("open") or b.get("Open"),
        "high": b.get("high") or b.get("High"),
        "low": b.get("low") or b.get("Low"),
        "close": b.get("close") or b.get("Close"),
        "volume": b.get("volume") or b.get("Volume"),
        "amount": b.get("amount") or b.get("Amount"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600000.SH")
    ap.add_argument("--count", type=int, default=10, help="拉取根数")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="两次拉取间隔秒数，看最晚 bar Close 是否变化")
    ap.add_argument("--rounds", type=int, default=2, help="拉取轮数")
    ap.add_argument("--base_url", default="http://127.0.0.1:8790")
    args = ap.parse_args()

    try:
        import httpx  # noqa: F401
    except ImportError:
        print("[FAIL] 需 httpx：uv run python scripts/verify_1d_snapshot.py")
        return 1

    print("=" * 70)
    print("1d 当天快照验证 — %s" % args.code)
    print("=" * 70)
    print("当前时间：%s" % datetime.now())

    # ping
    try:
        r = httpx.get(args.base_url + "/ping", timeout=5)
        if r.status_code != 200 or not (r.json() or {}).get("ok"):
            print("[FAIL] 桥 /ping 不通")
            return 1
        print("[OK] 桥 /ping 正常")
    except Exception as e:
        print("[FAIL] 连不上桥：", e)
        return 1

    today_str = datetime.now().strftime("%Y%m%d")
    print("今天日期：%s" % today_str)

    snapshots = []
    for rnd in range(args.rounds):
        if rnd > 0:
            print("\n等待 %.1f 秒后再次拉取..." % args.interval)
            time.sleep(args.interval)
        print("\n--- 第 %d 轮拉取 ---" % (rnd + 1))
        ok, bars, err = _pull_1d(args.base_url, args.code, args.count)
        if not ok:
            print("[FAIL] %s" % err)
            return 2
        print("[OK] 返回 %d 根 1d bar" % len(bars))
        last = _bar_summary(bars[-1])
        prev = _bar_summary(bars[-2]) if len(bars) >= 2 else None
        print("  最晚 bar：%s" % last)
        if prev:
            print("  次晚 bar：%s" % prev)
        snapshots.append(last)

    print("\n" + "=" * 70)
    print("判定：")
    last_bar = snapshots[-1]
    is_today = last_bar["time"].startswith(today_str)
    print("  最晚 bar 日期：%s" % last_bar["time"])
    print("  是否为今天（%s）：%s" % (today_str, "是" if is_today else "否 → 仅到昨日收盘"))

    if len(snapshots) >= 2:
        c0, c1 = snapshots[0]["close"], snapshots[1]["close"]
        changed = c0 != c1
        print("  第1轮 Close=%s  第2轮 Close=%s" % (c0, c1))
        if is_today:
            if changed:
                print("  Close 有变化 → 【当天实时快照】盘中随价刷新")
            else:
                print("  Close 无变化 → 当天快照但盘中未刷新，或非交易时段/已收盘")
        else:
            print("  （最晚 bar 非今天，Close 是否变化不改变判定）")

    print("\n结论：")
    if is_today:
        print("  1d 最新 bar 是【当天】数据。实盘日线公式注入盘中即可拿到今日 bar。")
        if len(snapshots) >= 2 and snapshots[0]["close"] != snapshots[1]["close"]:
            print("  且 Close 盘中变动 → 确认为当天实时快照（非昨日复用）。")
    else:
        print("  1d 最新 bar 仅到【%s】（昨日收盘）。盘中注入算的是昨日信号。" % last_bar["time"])
        print("  今日日线信号需等盘后 1d bar 落盘才有。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
