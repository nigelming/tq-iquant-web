import logging

import pandas as pd
import polars as pl
from typing import Dict, List, Callable, Optional
from .utils import get_tdx_lock, get_tq

logger = logging.getLogger(__name__)

_FIELDS = ["Open", "High", "Low", "Close", "Volume", "Amount"]

# --- 午休幽灵 bar（13:00）---
# 批量 get_market_data(fill_data=True) 会把午休空档用「上一根 bar 收盘」填充：
# 在 11:30 后重复一根同样的数据并打 13:00 时间戳（1m/5m/15m/30m/1h 皆如此，
# 实测 30m = 9 根/日，13:00 与 11:30 的 OHLC 完全一致）。TQ 公式/通达信图表返回
# 逐 bar 真实序列，无此 bar；且 A 股分钟 bar 下午首根合法时间为
# 13:01/13:05/13:15/13:30/14:00，13:00 永远不可能真实存在，整根剔除安全。
# 不清掉会让 klines 时间轴多一根（9 根/日 vs 公式 8 根/日），分钟级公式输出按
# 索引对齐时整体前移——等于把后 N 根 bar 的信号全部提前触发（隐性未来函数）。
_LUNCH_PHANTOM_PERIODS = {"1m", "5m", "15m", "30m", "1h"}
_LUNCH_PHANTOM_TIME = "13:00"


def strip_lunch_phantom(raw: Optional[dict], period: str) -> Optional[dict]:
    """剔除 get_market_data(fill_data=True) 批量取数注入的午休幽灵 13:00 bar。

    raw: {field: pandas.DataFrame(index=时间戳, columns=股票代码)}。
    无 13:00（或非分钟周期，1d/1w/1mon 无此问题）时原样透传（对象同一，零拷贝）。
    """
    if not raw or period not in _LUNCH_PHANTOM_PERIODS:
        return raw
    out: Dict[str, pd.DataFrame] = {}
    changed = False
    for field, df in raw.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            out[field] = df
            continue
        mask = df.index.to_series().dt.strftime("%H:%M") != _LUNCH_PHANTOM_TIME
        cut = df.loc[mask]
        if cut.shape[0] != df.shape[0]:
            changed = True
            logger.debug("剔除 %s 午休幽灵 13:00 bar %d 根(field=%s)",
                         period, int(df.shape[0] - cut.shape[0]), field)
        else:
            cut = df
        out[field] = cut
    return out if changed else raw


