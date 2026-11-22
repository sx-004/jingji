"""数据源清单。

新闻走 RSSHub 公共实例的财经垂直路由（华尔街见闻全球快讯、第一财经简报、
财新、金十、格隆汇），行情走 Yahoo Finance chart 接口。两者免费、不用 key。
不与「5、热点事件」仓共用新闻now 热榜源，避免重叠；币圈完全交给「3、币圈新闻」。

代码表来自 Notion「6、全球经济监控」第七节，实测全部可读。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class RssRoute:
    id: str
    path: str
    label: str
    verified_at: str = "2026-11-18"
    enabled: bool = True


# 公共实例，前面的挂了自动换下一个；官方 rsshub.app 403，不用。
RSS_INSTANCES: List[str] = [
    "https://rsshub.rssforever.com",
    "https://rss.injahow.cn",
    "https://hub.slarker.me",
]

RSS_ROUTES: List[RssRoute] = [
    RssRoute("wscn-global", "/wallstreetcn/live/global", "华尔街见闻全球快讯"),
    RssRoute("yicai-brief", "/yicai/brief", "第一财经简报"),
    RssRoute("caixin-latest", "/caixin/latest", "财新"),
    RssRoute("jin10", "/jin10", "金十数据"),
    RssRoute("gelonghui-live", "/gelonghui/live", "格隆汇"),
]

EXCLUDED_RSS = {
    "cls-telegraph": "财联社 /cls/telegraph 在 rssforever 返回 503，不纳入；财联社走 newsnow 的 cls-telegraph",
    "rsshub.app": "官方公共实例 403 被挡",
    "rss.shab.fun": "连不上",
    "rsshub.henry.wang": "连不上",
    "rsshub.ovo.gay": "连不上",
}


@dataclass(frozen=True)
class QuoteSymbol:
    group: str
    name: str
    code: str
    enabled: bool = True


QUOTE_GROUPS: List[str] = ["美股", "欧股", "A股", "亚太", "商品", "汇率债"]

QUOTE_SYMBOLS: List[QuoteSymbol] = [
    QuoteSymbol("美股", "标普500", "^GSPC"),
    QuoteSymbol("美股", "道指", "^DJI"),
    QuoteSymbol("美股", "纳指", "^IXIC"),
    QuoteSymbol("美股", "费半", "^SOX"),
    QuoteSymbol("美股", "纳指100", "^NDX"),
    QuoteSymbol("欧股", "德国DAX", "^GDAXI"),
    QuoteSymbol("欧股", "英国富时", "^FTSE"),
    QuoteSymbol("欧股", "法国CAC", "^FCHI"),
    # A股单独一组（用户：分开点，一目了然）。五个代码 2026-11-18 在 Yahoo 全部实测可读。
    QuoteSymbol("A股", "上证", "000001.SS"),
    QuoteSymbol("A股", "深证", "399001.SZ"),
    QuoteSymbol("A股", "沪深300", "000300.SS"),
    QuoteSymbol("A股", "创业板", "399006.SZ"),
    QuoteSymbol("A股", "科创50", "000688.SS"),
    # 北证50：899050 / 899050.BJ / 899050.SS / ^BSE50 / ^BJ50 / 930599.CSI
    # 2026-11-18 逐个实测 Yahoo Finance 全返回 404，暂不纳入。
    QuoteSymbol("亚太", "恒生", "^HSI"),
    QuoteSymbol("亚太", "韩国KOSPI", "^KS11"),
    QuoteSymbol("亚太", "日经", "^N225"),
    QuoteSymbol("商品", "黄金", "GC=F"),
    QuoteSymbol("商品", "白银", "SI=F"),
    QuoteSymbol("商品", "原油", "CL=F"),
    QuoteSymbol("商品", "铜", "HG=F"),
    QuoteSymbol("汇率债", "美元指数", "DX-Y.NYB"),
    QuoteSymbol("汇率债", "欧元美元", "EURUSD=X"),
    QuoteSymbol("汇率债", "美债10年", "^TNX"),
    QuoteSymbol("汇率债", "VIX恐慌", "^VIX"),
]


def active_routes() -> List[RssRoute]:
    return [r for r in RSS_ROUTES if r.enabled]


def active_symbols() -> List[QuoteSymbol]:
    return [s for s in QUOTE_SYMBOLS if s.enabled]
