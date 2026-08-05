"""iQuant 行情可达性验证 — 验证 xtquant.xtdata 能否在独立进程拿 tick/1m bar。

前置条件：iQuant 客户端在运行（行情服务监听 127.0.0.1:58610）。
用 live 环境（Python 3.7）跑，xtquant 的 pyd 是 cp37。

验证点：
  A. xtquant.xtdata import + get_client 自动连 58610
  B. get_full_tick(['000001.SZ']) 真连接拉盘口 tick，看字段结构
  C. subscribe_quote(period='1m') 订阅 1m bar，回调 datas 结构 + 是否真推
  D. subscribe_quote(period='tick') 订阅分笔，回调频率 + 结构
  E. get_market_data_ex(period='1m', count=5) 拉历史 1m，看是否当天有数据（对照 TDX 盘中只有昨天）

用法（在 live/ 目录）：
  uv run python scripts/verify_iquant_quote.py            # 默认全量 A-E
  uv run python scripts/verify_iquant_quote.py --only B   # 只跑某项

注意：C/D 会真实订阅，订阅号用完自动 unsubscribe。盘中跑才有推送；盘外可能只拉到快照。
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

# 注入 iQuant 的 xtquant
XTQUANT_PATH = r"D:/iQuant/bin.x64/Lib/site-packages"
if XTQUANT_PATH not in sys.path:
    sys.path.insert(0, XTQUANT_PATH)

# 交易端认证配置（iQuant 行情需交易端登录后才有权限）
#   userdata_mini 路径：iQuant 行情共享内存所在目录
#   account：资金账号（股票账号）
# 优先级：CLI --account > 环境变量 IQUANT_ACCOUNT > 空（不认证）
USERDATA_MINI_PATH = r"D:/iQuant/userdata_mini"
AUTH_SESSION = 888888  # 任务 session id（任意 int，避开客户端占用的 123456）


def _import_xtdata():
    try:
        from xtquant import xtdata
        return xtdata, None
    except Exception as e:
        import traceback
        return None, traceback.format_exc()


def authenticate_trade(account):
    """交易端登录认证：行情服务要求交易端先认证。

    链路：XtQuantTrader(userdata_mini, session).start() → .connect() → .subscribe(StockAccount(account))。
    认证成功后 xtdata.get_full_tick / subscribe_quote 才有权限返回数据。

    返回 (trader, result) 或 (None, err)。result=0 表示连接成功。
    """
    if not account:
        print("[AUTH] 未提供账号，跳过交易端认证（行情接口可能返回 not authenticated）")
        return None, "no account"
    print("=" * 64)
    print("0. 交易端认证（XtQuantTrader 登录）")
    print("=" * 64)
    try:
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount
    except Exception as e:
        print(f"[FAIL] import xttrader/xttype 失败：{e}")
        return None, str(e)

    print(f"     userdata_mini: {USERDATA_MINI_PATH}")
    print(f"     account: {account}")
    if not Path(USERDATA_MINI_PATH).exists():
        print(f"[WARN] userdata_mini 路径不存在：{USERDATA_MINI_PATH}")

    try:
        trader = XtQuantTrader(USERDATA_MINI_PATH, AUTH_SESSION)
        trader.start()
        result = trader.connect()  # 0=成功
        print(f"[{'OK' if result == 0 else 'WARN'}] trader.connect() = {result}")
        if result != 0:
            print("       → 连接失败，确认 iQuant 客户端在运行（XtItClient.exe 监听 58600）")
            return trader, result
        acc = StockAccount(account)
        trader.subscribe(acc)
        print(f"[OK] trader.subscribe(StockAccount({account})) 完成")
        # 给行情服务一点时间同步认证状态
        time.sleep(2)
        return trader, result
    except Exception as e:
        print(f"[FAIL] 认证异常：{e}")
        return None, str(e)


def probe_import_and_connect():
    print("=" * 64)
    print("A. xtquant.xtdata import + 自动连接 58610")
    print("=" * 64)
    xtdata, err = _import_xtdata()
    if xtdata is None:
        print(f"[FAIL] import 失败：\n{err}")
        return None
    print("[OK] from xtquant import xtdata 成功")
    for fn in ("subscribe_quote", "unsubscribe_quote", "get_full_tick",
               "get_market_data_ex", "run", "subscribe_whole_quote"):
        print(f"     {fn}: {hasattr(xtdata, fn)}")
    # get_client 自动连
    try:
        client = xtdata.get_client()
        connected = client.is_connected()
        print(f"[{'OK' if connected else 'WARN'}] get_client().is_connected() = {connected}")
        if not connected:
            print("       → 58610 未连上，确认 iQuant 客户端在运行")
            return None
    except Exception as e:
        print(f"[FAIL] get_client 异常：{e}")
        return None
    return xtdata


def probe_full_tick(xtdata):
    print("\n" + "=" * 64)
    print("B. get_full_tick 盘口 tick 字段结构")
    print("=" * 64)
    codes = ["000001.SZ", "600000.SH"]
    try:
        res = xtdata.get_full_tick(codes)
    except Exception as e:
        print(f"[FAIL] get_full_tick 异常：{e}")
        return
    if not res:
        print("[WARN] 返回空 —— 可能非交易时段或未接行情")
        return
    print(f"[OK] 返回 {len(res)} 只股票")
    for code, snap in res.items():
        print(f"\n  {code}:")
        if isinstance(snap, dict):
            for k, v in snap.items():
                vs = v if not isinstance(v, (list,)) else (v[:3] + ['...'] if len(v) > 3 else v)
                print(f"     {k} = {vs}")
        else:
            print(f"     type={type(snap).__name__}: {str(snap)[:200]}")
    # 关键：有没有 lastPrice / askPrice1 / bidPrice1（真 tick 字段）
    sample = next(iter(res.values()), {}) if isinstance(res, dict) else {}
    if isinstance(sample, dict):
        tick_keys = [k for k in sample.keys() if k in (
            "lastPrice", "askPrice1", "bidPrice1", "volume", "amount", "open", "high", "low"
        )]
        print(f"\n  [含真 tick 字段]：{tick_keys}")
        print("  → get_full_tick 给的是盘口快照（lastPrice+五档），是单点读，非序列")


def probe_subscribe(xtdata, period, label, max_wait=30, max_events=5):
    print(f"\n" + "=" * 64)
    print(f"{label}. subscribe_quote(period='{period}') 实推验证")
    print("=" * 64)
    captured = {"events": [], "done": False}

    def on_quote(datas):
        if captured["done"]:
            return
        captured["events"].append(datas)
        n = len(captured["events"])
        # 打印前 2 条完整结构
        if n <= 2:
            print(f"  [回调 #{n}] type={type(datas).__name__}")
            if isinstance(datas, dict):
                for k, v in list(datas.items())[:3]:
                    print(f"     {k} → {type(v).__name__}: {str(v)[:300]}")
        else:
            print(f"  [回调 #{n}] ...")

    try:
        seq = xtdata.subscribe_quote("000001.SZ", period=period, callback=on_quote)
        print(f"[OK] subscribe_quote 返回订阅号 seq={seq}")
    except Exception as e:
        print(f"[FAIL] subscribe_quote 异常：{e}")
        return

    # 在后台线程跑 run() 接收回调（run 阻塞）
    runner = threading.Thread(target=xtdata.run, daemon=True)
    runner.start()
    print(f"     等待回调推送（最多 {max_wait} 秒，目标 {max_events} 条）...")

    for _ in range(max_wait):
        if len(captured["events"]) >= max_events:
            break
        time.sleep(1)
    captured["done"] = True

    try:
        xtdata.unsubscribe_quote(seq)
        print(f"[OK] unsubscribe_quote({seq})")
    except Exception as e:
        print(f"[WARN] unsubscribe 异常：{e}")

    n = len(captured["events"])
    print(f"\n[结论] period='{period}' 共收到 {n} 次回调")
    if n == 0:
        print("       → 无推送：可能非交易时段，或 period 不支持，或需 level-2 权限")
    else:
        ev0 = captured["events"][0]
        if isinstance(ev0, dict):
            print(f"       回调顶层 keys：{list(ev0.keys())}")
            v0 = next(iter(ev0.values()))
            if isinstance(v0, list) and v0:
                row0 = v0[0]
                print(f"       data[0] 类型：{type(row0).__name__}")
                if hasattr(row0, "dtype"):
                    print(f"       data[0] 是 numpy 结构化数组，dtype.names：{list(row0.dtype.names) if hasattr(row0,'dtype') else '?'}")
                elif isinstance(row0, dict):
                    print(f"       data[0] keys：{list(row0.keys())}")
                else:
                    print(f"       data[0]：{str(row0)[:200]}")
            elif hasattr(v0, "columns"):
                print(f"       data 是 DataFrame，columns：{list(v0.columns)}")
        print(f"       → period='{period}' 可真推送，datas 结构见上")


def probe_history_1m(xtdata):
    print("\n" + "=" * 64)
    print("E. get_market_data_ex(period='1m', count=5) 历史数据对照")
    print("=" * 64)
    try:
        res = xtdata.get_market_data_ex(
            field_list=["time", "open", "high", "low", "close", "volume"],
            stock_list=["000001.SZ"], period="1m", count=5,
        )
    except Exception as e:
        print(f"[FAIL] get_market_data_ex 异常：{e}")
        return
    if not res:
        print("[WARN] 返回空")
        return
    df = res.get("000001.SZ")
    print(f"[OK] 返回，type={type(df).__name__}")
    if df is None:
        return
    if hasattr(df, "index"):
        print(f"     index 最后 5：{list(df.index)[-5:]}")
        print(f"     当前时间：{time.strftime('%Y%m%d %H:%M:%S')}")
        print(f"     columns：{list(df.columns) if hasattr(df,'columns') else 'N/A'}")
        print(f"     → 对比 TDX：盘中 1m 最新 bar 是否是今天（TDX 只有昨天）")
    else:
        print(f"     值：{str(df)[:300]}")


def main():
    parser = argparse.ArgumentParser(description="iQuant 行情可达性验证")
    parser.add_argument("--only", choices=["A", "B", "C", "D", "E"], help="只跑某一项")
    parser.add_argument("--account", default=os.environ.get("IQUANT_ACCOUNT", ""),
                        help="iQuant 资金账号（交易端认证用）；默认读环境变量 IQUANT_ACCOUNT")
    parser.add_argument("--skip-auth", action="store_true", help="跳过交易端认证（仅测连接，预期 not authenticated）")
    args = parser.parse_args()

    xtdata = None
    if args.only in (None, "A"):
        xtdata = probe_import_and_connect()
        if xtdata is None:
            print("\n[A 失败] 无法连接 iQuant 行情服务，后续跳过。确认 iQuant 客户端在运行。")
            return
    else:
        xtdata, err = _import_xtdata()
        if xtdata is None:
            print(f"import 失败：{err}")
            return

    # 交易端认证（B/C/D 需要认证后才有数据；A/E 不强依赖）
    trader = None
    if not args.skip_auth and args.only in (None, "B", "C", "D"):
        trader, auth_result = authenticate_trade(args.account)

    try:
        if args.only in (None, "B"):
            probe_full_tick(xtdata)
        if args.only in (None, "C"):
            probe_subscribe(xtdata, period="1m", label="C", max_wait=30, max_events=3)
        if args.only in (None, "D"):
            probe_subscribe(xtdata, period="tick", label="D", max_wait=20, max_events=5)
        if args.only in (None, "E"):
            probe_history_1m(xtdata)
    finally:
        if trader is not None:
            try:
                trader.stop()
                print("[OK] trader.stop 完成")
            except Exception:
                pass

    print("\n验证完成。")


if __name__ == "__main__":
    main()
