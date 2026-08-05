"""实盘引擎可行性验证脚本 — 验证 0008 方案两个关键风险点。

阶段1（纯源码探查，不需通达信连接）：
  风险点1 TDX mode 切换：源码已证明 initialize(path) 的 path 是连接标识，
           TDX 目录由 sys.path 注入哪个 tqcenter.py 决定，import 后不可运行时切换。
  风险点2 回调→喂数据→公式闭环：源码已证明 formula_set_data(type=0) 内存注入 +
           formula_format_data 格式化 + formula_process_mul_zb 算公式，非写本地文件。

阶段2（需通达信 live 版运行，抓真实回调格式）：
  风险点3 subscribe_hq 回调 data_str 的真实 JSON 结构（字段名/bar或tick/时间戳）。
  风险点4 formula_set_data + formula_process_mul_zb 在 live 目录下端到端算出信号。

用法：
  阶段1：  uv run python scripts/verify_live_engine.py --stage 1
  阶段2：  uv run python scripts/verify_live_engine.py --stage 2   （需通达信 live 版在跑）

注意：阶段2 会真实 initialize 连接通达信，占 run_id；跑完自动 close。
"""
import argparse
import sys
from pathlib import Path

# 阶段1 用源码静态探查，不 import tqcenter（避免副作用）
STAGE1_ONLY = "--stage" in sys.argv and "2" not in sys.argv


def stage1_source_inspect():
    """纯源码探查：读 tqcenter.py 源码定论风险点1/2，不连真机。"""
    print("=" * 70)
    print("阶段1：源码静态探查（不连接通达信）")
    print("=" * 70)

    candidates = [
        Path("D:/new_tdx64/PYPlugins/sys/tqcenter.py"),
        Path("D:/new_tdx64_live/PYPlugins/sys/tqcenter.py"),
    ]
    src_path = next((p for p in candidates if p.exists()), None)
    if src_path is None:
        print("[FAIL] 找不到 tqcenter.py，无法探查")
        return False
    src = src_path.read_text(encoding="utf-8")
    print(f"[OK] 探查源码：{src_path}")

    # --- 风险点1：initialize / mode 切换 ---
    print("\n--- 风险点1：TDX mode 切换 ---")
    # initialize 签名
    if "def initialize(cls, \n                   path:str," in src:
        print("[源码] initialize(path, dll_path='') — path 是连接标识")
    # _auto_initialize 里 _connection_path 的用法
    if "cls.file_name = cls._connection_path.encode('utf-8')" in src:
        print("[源码] _connection_path 直接 encode 作 InitConnect 第一参数（连接标识/通信句柄名）")
    if "cls.run_mode = int(arguments[1])" in src:
        print("[源码] run_mode 来自命令行 --run_tdx 参数，非 initialize 入参")
    # 结论
    print("""
[结论] 风险点1：
  - initialize(path) 的 path 是「连接标识」（用本文件路径作唯一标识），不是 TDX 目录。
  - TDX 目录由 sys.path 注入哪个 tqcenter.py 决定 → import 后无法运行时切换。
  - run_mode 来自命令行 --run_tdx，非 API 控制。
  → 方案调整：get_tq(mode) 按 mode 注入对应 tdx_live_path/tdx_backtest_path 到 sys.path，
    但 tqcenter 是单例类（@classmethod），同进程只能持一个 TDX 目录连接。
    回测已在 ProcessPoolExecutor 子进程，天然隔离；实盘在主进程持 live 连接。
    主进程若要同时跑回测+实盘，回测必须走子进程（已如此）。
""")

    # --- 风险点2：回调→喂数据→公式闭环 ---
    print("--- 风险点2：回调→喂数据→公式闭环 ---")
    if "def formula_set_data(cls," in src and '"type": 0' in src:
        print("[源码] formula_set_data(type=0) → dll.TdxFuncMain 注入股票数据（内存喂数据）")
    if "def formula_format_data(cls," in src:
        print("[源码] formula_format_data — get_market_data 的 OHLCV DataFrame → 公式可识别格式")
    if "def formula_process_mul_zb(" in src:
        print("[源码] formula_process_mul_zb → formula_process_mul(type=4) → dll.TdxFuncMain 算公式")
    # 确认没有「写本地文件」API
    has_write_file = any(k in src for k in [
        "def append_bar", "def write_data", "def save_kline", "写文件", "落盘",
    ])
    print(f"[源码] 是否有「写本地 bar 文件」API：{'是(有)' if has_write_file else '否(无)'}")
    # subscribe_hq 限制
    if "订阅数大于100" in src:
        print("[源码] subscribe_hq 限制：订阅股票数 ≤ 100（硬上限）")
    print("""
[结论] 风险点2：
  - 闭环是：subscribe_hq 回调拿 bar → formula_format_data 格式化 → formula_set_data 注入
    → formula_process_mul_zb 算公式。内存注入，非写本地 .day 文件。
  - 用户「写本地文件触发公式」的直觉对应的真实 API 是 formula_set_data（内存喂数据）。
  - subscribe_hq 上限 100 只 → 多组合股票并集超 100 需分批订阅或后续评估升级。
  → 方案调整：LiveEngine 回调链路改为
    on_bar(bar_dict) → formula_format_data + formula_set_data → formula_process_mul_zb
    （而非「写文件」）。
""")

    print("阶段1 完成。两个风险点已从源码定论，无需连真机。")
    print("阶段2 才需连真机：抓 subscribe_hq 回调真实 JSON 格式（风险点3）。")
    return True


