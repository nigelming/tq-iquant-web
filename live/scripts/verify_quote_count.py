"""iQuant 桥 /quote 拉取根数验证 — 验证 count=250 period=1d 实际能返回多少根。

走桥 HTTP（8790），即引擎实盘真实走的路径（HttpBridgeDispatcher.query_quote）。
桥 /quote 透传 count 给 xtdata.get_market_data_ex(count=count)，不做截断。

验证点：
  A. GET /ping —— 桥是否在线（确认 iQuant 客户端 + 仿真桥策略在跑）
  B. GET /quote?code=&period=1d&count=250 —— 实际返回根数
     - 打印 len(bars)、首尾 stime 跨度、首尾 3 行 OHLCV
  C. 诊断：若 len < 250，区分原因
     - 桥 _fetch_quote 先 download_history_data(code, period, _history_start(days=30), "")
       再 get_market_data_ex(count=250)。HISTORY_DAYS=30 → 本地日线可能只有近 30 个交易日。
     - 判据：len(bars) ≈ 30 且跨度 ≈ 1.5 月 → 桥下载范围限制（本地数据不够），非 count 失效。
              len(bars) == QUOTE_COUNT(10) → count 参数没透传（脚本/桥 bug）。
              len(bars) > 30 但 < 250 → 本地历史就这么多（上市不久 / 数据未补全）。

用法（在 live/ 目录）：
  uv run python scripts/verify_quote_count.py                       # 默认 000001.SZ 1d 250
  uv run python scripts/verify_quote_count.py --code 600000.SH --period 1d --count 250
  uv run python scripts/verify_quote_count.py --port 8791           # 实盘桥

注意：纯标准库，无需 xtquant，不在 iQuant 客户端内跑。前提：桥策略已加载启动、监听端口。
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime


def _get(base_url, path):
    """GET base_url+path，返回 (status, body_dict_or_None, raw_text)。"""
    url = base_url + path
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        return None, None, "[连接失败] %s" % e.reason
    except Exception as e:
        return None, None, "[异常] %s" % e
    try:
        return status, json.loads(raw), raw
    except Exception:
        return status, None, raw


def _bar_time(b):
    """取 bar 时间，返回易读字符串。

    桥 _df_to_bars = df.reset_index().to_dict("records")，xtdata DataFrame 索引列
    被 reset_index 保留为 'index'（字符串 'YYYYMMDDHHMMSS'），另有 'time' 毫秒时间戳。
    优先 'index'（可读），其次 'time'（毫秒戳），兼容 'stime'。
    """
    idx = b.get("index")
    if idx:
        s = str(idx)
        # 'YYYYMMDDHHMMSS' / 'YYYYMMDD' → 加分隔符易读
        if len(s) == 14 and s.isdigit():
            return "%s-%s-%s %s:%s:%s" % (s[:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])
        if len(s) == 8 and s.isdigit():
            return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
        return s
    t = b.get("time")
    if t is not None:
        return _ms_to_str(t)
    s = b.get("stime")
    if s is not None:
        return _ms_to_str(s) if isinstance(s, (int, float)) else str(s)
    return "?"


def _ms_to_str(ms):
    try:
        ts = ms / 1000.0 if ms > 1e12 else float(ms)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms)


def _bar_summary(bars, n=3):
    """打印前 n / 后 n 行 OHLCV 摘要。"""
    if not bars:
        print("     (空)")
        return
    head = bars[:n]
    tail = bars[-n:] if len(bars) > n else []
    print("     前 %d 行：" % len(head))
    for b in head:
        print("       %s  O=%s H=%s L=%s C=%s V=%s" % (
            _bar_time(b),
            b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("volume")))
    if tail:
        print("     后 %d 行：" % len(tail))
        for b in tail:
            print("       %s  O=%s H=%s L=%s C=%s V=%s" % (
                _bar_time(b),
                b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("volume")))


def main():
    p = argparse.ArgumentParser(description="iQuant 桥 /quote 拉取根数验证")
    p.add_argument("--code", default="000001.SZ", help="股票代码（带后缀，默认 000001.SZ）")
    p.add_argument("--period", default="1d", help="K 线周期（默认 1d，可 1m/5m/15m/30m/1h/1d）")
    p.add_argument("--count", type=int, default=250, help="请求根数（默认 250）")
    p.add_argument("--port", type=int, default=8790, help="桥端口（默认 8790 仿真，8791 实盘）")
    args = p.parse_args()

    base_url = "http://127.0.0.1:%d" % args.port

    # ---- A. /ping 桥在线 ----
    print("=" * 64)
    print("A. 桥在线检查  GET /ping")
    print("=" * 64)
    status, body, raw = _get(base_url, "/ping")
    if status is None:
        print("[FAIL] %s" % raw)
        print("       → 桥未启动？确认 iQuant 客户端在运行 + 仿真桥策略已加载启动（监听 %d）" % args.port)
        return 1
    print("[HTTP %d] %s" % (status, json.dumps(body, ensure_ascii=False) if body else raw))
    if not (body and body.get("ok")):
        print("[FAIL] 桥返回 ok=false，后续跳过")
        return 1
    print("[OK] 桥在线")

    # ---- B. /quote count ----
    print("\n" + "=" * 64)
    print("B. 拉取验证  GET /quote?code=%s&period=%s&count=%d" % (args.code, args.period, args.count))
    print("=" * 64)
    path = "/quote?code=%s&period=%s&count=%d" % (args.code, args.period, args.count)
    status, body, raw = _get(base_url, path)
    if status is None:
        print("[FAIL] %s" % raw)
        return 1
    if status != 200:
        print("[HTTP %d] %s" % (status, raw))
        return 1
    if not body or not body.get("ok"):
        print("[FAIL] ok=false  body=%s" % (raw[:500] if raw else "空"))
        return 1

    data = body.get("data") or {}
    bars = data.get(args.code, []) if isinstance(data, dict) else []
    cached = body.get("cached")

    print("[OK] 桥返回 ok=true  cached=%s" % cached)
    print("     请求 count = %d" % args.count)
    print("     实际拿到    = %d 根" % len(bars))

    if bars:
        first_t = _bar_time(bars[0])
        last_t = _bar_time(bars[-1])
        print("     首行时间  = %s" % first_t)
        print("     末行时间  = %s" % last_t)
    _bar_summary(bars)

    # ---- C. 诊断 ----
    print("\n" + "=" * 64)
    print("C. 诊断")
    print("=" * 64)
    got = len(bars)
    want = args.count
    if got == 0:
        print("[WARN] 0 根 —— 非交易时段/本地无该周期数据/代码不存在。")
        print("       1d 历史需 iQuant 客户端已下载过日线（download_history_data 才能取到）。")
    elif got >= want:
        print("[OK] 达到请求根数 %d，count 参数生效，本地数据充足。" % want)
    else:
        # 不足，区分原因
        # 估算跨度（仅对 1d 用日期差）
        try:
            d0 = datetime.strptime(first_t[:10], "%Y-%m-%d")
            d1 = datetime.strptime(last_t[:10], "%Y-%m-%d")
            span_days = (d1 - d0).days
            print("     时间跨度 ≈ %d 天（%s ~ %s）" % (span_days, first_t[:10], last_t[:10]))
            if args.period == "1d":
                # 30 个交易日 ≈ 42 自然日；250 根 ≈ 1 年（≈360 自然日）
                if span_days <= 45 and got <= 35:
                    print("     → 判定：桥 HISTORY_DAYS=30 下载范围限制（本地只下到近 ~30 个交易日）。")
                    print("       count=%d 是生效的（xtdata 想给），但桥 download_history_data 只补了近 %d 天，"
                          % (want, 30))
                    print("       本地日线缓存不足 → xtdata 只能返回已有的。这与「count 上限」无关。")
                    print("       若要拉满 250 根：需扩大 download_history_data 的起始时间（HISTORY_DAYS 调大），")
                    print("       或在 iQuant 客户端先手动补下载对应周期的历史数据。")
                elif got == 10:
                    print("     → 判定：拿到 10 根 = 桥默认 QUOTE_COUNT，count 参数可能没透传！检查脚本/桥。")
                else:
                    print("     → 判定：本地该周期历史就这么多（上市不久 / 客户端未补下载）。")
                    print("       count 参数已生效（返回 %d <= 本地存量），非 count 上限问题。" % got)
            else:
                print("     → 非日线周期，根数不足多半是本地存量不够（合成周期需先下基础周期）。")
        except Exception as e:
            if first_t == "?" or last_t == "?":
                print("     (bar 无可读时间字段，跳过跨度判断)  返回 %d 根 < 请求 %d" % (got, want))
            else:
                print("     (跨度计算跳过：%s)  返回 %d 根 < 请求 %d" % (e, got, want))
            print("     → 多半是本地该周期数据存量不足，count 参数本身已透传。")

    print("\n验证完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