class TQData:
    def __init__(self):
        # 最近一次 get_history_raw 因行情数据异常被跳过的股票代码。
        # 通达信偶发对个别股票返回非法/占位日期（如 '20040000'）会让批量
        # get_market_data 整批抛错；逐股兜底时把取不到的股票记在这里，
        # 供上层（build_klines/run_backtest）转成回测告警。
        self.skipped_stocks: List[str] = []

    def get_stock_pools(self) -> List[dict]:
        with get_tdx_lock():
            return self._get_pools()

    def get_pool_stocks(self, pool_code: str) -> List[dict]:
        with get_tdx_lock():
            return self._get_stocks(pool_code)

    def get_industry_list(self) -> List[dict]:
        """通达信二级行业（881xxx，共 128 个），归一化 [{"code","name"}]。

        list_type=1 时 get_sector_list 返回 [{"Code","Name"}] 字典。
        """
        with get_tdx_lock():
            raw = get_tq().get_sector_list(list_type=1) or []
            out: List[dict] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("Code") or "")
                if code.startswith("881"):
                    out.append({"code": code,
                                "name": str(item.get("Name") or code)})
            return out

    def get_stock_industry(self, stock_code: str) -> Optional[dict]:
        """个股所属通达信二级行业（get_relation 中 BlockType=='行业' 的唯一块）。

        返回 {"code": "881002.SH", "name": "煤炭开采"}；无行业块返回 None。
        """
        with get_tdx_lock():
            blocks = get_tq().get_relation(stock_code) or []
            for b in blocks:
                if isinstance(b, dict) and b.get("BlockType") == "行业":
                    return {"code": b.get("BlockCode"),
                            "name": b.get("BlockName")}
            return None

    def get_history(
        self, stocks: List[str], periods: List[str],
        start: str = "", end: str = "",
        dividend_type: str = "front", count: int = 100,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        with get_tdx_lock():
            return self._load_history(stocks, periods, start, end, dividend_type, count)

    def get_history_raw(
        self, stocks: List[str], periods: List[str],
        start: str = "", end: str = "",
        dividend_type: str = "front", count: int = 100,
    ) -> Dict[str, dict]:
        """取原始 TQ 行情（未经转换），按周期分组：{period: {field: pandas.DataFrame}}。

        field ∈ Open/High/Low/Close/Volume/Amount，DataFrame.index=时间戳，columns=股票代码。
        一次调用 = 单周期多股票。转换由调用方（_convert_market_data）负责，便于单测。
        """
        with get_tdx_lock():
            return self._load_history_raw(stocks, periods, start, end, dividend_type, count)

    def subscribe_bars(
        self, stocks: List[str], periods: List[str],
        callback: Callable,
    ) -> None:
        with get_tdx_lock():
            self._subscribe(stocks, periods, callback)

    def get_all_stocks(self, market: str = "5") -> List[str]:
        tq = get_tq()
        return tq.get_stock_list(market) or []

    # --- TDX 底层调用 ---
    def _get_pools(self) -> List[dict]:
        """通达信用户自定义板块，归一化为 [{"code","name"}]。

        SDK get_user_sector() 返回 [{"Code","Name"}]（大写首字母）。
        v1 曾用 get_sector_list（系统板块，587 个，非用户板块）且把 dict 当 str → 线上炸。
        """
        tq = get_tq()
        sectors = tq.get_user_sector() or []
        return [{"code": s["Code"], "name": s["Name"]} for s in sectors]

    def _get_stocks(self, pool_code: str) -> List[dict]:
        """板块成分股，归一化为 [{"stock_code","stock_name"}]。

        SDK get_stock_list_in_sector(code, block_type=1, list_type=1) 返回
        [{"Code","Name"}]。必须传板块 Code（如 TQCS），传 Name 返回空。
        """
        tq = get_tq()
        stocks = tq.get_stock_list_in_sector(pool_code, block_type=1, list_type=1) or []
        return [
            {"stock_code": s["Code"], "stock_name": s["Name"]}
            for s in stocks
        ]

    def _load_history(
        self, stocks, periods, start="", end="",
        dividend_type="front", count=100,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        tq = get_tq()
        result = {}
        for period in periods:
            df = tq.get_market_data(
                field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                stock_list=stocks,
                period=period,
                start_time=start,
                end_time=end,
                count=count,
                dividend_type=dividend_type,
                fill_data=True,
            )
            if df is not None:
                df = strip_lunch_phantom(df, period)
                for code in stocks:
                    if code not in result:
                        result[code] = {}
                    try:
                        series = {}
                        for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
                            if col in df and code in df[col].columns:
                                series[col] = df[col][code]
                        if series:
                            result[code][period] = pl.DataFrame(series)
                    except Exception:
                        pass
        return result

    def _load_history_raw(
        self, stocks, periods, start="", end="",
        dividend_type="front", count=100,
    ) -> Dict[str, dict]:
        """原始 TQ 行情按周期分组：{period: raw_dict}。raw_dict 即 tq.get_market_data 返回值。

        批量调用若抛异常（通达信偶发返回非法日期，如 '20040000'），退化为逐只
        拉取并跳过取不到的股票，避免个别坏数据导致整单回测失败。被跳过的股票
        记录在 self.skipped_stocks。
        """
        tq = get_tq()
        self.skipped_stocks = []
        result: Dict[str, dict] = {}
        for period in periods:
            try:
                df = tq.get_market_data(
                    field_list=_FIELDS,
                    stock_list=stocks,
                    period=period,
                    start_time=start,
                    end_time=end,
                    count=count,
                    dividend_type=dividend_type,
                    fill_data=True,
                )
            except Exception as e:
                logger.warning(
                    "get_market_data 批量拉取失败(period=%s)，逐股兜底：%s", period, e)
                df = self._fetch_per_stock(
                    tq, stocks, period, start, end, dividend_type, count)
            if df is not None:
                result[period] = strip_lunch_phantom(df, period)
        return result

    def _fetch_per_stock(
        self, tq, stocks, period, start, end, dividend_type, count,
    ) -> Optional[dict]:
        """批量失败时逐只拉取，合并为与批量同构的 {field: DataFrame(index=time, columns=stocks)}。

        抛异常的股票记入 self.skipped_stocks 并跳过；全部失败返回 None。
        """
        collected: Dict[str, List[pd.DataFrame]] = {}
        for code in stocks:
            try:
                one = tq.get_market_data(
                    field_list=_FIELDS,
                    stock_list=[code],
                    period=period,
                    start_time=start,
                    end_time=end,
                    count=count,
                    dividend_type=dividend_type,
                    fill_data=True,
                )
            except Exception as e:
                self.skipped_stocks.append(code)
                logger.warning("跳过行情异常股票 %s(period=%s)：%s", code, period, e)
                continue
            if not isinstance(one, dict):
                continue
            for field in _FIELDS:
                sdf = one.get(field)
                if isinstance(sdf, pd.DataFrame) and sdf.shape[1] > 0:
                    collected.setdefault(field, []).append(sdf)
        if not collected:
            return None
        return {
            field: (dfs[0] if len(dfs) == 1 else pd.concat(dfs, axis=1))
            for field, dfs in collected.items()
        }

    def _subscribe(self, stocks, periods, callback):
        tq = get_tq()
        tq.subscribe_hq(stock_list=stocks, callback=callback)