def stage2_live_probe():
    """连真机：抓 subscribe_hq 回调格式 + 验证 formula_set_data→公式端到端。
    需通达信 live 版（D:/new_tdx64_live）运行并登录行情。"""
    print("=" * 70)
    print("阶段2：真实通达信连接探查（需 live 版在跑）")
    print("=" * 70)

    # 注入 live 目录的 tqcenter 路径
    live_pyplugins = "D:/new_tdx64_live/PYPlugins"
    for sub in ("sys", "user"):
        p = f"{live_pyplugins}/{sub}"
        if p not in sys.path:
            sys.path.append(p)

    try:
        from tqcenter import tq
    except Exception as e:
        print(f"[FAIL] 无法 import tqcenter：{e}")
        print("       确认通达信 live 版目录 D:/new_tdx64_live/PYPlugins 存在")
        return False

    # initialize 用本文件路径作连接标识
    conn_path = __file__.replace("\\", "/")
    try:
        tq.initialize(conn_path)
        print("[OK] tq.initialize 成功（live 目录）")
    except Exception as e:
        print(f"[FAIL] tq.initialize 失败：{e}")
        print("       确认通达信 live 版已启动并登录行情")
        return False

    # 风险点3：抓 subscribe_hq 回调格式
    # subscribe_hq 用 type:102/sub_type:0，回调按单只 stock_code 分发。
    # 多订阅几只活跃股票 + 延长等待，区分「订阅确认包」与「行情推送包」。
    print("\n--- 风险点3：subscribe_hq 回调真实 JSON 格式 ---")
    probe_codes = ["000001.SZ", "600000.SH", "000002.SZ"]
    captured = {"events": [], "done": False}

    def on_hq(data_str):
        """记录所有回调的完整原始 data_str（不截断），区分确认包 vs 行情包。"""
        if captured["done"]:
            return
        s = data_str if isinstance(data_str, str) else data_str.decode("utf-8", "ignore")
        captured["events"].append(s)
        idx = len(captured["events"])
        print(f"[回调 #{idx}] 完整 data_str ({len(s)} 字符)：")
        print(s)
        print("-" * 50)

    try:
        tq.subscribe_hq(stock_list=probe_codes, callback=on_hq)
        print(f"[OK] subscribe_hq({probe_codes}) 订阅成功，等待回调（最多 60 秒）...")
    except Exception as e:
        print(f"[FAIL] subscribe_hq 失败：{e}")
        tq.close()
        return False

    # 等回调（盘中推送频繁；盘外可能仅订阅确认包）
    import time
    for _ in range(60):
        if len(captured["events"]) >= 8:
            break
        time.sleep(1)
    captured["done"] = True

    if not captured["events"]:
        print("[WARN] 60 秒内无回调 —— 可能非交易时段，或通达信未推送")
        print("       请在交易时段（9:30-15:00）重跑阶段2")
    else:
        print(f"\n[结论] 风险点3：共抓到 {len(captured['events'])} 次回调")
        # 统计回调数据结构差异
        import json as _json
        keys_seen = set()
        for ev in captured["events"]:
            try:
                d = _json.loads(ev)
                keys_seen.update(d.keys())
            except Exception:
                pass
        print(f"       所有回调中出现过的 JSON 字段：{sorted(keys_seen)}")
        has_ohlc = any(k in keys_seen for k in ["Price", "Open", "Close", "LastPrice", "Now", "买价", "卖价"])
        print(f"       是否含行情字段(OHLC/Price等)：{has_ohlc}")
        if not has_ohlc:
            print("       → 仅订阅确认包，行情字段可能用中文字段名或在另一接口")
            print("       → 需查 tqcenter 文档或试 subscribe_quote(单股K线回调)")
        print("       → LiveEngine.on_bar 按此 JSON 结构构造 BarEvent")

    # 风险点4：hq 通知 → get_market_data 拉最新 bar（盘中实时数据验证）
    print("\n--- 风险点4：hq 通知 → get_market_data 拉最新 1m bar ---")
    print("           （subscribe_hq 仅通知「有更新」，OHLCV 需主动 get_market_data 拉）")
    try:
        probe_code = "000001.SZ"
        df = tq.get_market_data(
            field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
            stock_list=[probe_code], period="1m",
            count=3, dividend_type="front", fill_data=True,
        )
        if df is None:
            print("[WARN] get_market_data(period=1m) 返回 None")
        else:
            print(f"[OK] get_market_data(1m, count=3) 返回，字段：{list(df.keys())}")
            # 打印 Close 序列最后几个时间点，验证是否是「当前实时」数据
            close_df = df.get("Close")
            if close_df is not None and hasattr(close_df, "index"):
                import pandas as _pd
                idx = close_df.index[-3:]
                vals = close_df[probe_code].iloc[-3:] if probe_code in close_df.columns else None
                print(f"       Close 最后 3 个时间点：{list(idx)}")
                print(f"       Close 值：{list(vals) if vals is not None else 'N/A'}")
                last_ts = idx[-1]
                print(f"       最新 bar 时间戳：{last_ts}  (当前 {__import__('datetime').datetime.now()})")
                # 验证最新 bar 是否接近当前时间（盘中应在前几分钟内）
            # 再试 formula_format_data + formula_set_data（用 probe_code 而非遗留变量）
            formatted = tq.formula_format_data(df)
            print(f"[OK] formula_format_data 成功，股票数：{len(formatted)}")
            sd = tq.formula_set_data(
                stock_code=probe_code, stock_period="1m",
                stock_data=formatted[probe_code], count=len(formatted[probe_code]),
            )
            err = sd.get("ErrorId") if isinstance(sd, dict) else None
            print(f"[{'OK' if err == '0' else 'WARN'}] formula_set_data(1m) → ErrorId={err}")
    except Exception as e:
        print(f"[WARN] get_market_data/formula_set_data 验证异常：{e}")

    try:
        tq.close()
        print("\n[OK] tq.close 完成，连接已释放")
    except Exception:
        pass

    print("\n阶段2 完成。")
    return True


