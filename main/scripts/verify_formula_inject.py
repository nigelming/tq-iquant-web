# -*- coding: utf-8 -*-
"""公式内存注入可行性验证脚本 — 对比「正常自取调用」vs「内存注入后调用」。

验证目标（用户指定方法）：
  用系统公式 MACROSSPRO，
  (1) 正常调用 formula_process_mul_zb —— SDK 自取 .lc1 数据自算，记录结果；
  (2) 内存注入调用：get_market_data 取同样 OHLCV → formula_format_data
      → formula_set_data 注入 → formula_process_mul_zb 算，记录结果；
  (3) 对比两者 1m / 5m 输出是否一致 → 判定内存注入是否可替代正常调用。

判定标准：
  逐变量逐条比对 Value，完全一致 = PASS（内存注入可行）；
  有差异 = FAIL（注入数据未被公式引擎采用，或采用方式不同）。

为什么用历史日期范围（非「当前实时」）：
  .lc1 分钟文件盘后才写入，盘中只有昨日及更早数据；盘中「实时 bar」仅在
  hq_cache 快照里。本脚本目的是验证「注入数据能否被公式引擎采用」，用历史
  已落盘的日期范围即可公平对比（两条路径读同一份数据源），不受盘中实时
  缺数据干扰。盘中实时数据回填是下一切片（/deals + 实时 bar 注入）的范畴。

用法（需通达信 live 版运行并登录）：
  cd main
  uv run python scripts/verify_formula_inject.py
  uv run python scripts/verify_formula_inject.py --code 600000.SH --days 8 --count 200

注意：
  - 连 live 版目录 D:/new_tdx64_live/PYPlugins（与实盘 BarPoller 同源）。
  - 占 run_id，跑完自动 tq.close()。
"""
import argparse
import sys
import time
from datetime import datetime, timedelta


def _inject_paths():
    """注入 live 版 tqcenter 路径到 sys.path。"""
    live_pyplugins = "D:/new_tdx64_live/PYPlugins"
    for sub in ("sys", "user"):
        p = live_pyplugins + "/" + sub
        if p not in sys.path:
            sys.path.append(p)


def _print_section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _fmt_value(v):
    """统一数值格式化便于对比（容 6 位浮点）。None 保留原样。"""
    if v is None:
        return None
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return v


def _extract_vars(raw, code):
    """从 formula_process_mul_zb 返回里取某股票的 {var_name: [Value,...]}。

    raw 结构：{stock_code: {var_name: [{"Date":..,"Value":..}, ...]}, "ErrorId":"0", ...}
    """
    if not isinstance(raw, dict) or not raw:
        return None, "raw 为空或非 dict"
    err = raw.get("ErrorId")
    if err is not None and str(err) not in ("0", "19"):
        return None, "ErrorId=%s (%s)" % (err, raw.get("Error"))
    stock_data = raw.get(code)
    if not isinstance(stock_data, dict) or not stock_data:
        return None, "返回中无股票 %s 的数据" % code
    meta_keys = {"ErrorId", "Error", "code", "Date"}
    result = {}
    for var_name, val_list in stock_data.items():
        if var_name in meta_keys:
            continue
        if not isinstance(val_list, list):
            continue
        result[var_name] = [_fmt_value(e.get("Value")) for e in val_list if isinstance(e, dict)]
    return result, "OK (vars=%d)" % len(result)


def _looks_binary(vals):
    """判断序列是否像 0/1 信号（值域 ⊆ {0,1}，且至少有一个 0 或 1）。"""
    seen = set()
    for v in vals:
        try:
            seen.add(float(v))
        except (TypeError, ValueError):
            continue
    if not seen:
        return False
    return seen <= {0.0, 1.0}


def _count_signal_hits(vals):
    """统计序列中值为 1（信号触发）的个数。非数值/None 不计。"""
    n = 0
    for v in vals:
        try:
            if float(v) == 1.0:
                n += 1
        except (TypeError, ValueError):
            pass
    return n


