# -*- coding: gbk -*-
"""
iQuant 盘中实时行情验证 v2（改用 get_market_data_ex）
=====================================================
对应 docs/plans/0009-iquant-http-bridge.md §12 待验证点。

v1 结论（2026-08-05 盘中实测）：
  - get_history_data 已废弃，返回空 {}（官方提示改用 get_market_data_ex + download_history_data）
  - subscribe_quote 注册成功但 60s 无回调 → 订阅推送在 init 阻塞下不可用（V4 定案「拉取模式」）

v2 用官方推荐接口重新验证：
  V1. get_market_data_ex('1m') 盘中实时性：最新 bar 时间戳/close 是否今天、每 10s 是否更新
  V2. 最后一根 1m 是否为「进行中」bar：最新 bar 时间距当前 < 60s
  V3. get_market_data_ex('5m') 数据完整性：5m 是原生基础周期，直接拉看是否有数据
  V4. subscribe_quote 订阅回调（v1 已证不可用，此处保留确认）

关键 API（策略内置环境）：
  download_history_data(code, period, start_time, end_time)   # 补历史，全局函数
  ContextInfo.get_market_data_ex(field, stock_list, period,
                                 start_time='', end_time='', count=0,
                                 dividend_type='none', ...)    # 返回 {code: DataFrame}

用法：同 v1（真实行情模式，内置 Python，交易时段跑 60 秒，贴日志）。
"""
import time

# ================= 可配置 =================
ACCOUNT = "110002348760"      # TODO: 改成你自己的资金账号
CODES = ["600000.SH"]         # 观察股票
POLL_INTERVAL = 10            # 动态验证间隔（秒）
DOWNLOAD_START = "20260801"   # 补历史的起始日（YYYYMMDD）
# ==========================================

_CTX = None
_sub_cb_fired = [0]


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_call(label, fn):
    try:
        r = fn()
        print("[%s] %s => %s" % (_now(), label, str(r)[:500]))
        return r
    except Exception as e:
        print("[%s] %s => FAIL: %s" % (_now(), label, e))
        return None


def _download(period):
    """download_history_data 补充历史（首次或每天开盘前拉一次即可）。"""
    fn = globals().get("download_history_data")
    if fn is None:
        print("    [WARN] download_history_data 不可用，跳过补历史")
        return
    for code in CODES:
        try:
            fn(code, period, DOWNLOAD_START, "")
            print("    download_history_data(%s,%s,%s) ok" % (code, period, DOWNLOAD_START))
        except Exception as e:
            print("    download_history_data(%s,%s) FAIL: %s" % (code, period, e))


def _print_df(df, label):
    """打印 DataFrame 尾部关键信息。"""
    try:
        import pandas as pd
    except Exception:
        pd = None
    print("    %s: type=%s" % (label, type(df).__name__))
    if df is None:
        return
    if hasattr(df, "tail"):
        tail = df.tail(3)
        cols = list(df.columns)
        print("    columns=%s" % (cols[:12]))
        # 尝试打印 time/close 关键列
        for c in ("time", "Time", "datetime", "date"):
            if c in cols:
                print("    %s 列最后 3 个: %s" % (c, list(df[c].tail(3))))
        if "close" in cols:
            print("    close 最后 3 个: %s" % list(df["close"].tail(3)))
        try:
            print("    tail:\n%s" % tail.to_string())
        except Exception:
            pass


def probe_ex(ctx, period, n=10, label=""):
    """用 get_market_data_ex 拉 n 根 bar，打印。"""
    _download(period)
    res = _safe_call(
        "V get_market_data_ex(period='%s',count=%d)" % (period, n),
        lambda: ctx.get_market_data_ex([], CODES, period=period, count=n, dividend_type="none"),
    )
    if not res:
        return None
    for code, df in res.items():
        if hasattr(df, "tail"):
            _print_df(df, code)
        else:
            print("    %s: %s" % (code, str(df)[:300]))
    return res