def stage3_snapshot_probe():
    """探查实时数据接口字段：get_market_snapshot / get_pricevol 盘中返回什么。
    回答关键问题：subscribe_hq 通知后拿到的实时数据能否合成 OHLCV bar 喂公式。
    需通达信 live 版运行并登录行情。"""
    print("=" * 70)
    print("阶段3：实时数据接口字段探查（需 live 版在跑、登录行情、盘中或盘后）")
    print("=" * 70)

    live_pyplugins = "D:/new_tdx64_live/PYPlugins"
    for sub in ("sys", "user"):
        p = f"{live_pyplugins}/{sub}"
        if p not in sys.path:
            sys.path.append(p)

    try:
        from tqcenter import tq
    except Exception as e:
        print(f"[FAIL] 无法 import tqcenter：{e}")
        return False

    conn_path = __file__.replace("\\", "/")
    try:
        tq.initialize(conn_path)
        print("[OK] tq.initialize 成功")
    except Exception as e:
        print(f"[FAIL] tq.initialize 失败：{e}")
        return False

    probe_code = "000001.SZ"

    # --- A. get_market_snapshot：tick 级实时快照字段 ---
    print("\n--- A. get_market_snapshot（实时快照，盘中拿当天最新 tick）---")
    try:
        snap = tq.get_market_snapshot(stock_code=probe_code, field_list=[])
        if not snap:
            print("[WARN] snapshot 返回空 —— 可能非交易时段或未接行情")
        else:
            print(f"[OK] snapshot 返回 {len(snap)} 个字段：")
            for k, v in snap.items():
                print(f"       {k} = {v}")
            # 关键：有没有现成的高开低收/累计量字段
            ohlc_keys = [k for k in snap.keys() if k.lower() in (
                "open", "high", "low", "close", "lastprice", "now", "price",
                "pre_close", "amount", "volume", "vol", "turnover",
            )]
            print(f"       [含 OHLCV 相关字段]：{ohlc_keys or '无（需另测字段名）'}")
    except Exception as e:
        print(f"[WARN] snapshot 异常：{e}")

    # --- B. get_pricevol：批量价格成交量 ---
    print("\n--- B. get_pricevol（批量价量，看结构）---")
    try:
        pv = tq.get_pricevol(stock_list=[probe_code, "600000.SH"])
        if not pv:
            print("[WARN] pricevol 返回空")
        else:
            print(f"[OK] pricevol 返回，类型 {type(pv).__name__}")
            if isinstance(pv, dict):
                print(f"       顶层键：{list(pv.keys())[:10]}")
                first_key = next(iter(pv), None)
                if first_key is not None:
                    val = pv[first_key]
                    print(f"       {first_key} → {type(val).__name__}: {val if not isinstance(val, (dict, list)) else (list(val.keys()) if isinstance(val, dict) else val[:5])}")
            else:
                print(f"       值：{str(pv)[:300]}")
    except Exception as e:
        print(f"[WARN] pricevol 异常：{e}")

    # --- C. 对照：get_market_data 当天 1m（确认是否真的只能拿到昨天）---
    print("\n--- C. get_market_data(1m, count=2) 对照（预期：盘中最新bar是今天 or 昨天）---")
    try:
        df = tq.get_market_data(
            field_list=["Open", "High", "Low", "Close", "Volume"],
            stock_list=[probe_code], period="1m", count=2,
            dividend_type="front", fill_data=True,
        )
        close_df = df.get("Close")
        if close_df is not None and hasattr(close_df, "index"):
            print(f"       Close 最后 2 个时间点：{list(close_df.index[-2:])}")
            print(f"       当前时间：{__import__('datetime').datetime.now()}")
        else:
            print("[WARN] get_market_data(1m) 无数据")
    except Exception as e:
        print(f"[WARN] get_market_data 异常：{e}")

    try:
        tq.close()
        print("\n[OK] tq.close 完成")
    except Exception:
        pass

    print("""
[阶段3 结论模板]
  - snapshot 字段：{见上方实际输出}
  - pricevol 结构：{见上方实际输出}
  - get_market_data(1m) 最新 bar 时间：{见上方}
  → 若 snapshot 含 Open/High/Low/Close/Volume/Amount 当日累计字段
    → 可在引擎内用「昨日已合成 bar + 今日实时累计」拼出当前 bar
  → 若 snapshot 只有最新价(LastPrice) 无累计量
    → 需引擎自行按周期聚合 tick（订阅后本地累积 OHLCV）
""")
    return True


