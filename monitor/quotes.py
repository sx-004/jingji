"""Yahoo Finance 行情看板：只算百分比涨跌，不给点位、不给 K 线、不做买卖建议。

range=5d 的 chartPreviousClose 是「5 天区间开始前一天」的收盘价，拿它算出来的是
5 天累计涨跌，不是单日涨跌。单日涨跌必须用 indicators.quote[0].close 里最后两个
有效收盘价（最后一个=最新，倒数第二个=上一交易日）。
单个标的失败只跳过它，不让整块看板挂掉；全标失败单独记在报告里。
"""
from __future__ import annotations

import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import http, rules, sources

CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart/{code}?interval=1d&range=5d"


@dataclass
class Quote:
    group: str
    name: str
    code: str
    price: float = 0.0
    previous_close: float = 0.0
    change_pct: float = 0.0
    ok: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "group": self.group, "name": self.name, "code": self.code,
            "price": round(self.price, 4), "previous_close": round(self.previous_close, 4),
            "change_pct": round(self.change_pct, 2), "ok": self.ok, "error": self.error[:200],
        }


def quote_url(code: str) -> str:
    return CHART_API.format(code=urllib.parse.quote(code, safe=""))


def single_day_change(closes, *, chart_previous_close=None,
                      regular_market_price=None) -> Tuple[float, float, float, str]:
    """日线 close 数组 → (最新价, 昨收, 单日涨跌%, 错误)。

    正常情况（≥2 个有效收盘）：倒数第二个是上一交易日，最后一个是最新，
    此时 chartPreviousClose 是「5 天区间开始前一天」的价，绝不能用。

    例外：Yahoo 对部分 A 股指数整段只返回 1 条收盘（实测沪深300/创业板/科创50）。
    这时第一条就是最新交易日，chartPreviousClose 恰好是它前一个交易日的收盘
    （已用交易所昨收独立核对），用它算单日涨跌是对的；仅此一种情况用回退。
    """
    valid: List[float] = []
    for raw in closes or []:
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            valid.append(value)
    if len(valid) >= 2:
        last, previous = valid[-1], valid[-2]
        return last, previous, (last - previous) / previous * 100.0, ""
    if len(valid) == 1:
        try:
            previous = float(chart_previous_close)
        except (TypeError, ValueError):
            previous = 0.0
        if previous <= 0:
            return 0.0, 0.0, 0.0, "只有一条日线收盘且没有昨收，无法算单日涨跌"
        try:
            price = float(regular_market_price)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            price = valid[0]
        return price, previous, (price - previous) / previous * 100.0, ""
    return 0.0, 0.0, 0.0, "日线收盘有效值不足，无法算单日涨跌"


def parse_chart(payload: dict, symbol: sources.QuoteSymbol) -> Quote:
    quote = Quote(group=symbol.group, name=symbol.name, code=symbol.code)
    try:
        chart = payload["chart"]
        if chart.get("error"):
            err = chart["error"]
            desc = err.get("description", "chart error") if isinstance(err, dict) else str(err)
            quote.error = str(desc)[:200]
            return quote
        result = chart["result"][0]
        meta = result.get("meta") or {}
        indicators = result.get("indicators") or {}
        quote_series = indicators.get("quote") or []
        closes = quote_series[0].get("close") if quote_series else []
        price, previous, pct, error = single_day_change(
            closes,
            chart_previous_close=meta.get("chartPreviousClose"),
            regular_market_price=meta.get("regularMarketPrice"),
        )
        if error:
            quote.error = error
            return quote
        quote.price = price
        quote.previous_close = previous
        quote.change_pct = pct
        quote.ok = True
        return quote
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        quote.error = f"字段解析失败: {type(exc).__name__}: {exc}"[:200]
        return quote


def fetch_quote(symbol: sources.QuoteSymbol, *, timeout: float = 20.0, retries: int = 1) -> Quote:
    result = http.fetch(quote_url(symbol.code), timeout=timeout, retries=retries)
    if not result.ok:
        return Quote(group=symbol.group, name=symbol.name, code=symbol.code,
                     error=f"{result.error_type}: {result.error[:160]}")
    try:
        return parse_chart(json.loads(result.text("utf-8")), symbol)
    except ValueError as exc:
        return Quote(group=symbol.group, name=symbol.name, code=symbol.code,
                     error=f"JSON 解析失败: {exc}"[:200])


def fetch_board(*, timeout: float = 20.0, retries: int = 1,
                symbols: Optional[List[sources.QuoteSymbol]] = None) -> Tuple[List[Quote], List[Quote]]:
    """返回 (全部标的, 失败标的)。并发抓，单标失败不影响其他。"""
    active = list(symbols if symbols is not None else sources.active_symbols())
    if not active:
        return [], []
    with ThreadPoolExecutor(max_workers=rules.MAX_WORKERS) as pool:
        quotes = list(pool.map(
            lambda s: fetch_quote(s, timeout=timeout, retries=retries), active,
        ))
    return quotes, [q for q in quotes if not q.ok]


def format_change(pct: float) -> str:
    return f"{pct:+.2f}%"


def format_board(quotes: List[Quote]) -> List[str]:
    """看板行：按分组顺序，每组一行；没数据的分组不显示。"""
    ok_quotes = [q for q in quotes if q.ok]
    lines = ["【行情走向】"]
    if not ok_quotes:
        lines.append("行情数据暂不可用")
        return lines
    for group in sources.QUOTE_GROUPS:
        cells = [f"{q.name} {format_change(q.change_pct)}" for q in ok_quotes if q.group == group]
        if cells:
            lines.append(f"{group}：" + "｜".join(cells))
    return lines
