"""判定规则与推送格式。

两道关卡，都「有序、首个命中即停」，每条都记命中规则名，可审计：
  1) filter_reason：先挡掉不该推的（币圈、纯行情快讯、个股财报数字、非经济杂项、非中文、太短）。
  2) classify：剩下的按四类归类，首个命中的规则胜出。

阈值与关键词都写在这里，实推后直接改这里，不用动算法代码。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import lang

# ---- 运行与推送阈值 ----
MAX_ITEMS = 10             # 一轮最多推几条新闻（不含末尾行情看板），可配置
OVERLAP_MINUTES = 30       # 时间窗起点往前重叠，吸收 cron 延迟
NEWS_WINDOW_HOURS = 24     # 首次运行（没有 last_run）时往前看多久
DEDUP_RETENTION_DAYS = 7   # 推过的条目保留多久不重复推
FETCH_TIMEOUT = 25.0
FETCH_RETRIES = 1
MAX_WORKERS = 6
TITLE_MIN_LEN = 6

# ---- 同事件判定（阈值写在这里，可调） ----
SAME_EVENT_SIMILARITY = 0.6   # 中文二元组相似度阈值：共同二元组数 / 较短一方总数
SAME_EVENT_SAME_DAY = True    # 只在同一天（北京时间）内判同一件事
SAME_EVENT_MIN_DIFF = 3       # 归一化后只差这么多字 → 并列/相反事实，不合并
SAME_EVENT_MIN_TOKENS = 4     # 二元组太少的标题不参与相似度判断

# 四类，顺序即消息里的分组顺序；没内容的类别不显示
CATEGORIES: List[str] = ["央行", "重大事件", "公司", "政策/贸易"]

# ---- 挡掉：币圈 ----
CRYPTO_TERMS = (
    "比特币", "以太坊", "币圈", "加密货币", "加密资产", "虚拟货币", "稳定币",
    "数字资产", "区块链", "代币", "挖矿", "空投", "山寨币", "狗狗币",
    "币安", "DeFi", "NFT", "铭文", "链上", "减半", "现货ETF", " Meme ",
)
CRYPTO_TICKER_RE = re.compile(r"\b(?:BTC|ETH|USDT|USDC|DOGE|XRP|BNB|SOLANA)\b", re.I)
# 央行法定数字货币不是币圈，留给它本来该去的类别
CRYPTO_ALLOW = ("数字人民币", "法定货币", "央行数字货币", "数字欧元", "主权数字货币")

# ---- 挡掉：纯行情涨跌快讯 ----
# 有「行情动作 + 数字 + 指数/资产」而且没有事件信号，才当纯快讯挡掉。
QUOTE_MOVE_RE = re.compile(
    r"收涨|收跌|上涨|下跌|涨超|跌超|涨逾|跌逾|涨幅|跌幅|走高|走低|微涨|微跌|"
    r"扩大跌幅|收复|回落|反弹|暴跌|飙升|重挫|下挫|收涨超|收跌超|溃败|跌破|涨破|涨|跌"
)
QUOTE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|点)|收涨|收跌|跌超|涨超|微涨|微跌|下跌|上涨")
QUOTE_ASSET_RE = re.compile(
    r"纳指|道指|标普|费半|费城半导体|纳斯达克|德国DAX|DAX|英国富时|法国CAC|"
    r"恒生|恒指|韩国KOSPI|KOSPI|日经|上证|深证|沪深300|富时A50|A50|"
    r"美元指数|美元兑|USD/|美债|国债收益率|原油|油价|黄金|金价|白银|铜价|"
    r"布伦特|WTI|期指|期货|ETF|三大指数|三大股指|中概股|芯片股|科技股|"
    r"板块|夜盘|美股收盘|港股收盘|开盘|收盘|盘中|盘前|异动"
)
# 有这些词，说明是「事件」而不是碎片行情，不能挡
EVENT_SIGNAL_RE = re.compile(
    r"做空|浑水|香橼|逼空|财报|业绩报告|业绩|营收|净利|盈利|亏损|并购|收购|"
    r"减持|增持|清仓|调仓|暴雷|爆雷|违约|破产|重组|调查|监管|立案|处罚|"
    r"罚款|反垄断|造假|欺诈|关税|贸易|法案|政策|央行|美联储|降息|加息|"
    r"利率|出口管制|进口限制|制裁|上市|IPO|退市|回购|分红|裁员|董事会|"
    r"委员会|建厂|产能|市值|订单|签署|达成|公布|预计|拟|计划|"
    r"宣布|新规|发文|豁免|限制|巨震|闪崩|熔断"
)

# ---- 挡掉：个股财报数字细节（世界经济大事，不是单只股票的数字） ----
# 只有「数字型财报词」且没有大动作时，才当碎片挡掉。
EARNINGS_NUMBER_RE = re.compile(
    r"每股收益|每股盈利|每股派|\bEPS\b|净利润|净利|营收|营业收入|归母净利|"
    r"回购.{0,30}(?:股|票据|债券)|股息|派息"
)
# 大动作优先：并购、重大业务变化、暴雷、重大人事——有这些就不是碎片数字。
# 注意：不能把“董事长/CEO/董事会”这些头衔词本身当大动作，
# 否则“董事长提议回购股份”这类碎消息会被当成重大人事放行。
BIG_ACTION_RE = re.compile(
    r"并购|收购|合并|分拆|重组|破产|暴雷|爆雷|违约|退市|上市|\bIPO\b|"
    r"裁员|辞职|离职|辞任|卸任|任命|接任|换帅|罢免|"
    r"砍半|腰斩|关停|停产|剥离|出售|私有化|罢工|召回|罚款|处罚|调查"
)


def is_stock_earnings_detail(title: str) -> bool:
    """个股财报数字细节：每股收益/营收/净利润/回购股数这类，没有大动作就挡掉。"""
    if not EARNINGS_NUMBER_RE.search(title):
        return False
    return not BIG_ACTION_RE.search(title)


# ---- 挡掉：民调/调查类软文（不是经济事件） ----
SURVEY_RE = re.compile(r"调查显示|调查称|调查发现|调查：|花旗调查|盖洛普|民调|问卷")


# ---- 挡掉：跟经济无关 ----
NON_ECONOMIC_RE = re.compile(
    r"议会议长|当选|选举|大选|换届|就职|使馆|领事馆|外交|会晤|首相|总统|"
    r"示威|游行|抗议|政变|金正恩|视察|气候大会|滑雪|演唱会|明星|电影票房|"
    r"国足|奥运|世界杯|台风|地震|天气|旅游区"
)
ECONOMIC_SIGNAL_RE = re.compile(
    r"关税|贸易|经贸|经济|市场|股市|股市|央行|美联储|利率|通胀|GDP|"
    r"油价|原油|能源|投资|制裁|出口|进口|财政|债务|产业|芯片|半导体"
)


@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    patterns: Tuple[str, ...]


# 有序，首个命中即停。跨类冲突时按 Notion 的类别顺序：央行 → 重大事件 → 公司 → 政策/贸易。
RULES: List[Rule] = [
    Rule("cb-fed", "央行", (r"美联储", r"联邦基金", r"FOMC", r"议息", r"鲍威尔",
                            r"威廉姆斯", r"纽约联储", r"点阵图", r"前瞻指引")),
    Rule("cb-policy", "央行", (r"央行", r"货币政策", r"降息", r"加息", r"基准利率",
                              r"利率决议", r"量化宽松", r"\bQE\b", r"\bQT\b",
                              r"缩表", r"扩表", r"流动性")),
    Rule("cb-bonds", "央行", (r"债券供给", r"市政债", r"国债发行", r"国债供给",
                             r"美债", r"国债收益率")),

    Rule("ev-short", "重大事件", (r"做空", r"浑水", r"香橼", r"逼空", r"大空头")),
    Rule("ev-investor", "重大事件", (r"减持", r"清仓", r"桥水", r"索罗斯", r"分歧",
                                    r"调仓", r"抛售", r"大佬")),
    Rule("ev-shock", "重大事件", (r"暴雷", r"爆雷", r"违约", r"破产", r"债务重组",
                                 r"巨震", r"闪崩", r"熔断", r"崩盘", r"溃败",
                                 r"监管突袭", r"突击检查", r"立案", r"调查",
                                 r"反垄断", r"财务造假", r"欺诈", r"SEC", r"证交会")),

    Rule("co-earnings", "公司", (r"财报", r"业绩报告", r"营收", r"净利润", r"净利",
                                r"季报", r"年报", r"每股收益", r"盘后公布", r"业绩")),
    Rule("co-action", "公司", (r"并购", r"收购", r"合并", r"分拆", r"上市", r"\bIPO\b",
                              r"退市", r"回购", r"分红", r"裁员", r"董事会",
                              r"辞职", r"离职", r"罢免", r"任命", r"接任", r"CEO",
                              r"董事长", r"总裁", r"高管",
                              r"数据中心", r"建厂", r"产能", r"订单", r"新业务",
                              r"市值(?:跌破|突破|超过?|达|蒸|缩)|总市值")),

    Rule("pol-trade", "政策/贸易", (r"关税", r"贸易", r"出口管制", r"进口限制", r"制裁",
                                   r"反倾销", r"豁免", r"配额", r"\bAPEC\b", r"\bWTO\b",
                                   r"\bG20\b", r"自贸", r"经贸")),
    Rule("pol-fiscal", "政策/贸易", (r"法案", r"财政", r"预算", r"支出", r"税收", r"减税",
                                    r"加税", r"国会", r"参议院", r"众议院", r"产业政策",
                                    r"补贴", r"发文", r"新规", r"指导意见", r"部委",
                                    r"医保")),
]

_COMPILED = [(rule, re.compile("|".join(f"(?:{p})" for p in rule.patterns), re.I))
             for rule in RULES]


def is_crypto(title: str) -> bool:
    if any(term in title for term in CRYPTO_ALLOW):
        return False
    if any(term.lower() in title.lower() for term in CRYPTO_TERMS):
        return True
    return bool(CRYPTO_TICKER_RE.search(title))


REACTION_RE = re.compile(
    r"盘后.{0,12}(?:拉升|涨超|跌超|上涨|下跌|大涨|大跌|跳水)|"
    r"盘前.{0,12}(?:涨|跌)|财报公布后.{0,12}(?:拉升|涨超|跌超)"
)
REACTION_DETAIL_RE = re.compile(r"营收|净利|预期|每股收益|毛利率|展望")
# 一句话里堆满行情词、又没有动作动词，就算带「美联储」也是碎片行情
STRONG_QUOTE_RE = re.compile(r"期货|股指|指数|涨幅|跌幅|回吐|收涨|收跌|上涨|下跌|走高|走低")
NO_ACTION_RE = re.compile(
    r"宣布|决定|通过|签署|发布|公布|调查|做空|并购|收购|财报|立案|制裁|法案|"
    r"降息|加息|维持|警告|预计|拟|计划"
)


def is_pure_market_quote(title: str) -> bool:
    """纯行情涨跌快讯：有行情动作和数字，且落在指数/资产上，又没有事件信号。"""
    # 财报后的盘后拉升/跳水只是价格反应，不是财报事实本身
    if REACTION_RE.search(title) and not REACTION_DETAIL_RE.search(title):
        return True
    # 「美联储…股指期货回吐涨幅…上涨0.1%」这类带宏观词的行情也挡
    if len(STRONG_QUOTE_RE.findall(title)) >= 2 and not NO_ACTION_RE.search(title):
        return True
    if EVENT_SIGNAL_RE.search(title):
        return False
    if not QUOTE_MOVE_RE.search(title):
        return False
    if not QUOTE_NUMBER_RE.search(title):
        return False
    return bool(QUOTE_ASSET_RE.search(title))


def is_non_economic(title: str) -> bool:
    if not NON_ECONOMIC_RE.search(title):
        return False
    return not ECONOMIC_SIGNAL_RE.search(title)


def filter_reason(title: str) -> str:
    """返回挡掉原因；空串表示通过。有序、首个命中即停。"""
    text = lang.clean_title(title)
    if len(text) < TITLE_MIN_LEN:
        return "too_short"
    if not lang.is_chinese(text):
        return "not_chinese"
    if is_crypto(text):
        return "crypto"
    if is_pure_market_quote(text):
        return "market_quote"
    if is_stock_earnings_detail(text):
        return "earnings_detail"
    if SURVEY_RE.search(text):
        return "survey"
    if is_non_economic(text):
        return "non_economic"
    return ""


def same_event(left: str, left_ms: int, right: str, right_ms: int) -> bool:
    """同一件事的综合判定（标题层面）。

    信号任一命中即合并：
    - 归一化后完全相同；
    - 共同实体前缀 + 够长的共同主题词（同一份文件被两家各报一次）；
    - 同一天 + 中文二元组相似度超过阈值（纯标准库，不装分词库）。
    先挡掉“看似相似但不是同一件事”的：相反词、数字不同、季度/地区不同。
    """
    a, b = lang.normalize_title(left), lang.normalize_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if lang.opposing(left, right):
        return False
    if lang.conflicting(left, right):
        return False
    if lang.same_title(left, right):
        return True
    if not SAME_EVENT_SAME_DAY:
        return False
    if lang.day_key(left_ms) != lang.day_key(right_ms):
        return False
    if min(len(lang.bigrams(left)), len(lang.bigrams(right))) < SAME_EVENT_MIN_TOKENS:
        return False
    return lang.bigram_similarity(left, right) >= SAME_EVENT_SIMILARITY


def more_complete(left, right) -> bool:
    """返回 True 表示 left 信息更全，应当保留（标题更长 = 细节更多）。"""
    left_title = len(lang.clean_title(getattr(left, "title", "")))
    right_title = len(lang.clean_title(getattr(right, "title", "")))
    if left_title != right_title:
        return left_title > right_title
    left_summary = len(getattr(getattr(left, "item", None), "summary", "") or "")
    right_summary = len(getattr(getattr(right, "item", None), "summary", "") or "")
    if left_summary != right_summary:
        return left_summary > right_summary
    left_ms = getattr(getattr(left, "item", None), "published_ms", 0) or 0
    right_ms = getattr(getattr(right, "item", None), "published_ms", 0) or 0
    return left_ms <= right_ms


def classify(title: str) -> Tuple[str, str]:
    """返回 (类别, 命中规则名)；归不了类返回 ("", "")。首个命中即停。"""
    text = lang.clean_title(title)
    for rule, pattern in _COMPILED:
        if pattern.search(text):
            return rule.category, rule.name
    return "", ""


def format_item_line(item) -> str:
    """每条一行：标题 — 原文链接。链接裸写，Telegram 会自动识别为可点。"""
    title = lang.clean_title(item.title).replace("\n", " ").strip()
    url = str(item.url or "").strip()
    return f"{title} — {url}" if url else title


def build_message(items, board_lines: List[str]) -> str:
    """按类别分组，空类别不显示；末尾固定行情看板。"""
    lines = [f"全球经济 · 本轮 {len(items)} 条", ""]
    for category in CATEGORIES:
        group = [it for it in items if getattr(it, "category", "") == category]
        if not group:
            continue
        lines.append(f"【{category}】")
        lines.extend(format_item_line(it) for it in group)
        lines.append("")
    # 保证有且只有一个看板标题：format_board 自带，手工传入的样例可能不带
    if not board_lines or not str(board_lines[0]).startswith("【行情走向】"):
        lines.append("【行情走向】")
    lines.extend(board_lines)
    return "\n".join(lines).strip()