def stage4_callback_frequency_probe():
    """实测 subscribe_hq 回调频率 + snapshot 每次是否变化。
    回答：1 分钟内 snapshot 触发多少次？是 tick 级（高频）还是分钟级？
    需通达信 live 版运行、登录行情、交易时段（9:30-15:00）。"""
    print("=" * 70)
    print("阶段4：subscribe_hq 回调频率 + snapshot 变化实测（需 live 版、盘中）")
    print("=" * 70)

    live_pyplugins = "D:/new_tdx64_live/PYPlugins"
    for sub in ("sys", "user"):
        p = f"{live_pyplugins}/{sub}"
        if p not in sys.path:
            sys.path.append(p)

    try:
        from tqcenter import tq
    except Exception as e:
        print(f"[FAIL] 无法 import tqcenter：{e}")
        return False

    conn_path = __file__.replace("\\", "/")
    try:
        tq.initialize(conn_path)
        print("[OK] tq.initialize 成功")
    except Exception as e:
        print(f"[FAIL] tq.initialize 失败：{e}")
        return False

    probe_codes = ["000001.SZ", "600000.SH"]
    state = {
        "callbacks": [],      # [(seq, code, now_volume, now_price, timestamp)]
        "done": False,
    }

    def on_hq(data_str):
        if state["done"]:
            return
        s = data_str if isinstance(data_str, str) else data_str.decode("utf-8", "ignore")
        import json as _json
        import datetime as _dt
        try:
            d = _json.loads(s)
            code = d.get("Code", "?")
        except Exception:
            code = "?"
        # 每次回调拉一次 snapshot，看 Now/Volume 是否真的在变
        try:
            snap = tq.get_market_snapshot(stock_code=code, field_list=[])
            now_p = snap.get("Now") if snap else None
            vol = snap.get("Volume") if snap else None
            mx = snap.get("Max") if snap else None
            mn = snap.get("Min") if snap else None
        except Exception as e:
            now_p = vol = mx = mn = f"ERR:{e}"
        ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        seq = len(state["callbacks"]) + 1
        state["callbacks"].append((seq, code, ts, now_p, vol, mx, mn))
        # 控制台只打印前 5 条 + 每 10 条一条，避免刷屏
        if seq <= 5 or seq % 10 == 0:
            print(f"  [#{seq}] {ts} {code}  Now={now_p} Vol={vol} Max={mx} Min={mn}")

    try:
        tq.subscribe_hq(stock_list=probe_codes, callback=on_hq)
        print(f"[OK] subscribe_hq({probe_codes}) 订阅成功，统计 60 秒内回调频率...")
    except Exception as e:
        print(f"[FAIL] subscribe_hq 失败：{e}")
        tq.close()
        return False

    import time
    for _ in range(60):
        if state["done"]:
            break
        time.sleep(1)
    state["done"] = True

    cbs = state["callbacks"]
    print(f"\n[结论] 60 秒内共 {len(cbs)} 次回调")
    if cbs:
        # 按股票分组统计
        from collections import Counter
        by_code = Counter(c[1] for c in cbs)
        print(f"       按股票：{dict(by_code)}")
        # 看 Volume 是否变化（判断 snapshot 是实时更新还是固定值）
        for code in probe_codes:
            vols = [c[4] for c in cbs if c[1] == code and c[4] is not None]
            uniq = set(vols)
            print(f"       {code} Volume 出现 {len(uniq)} 种不同值（共 {len(vols)} 次拉取）"
                  f"{'→ snapshot 在实时更新' if len(uniq) > 1 else '→ Volume 未变（可能停牌/非交易时段/未接行情）'}")
        # 时间间隔分布
        if len(cbs) >= 2:
            print(f"       首次回调：{cbs[0][2]}，末次：{cbs[-1][2]}")
        print(f"       → 每分钟约 {len(cbs)} 条 snapshot 记录（含通知去重后实际拉取数）")

    try:
        tq.close()
        print("[OK] tq.close 完成")
    except Exception:
        pass

    print("""
[阶段4 结论解读]
  - 若每分钟 callback 数 >> 1（如几十次）：subscribe_hq 是 tick 级高频通知，
    snapshot 每次拿到的 Now/Volume 在变 → 走分钟线需 BarAggregator 做 tick→bar 聚合
  - 若每分钟 callback 数 ≈ 1：可能已是分钟级推送，聚合更简单
  - 若 callback = 0 或 Volume 不变：非交易时段或未登录行情，盘中重测
""")
    return True


