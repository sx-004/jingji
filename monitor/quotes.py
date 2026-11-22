"""Yahoo Finance 行情看板：只算百分比涨跌，不给点位、不给 K 线、不做买卖建议。

取 regularMarketPrice 与 chartPreviousClose 算涨跌百分比（实测与 Notion 样例一致）。
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
        meta = result["meta"]
        price = float(meta["regularMarketPrice"])
        previous = meta.get("chartPreviousClose")
        if previous is None:
            previous = meta.get("previousClose")
        previous = float(previous)
        if previous == 0:
            quote.error = "昨收为 0，无法计算涨跌"
            return quote
        quote.price = price
        quote.previous_close = previous
        quote.change_pct = (price - previous) / previous * 100.0
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