def _latest_bar_info(res):
    """从 get_market_data_ex 返回里取第一只股票的最新 close + time。"""
    if not res:
        return None, None, None
    for code, df in res.items():
        if not hasattr(df, "tail") or len(df) == 0:
            continue
        last = df.iloc[-1]
        close = last.get("close")
        t = None
        for c in ("time", "Time", "datetime", "date"):
            if c in df.columns:
                t = last[c]
                break
        return code, close, t
    return None, None, None


def verify_static(ctx):
    print("\n========== 静态验证（启动时） ==========")
    probe_ex(ctx, "1m", 10, "V1")
    probe_ex(ctx, "5m", 10, "V3")


def verify_dynamic(ctx, last_close):
    print("\n---------- 动态观察 %s ----------" % _now())
    res = probe_ex(ctx, "1m", 5, "V1")
    code, close, t = _latest_bar_info(res)
    if close is None:
        return last_close
    changed = (last_close is None or close != last_close)
    print("    %s 最新 close=%s  较上次%s" % (code, close, "更新" if changed else "未变"))
    if t is not None:
        ts = None
        try:
            s = str(t)
            if len(s) >= 14 and s.isdigit():            # yyyymmddhhmmss
                import datetime as _dt
                ts = _dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S").timestamp()
            elif len(s) == 13 and s.isdigit():          # 毫秒时间戳
                ts = int(s) / 1000
            elif len(s) == 10 and s.isdigit():          # 秒时间戳
                ts = int(s)
            if ts:
                gap = time.time() - ts
                print("    V2: 最新 bar 时间=%s 距今 %.0f 秒 -> %s" % (
                    time.strftime("%H:%M:%S", time.localtime(ts)), gap,
                    "进行中(<60s)" if gap < 60 else "已完成(>=60s)"))
        except Exception as e:
            print("    V2: 时间解析失败(%s): %s" % (t, e))
    return close


def verify_subscribe(ctx):
    print("\n========== V4: subscribe_quote 订阅回调 ==========")

    def on_quote(datas):
        _sub_cb_fired[0] += 1
        if _sub_cb_fired[0] <= 3:
            print("[%s] [SUB回调 #%d] %s" % (_now(), _sub_cb_fired[0], str(datas)[:200]))

    fn = globals().get("subscribe_quote") or getattr(ctx, "subscribe_quote", None)
    if fn is None:
        print("V4: subscribe_quote 不可用")
        return
    for code in CODES:
        try:
            seq = fn(code, period="1m", callback=on_quote)
            print("V4: subscribe_quote(%s,'1m') 注册成功 seq=%s" % (code, seq))
        except Exception as e:
            print("V4: subscribe_quote 注册失败: %s" % e)


def init(ContextInfo):
    global _CTX
    _CTX = ContextInfo
    try:
        ContextInfo.set_account(ACCOUNT)
    except Exception as e:
        print("[V] set_account failed: %s" % e)

    verify_static(ContextInfo)
    verify_subscribe(ContextInfo)

    last_close = None
    last_t = time.time()
    print("\n========== 进入动态观察（每 %d 秒）==========" % POLL_INTERVAL)
    while True:                                   # init 阻塞，与正式桥同机制
        try:
            if time.time() - last_t >= POLL_INTERVAL:
                last_close = verify_dynamic(ContextInfo, last_close)
                last_t = time.time()
        except Exception as e:
            print("[V] 循环异常: %s" % e)
        time.sleep(1)


def handlebar(ContextInfo):
    pass


# ================= 判定标准 =================
# V1: get_market_data_ex('1m') 最新 bar 是今天且每 10s 更新 → 实时可行
# V2: 最新 bar 距当前 <60s → 进行中（策略只用更早的已完成 bar）
# V3: get_market_data_ex('5m') 有数据 → 5m 可直接拉（原生周期）
# V4: 60s 无 [SUB回调] → 订阅推送不可用，定案拉取模式
# ===========================================