def _compare_vars(normal, injected, code, period):
    """逐变量逐条对比 normal vs injected，打印差异并返回是否全等且有信号。

    防虚假一致：若两侧所有变量全程为 0（无任何信号触发），即使全等也判为
    「无意义」——可能两条路径都取不到数据/算不出信号，证明不了注入生效。
    """
    print("\n--- 对比 [%s %s] ---" % (code, period))
    if normal is None:
        print("[FAIL] 正常调用无数据，无法对比")
        return False
    if injected is None:
        print("[FAIL] 注入调用无数据，无法对比")
        return False
    all_pass = True
    all_vars = sorted(set(normal.keys()) | set(injected.keys()))
    if not all_vars:
        print("[WARN] 两侧均无变量输出")
        return False
    # 信号命中统计（只统计疑似信号变量：值域含 0/1 的；均线类连续值不计）
    total_hits_normal = 0
    total_hits_injected = 0
    for var in all_vars:
        n_vals = normal.get(var) or []
        i_vals = injected.get(var) or []
        n_hit = _count_signal_hits(n_vals)
        i_hit = _count_signal_hits(i_vals)
        # 值域含 0/1 视为信号变量（均线类连续值一般无 1）
        looks_like_signal = n_hit > 0 or i_hit > 0 or _looks_binary(n_vals)
        if looks_like_signal:
            total_hits_normal += n_hit
            total_hits_injected += i_hit
            print("  [信号] %s: normal 命中 %d 个 1，injected 命中 %d 个 1"
                  % (var, n_hit, i_hit))
        if n_vals == i_vals:
            print("  [PASS] %s: %d 条一致  末3=%s" % (var, len(n_vals), n_vals[-3:]))
        else:
            all_pass = False
            n_len, i_len = len(n_vals), len(i_vals)
            print("  [FAIL] %s: 条数 normal=%d injected=%d" % (var, n_len, i_len))
            # 找首个差异
            common = min(n_len, i_len)
            diff_idx = None
            for k in range(common):
                if n_vals[k] != i_vals[k]:
                    diff_idx = k
                    break
            if diff_idx is not None:
                print("         首个差异 idx=%d: normal=%s injected=%s"
                      % (diff_idx, n_vals[diff_idx], i_vals[diff_idx]))
            else:
                print("         公共部分一致，差异仅在条数（长度对齐问题）")
            print("         normal  末3: %s" % n_vals[-3:])
            print("         injected 末3: %s" % i_vals[-3:])
    # 防虚假一致：全等但全程无信号 → 判无意义
    if all_pass and total_hits_normal == 0 and total_hits_injected == 0:
        print("\n  [WARN] 两侧全等但全程无信号(无 1) → 对比无意义")
        print("         可能两条路径都取不到数据/算不出交叉；")
        print("         建议：扩大日期范围(--days 20)/用 count=-1/换活跃股票重试")
        return False
    if all_pass:
        print("\n  [信号统计] normal 共 %d 个 1，injected 共 %d 个 1"
              % (total_hits_normal, total_hits_injected))
    return all_pass


def _run_normal(tq, formula_name, code, period, start_str, end_str, count):
    """正常调用：SDK 自取 .lc1 数据自算。"""
    t0 = time.time()
    raw = tq.formula_process_mul_zb(
        formula_name=formula_name,
        formula_arg="",
        return_count=-1,
        return_date=True,
        xsflag=-1,
        stock_list=[code],
        stock_period=period,
        start_time=start_str,
        end_time=end_str,
        count=count,
        dividend_type=1,
    )
    dt = time.time() - t0
    print("[正常调用] %.3fs  ErrorId=%s" % (dt, raw.get("ErrorId") if isinstance(raw, dict) else "?"))
    return _extract_vars(raw, code)