def stage5_high_low_cumulative_probe():
    """实测 get_market_snapshot 的 Max/Min 是「今日累计」还是「时间窗口内」。

    方法：盯住一只股票，主动轮询拉 snapshot（不靠订阅通知，固定间隔），
    记录 Open/Max/Min/Now 随时间轨迹：
      - 若 Max 单调不降、Min 单调不升 → 今日累计（开盘至今最高/最低）
      - 若 Max/Min 会回退/重置 → 时间窗口内
    主动轮询避开「订阅通知去重」干扰，纯粹看快照字段本身。
    需通达信 live 版运行、登录行情、交易时段。"""
    print("=" * 70)
    print("阶段5：Max/Min 累计性实测（需 live 版、盘中）")
    print("=" * 70)

    live_pyplugins = "D:/new_tdx64_live/PYPlugins"
    for sub in ("sys", "user"):
        p = f"{live_pyplugins}/{sub}"
        if p not in sys.path:
            sys.path.append(p)

    try:
        from tqcenter import tq
    except Exception as e:
        print(f"[FAIL] 无法 import tqcenter：{e}")
        return False

    conn_path = __file__.replace("\\", "/")
    try:
        tq.initialize(conn_path)
        print("[OK] tq.initialize 成功")
    except Exception as e:
        print(f"[FAIL] tq.initialize 失败：{e}")
        return False

    probe_code = "000001.SZ"
    print(f"\n盯住 {probe_code}，每 2 秒拉一次 snapshot，共 15 次（约 30 秒），看 Max/Min 轨迹：\n")
    print(f"  {'#':>3}  {'时间':<12} {'Open':>8} {'Max':>8} {'Min':>8} {'Now':>8} {'Volume':>10}")
    print("  " + "-" * 62)

    import time
    import datetime as _dt
    rows = []
    for i in range(15):
        try:
            snap = tq.get_market_snapshot(stock_code=probe_code, field_list=[])
            o = snap.get("Open"); mx = snap.get("Max"); mn = snap.get("Min")
            now = snap.get("Now"); vol = snap.get("Volume")
        except Exception as e:
            o = mx = mn = now = vol = None
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        rows.append((i + 1, ts, o, mx, mn, now, vol))
        print(f"  {i+1:>3}  {ts:<12} {str(o):>8} {str(mx):>8} {str(mn):>8} {str(now):>8} {str(vol):>10}")
        time.sleep(2)

    # 分析 Max/Min 单调性
    maxs = [r[3] for r in rows if r[3] is not None]
    mins = [r[5] for r in rows if r[5] is not None]
    # 注意 tuple 顺序：(seq, ts, Open, Max, Min, Now, Volume) → Max=idx3, Min=idx4
    maxs = [r[3] for r in rows if r[3] is not None]
    mins = [r[4] for r in rows if r[4] is not None]

    def monotonic_nondec(vals):
        return all(vals[i] <= vals[i+1] for i in range(len(vals) - 1))
    def monotonic_noninc(vals):
        return all(vals[i] >= vals[i+1] for i in range(len(vals) - 1))

    print("\n[单调性分析]")
    print(f"  Max 序列：{maxs}")
    print(f"  Max 单调不降（累计）？{monotonic_nondec(maxs) if len(maxs) >= 2 else '样本不足'}")
    print(f"  Min 序列：{mins}")
    print(f"  Min 单调不升（累计）？{monotonic_noninc(mins) if len(mins) >= 2 else '样本不足'}")

    print("""
[阶段5 结论解读]
  - Max 单调不降 + Min 单调不升 → Max/Min 是「今日开盘至今累计」最高/最低
    → 1d 路径：Max/Min 直接作当日日线 High/Low，正确
    → 1m 路径：不能用 snapshot 的 Max/Min 当 1m bar 的 High/Low（那是全天累计），
      必须聚合器自行从 Now(tick) 维护「本分钟内 max/min」
  - Max/Min 会回退或重置 → 是时间窗口内，需进一步确认窗口长度
""")

    try:
        tq.close()
        print("[OK] tq.close 完成")
    except Exception:
        pass
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="实盘引擎可行性验证")
    parser.add_argument("--stage", choices=["1", "2", "3", "4", "5"], default="1",
                        help="1=源码探查 2=连真机抓回调 3=快照字段 4=回调频率 5=Max/Min累计性(需盘中)")
    args = parser.parse_args()

    if args.stage == "1":
        ok = stage1_source_inspect()
    elif args.stage == "2":
        ok = stage2_live_probe()
    elif args.stage == "3":
        ok = stage3_snapshot_probe()
    elif args.stage == "4":
        ok = stage4_callback_frequency_probe()
    else:
        ok = stage5_high_low_cumulative_probe()

    sys.exit(0 if ok else 1)