def _run_injected(tq, formula_name, code, period, start_str, end_str, count):
    """内存注入调用：get_market_data → format → set_data → process_mul_zb。"""
    t0 = time.time()
    # 1) 取同样范围 OHLCV
    df = tq.get_market_data(
        field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
        stock_list=[code], period=period,
        start_time=start_str, end_time=end_str,
        count=count, dividend_type="front", fill_data=True,
    )
    if df is None:
        return None, "get_market_data 返回 None"
    # 校验字段齐全
    need = ["Open", "High", "Low", "Close", "Volume", "Amount"]
    miss = [k for k in need if k not in df or df[k] is None or df[k].empty]
    if miss:
        return None, "get_market_data 缺字段 %s" % miss
    close_df = df["Close"]
    n_bars = len(close_df)
    print("[注入] get_market_data OK，bar 数=%d，时间范围 %s ~ %s"
          % (n_bars, close_df.index[0], close_df.index[-1]))
    # 2) 格式化
    formatted = tq.formula_format_data(df)
    if not formatted or code not in formatted:
        return None, "formula_format_data 无 %s 数据" % code
    # 3) 注入
    sd = tq.formula_set_data(
        stock_code=code, stock_period=period,
        stock_data=formatted[code], count=len(formatted[code]),
        dividend_type=0,
    )
    err = sd.get("ErrorId") if isinstance(sd, dict) else None
    if err != "0":
        return None, "formula_set_data ErrorId=%s" % err
    print("[注入] formula_set_data OK (ErrorId=0)，注入 %d 条" % len(formatted[code]))
    # 4) 注入后再调公式算 —— 同样的查询参数
    raw = tq.formula_process_mul_zb(
        formula_name=formula_name,
        formula_arg="",
        return_count=-1,
        return_date=True,
        xsflag=-1,
        stock_list=[code],
        stock_period=period,
        start_time=start_str,
        end_time=end_str,
        count=count,
        dividend_type=1,
    )
    dt = time.time() - t0
    print("[注入调用] 总耗时 %.3fs  ErrorId=%s"
          % (dt, raw.get("ErrorId") if isinstance(raw, dict) else "?"))
    return _extract_vars(raw, code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="600000.SH", help="测试股票代码")
    ap.add_argument("--days", type=int, default=8, help="回溯天数（用于定 end=昨收盘）")
    ap.add_argument("--count", type=int, default=200, help="公式 count 参数")
    ap.add_argument("--formula", default="MACROSSPRO", help="公式名")
    args = ap.parse_args()

    code = args.code
    formula_name = args.formula
    count = args.count

    _print_section("公式内存注入可行性验证 — %s @ %s" % (formula_name, code))
    print("方法：正常自取调用 vs 内存注入后调用，对比 1m / 5m 输出是否一致")

    _inject_paths()
    try:
        from tqcenter import tq
    except Exception as e:
        print("[FAIL] 无法 import tqcenter：", e)
        print("       确认 D:/new_tdx64_live/PYPlugins 存在")
        return 1

    conn_path = __file__.replace("\\", "/")
    # tqcenter._auto_initialize 第 729 行把任何异常都包成「连接路径为空」(丢原始 e)，
    # 误导诊断。这里直接调 dll.InitConnect 探查原始返回，定位真因。
    try:
        tq.initialize(conn_path)
        print("[OK] tq.initialize 成功（live 目录）")
    except Exception as e:
        print("[FAIL] tq.initialize 抛异常（兜底消息可能误导）：%s" % e)
        print("       → 真因探查：直接调 dll.InitConnect 看原始返回...")
        try:
            import ctypes as _ctypes
            from tqcenter import dll as _dll, get_python_version_number
            _dll.InitConnect.restype = _ctypes.c_char_p
            fname = conn_path.encode("utf-8")
            dpath = (tq.dll_path or "").encode("utf-8")
            ptr = _dll.InitConnect(fname, dpath, 0, get_python_version_number(), False)
            if not ptr or len(ptr) <= 0:
                print("       [真因] InitConnect 返回空指针 → TPythClient 未响应")
                print("       → 通达信 live 版 (D:/new_tdx64_live) 未启动 或 未登录行情")
            else:
                ret = ptr.decode("utf-8", "ignore")
                print("       [真因] InitConnect 原始返回：%s" % ret[:300])
        except Exception as e2:
            print("       [探查失败] %s" % e2)
        print("\n排查清单：")
        print("  1. 通达信 live 版 D:/new_tdx64_live 是否已启动并登录（看行情能否刷新）")
        print("  2. 是否已有同名策略占用连接（关掉旧的再跑）")
        print("  3. TPythClient.dll 是否被通达信主进程加载（客户端须在跑）")
        return 1

    # 日期范围：end=昨收盘，start=前 days 天（确保 .lc1 有落盘数据）
    now = datetime.now()
    end_str = (now - timedelta(days=1)).strftime("%Y%m%d") + "150000"
    start_str = (now - timedelta(days=args.days)).strftime("%Y%m%d") + "090000"
    print("日期范围：%s ~ %s  count=%d" % (start_str, end_str, count))

    results = {}
    for period in ("1m", "5m"):
        _print_section("周期 %s — 正常调用" % period)
        normal, n_msg = _run_normal(tq, formula_name, code, period, start_str, end_str, count)
        print("  正常调用结果：%s" % n_msg)
        if normal:
            for v, vals in sorted(normal.items()):
                hit = _count_signal_hits(vals)
                tag = "  ★%d个1" % hit if hit else ""
                print("    %s: %d 条  末3=%s%s" % (v, len(vals), vals[-3:], tag))

        _print_section("周期 %s — 内存注入调用" % period)
        injected, i_msg = _run_injected(tq, formula_name, code, period, start_str, end_str, count)
        print("  注入调用结果：%s" % i_msg)
        if injected:
            for v, vals in sorted(injected.items()):
                hit = _count_signal_hits(vals)
                tag = "  ★%d个1" % hit if hit else ""
                print("    %s: %d 条  末3=%s%s" % (v, len(vals), vals[-3:], tag))

        results[period] = _compare_vars(normal, injected, code, period)

    try:
        tq.close()
        print("\n[OK] tq.close 完成")
    except Exception:
        pass

    _print_section("结论")
    overall = True
    for period, ok in results.items():
        tag = "PASS — 一致且有信号" if ok else "FAIL — 存在差异 或 无信号(无意义)"
        print("  %s: %s" % (period, tag))
        overall = overall and ok
    if overall:
        print("\n[总判定 PASS] 内存注入可行：实盘可用 get_market_data(桥/xtdata) → format → set_data")
        print("  → process_mul_zb 链路算公式，不依赖 .lc1 盘后落盘。")
        print("  下一步：实盘 LiveEngine 接入此链路，盘中注入 iQuant 桥拉的实时 bar。")
    else:
        print("\n[总判定 未通过] 请看上方明细：")
        print("  - 若是「无信号(无 1)」→ 对比无意义，扩大范围重试：")
        print("      uv run python scripts/verify_formula_inject.py --days 20 --count -1")
        print("    或换活跃股票： --code 000001.SZ")
        print("  - 若是「值/条数差异」→ 注入数据未被等价采用，可能需改查询参数")
        print("    （如注入后 count=-1 不传时间范围，强制用注入数据）")
    return 0 if overall else 2


if __name__ == "__main__":
    sys.exit(main())
