"""文本工具：中文判定、HTML 清洗、URL/标题归一化、中文二元组相似度。

二元组（bigram）不装分词库：中文按相邻两字切，英文按单词切并去停用词，
数字与百分比整体保留——纯标准库就能算相似度。
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
EFFECTIVE_RE = re.compile(r"[\u4e00-\u9fff0-9A-Za-z]")
TAG_RE = re.compile(r"<[^>]+>")
PREFIX_RE = re.compile(r"^(?:格隆汇|财联社|华尔街见闻|第一财经|金十数据)\s*\d{1,2}月\d{1,2}日\s*[｜|]\s*")
PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

CHINESE_MIN_RATIO = 0.30
CHINESE_MIN_CHARS = 2
CHINESE_STRONG_CHARS = 6
SAME_TITLE_RATIO = 0.95
SAME_TITLE_TOPIC_LEN = 7

CST = timezone(timedelta(hours=8))

# 英文停用词（相似度计算时丢掉）
STOPWORDS = {"a", "an", "the", "of", "to", "in", "for", "on", "at", "by", "and", "is",
             "are", "was", "were", "be", "as", "that", "this", "it", "with", "from"}
# 一个 token 的构成：中文连续段 / 英文单词（可带数字）/ 数字与百分比
TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+[A-Za-z0-9]*|\d+(?:[.,]\d+)?%?")
# 追踪参数：utm_*、ref、spm、from、share_source 等
TRACKING_PREFIX = ("utm", "spm", "from", "share", "ref", "gclid", "fbclid", "mc_cid", "mc_eid")
TRACKING_EXACT = ("referer", "referrer", "source", "cmpid", "icid", "spm_id")

# 相反词：一条出现前者、另一条出现后者 → 不是同一件事
OPPOSING_PAIRS = (
    ("加息", "降息"), ("上涨", "下跌"), ("涨价", "降价"), ("增产", "减产"),
    ("增持", "减持"), ("加仓", "减仓"), ("上调", "下调"), ("扩表", "缩表"),
    ("偏鹰", "偏鸽"), ("鹰派", "鸽派"), ("净流入", "净流出"),
    ("买入", "卖出"), ("扩大", "收窄"), ("上升", "下降"), ("回暖", "转冷"),
)
# 地区/国家：用于判断是不是同一件事
REGION_TOKENS = (
    "中国", "美国", "英国", "法国", "德国", "日本", "韩国", "印度", "俄罗斯", "欧盟",
    "欧元区", "意大利", "西班牙", "加拿大", "澳大利亚", "巴西", "墨西哥", "越南",
    "新加坡", "香港", "台湾", "荷兰", "瑞士", "瑞典", "挪威", "沙特", "以色列",
    "土耳其", "印尼", "泰国", "南非", "阿根廷", "智利", "希腊", "爱尔兰", "波兰",
    "乌克兰", "新西兰", "孟加拉", "巴基斯坦", "埃及", "尼日利亚", "肯尼亚",
)
# 数字与百分比、季度/半年：这些不同就不是同一件事
NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
QUARTER_TOKEN_RE = re.compile(r"第[一二三四1-4]季度|[一二三四1-4]季度|Q[1-4]|上半年|下半年")


def chinese_stats(text: str) -> tuple[int, float]:
    """返回 (中文字符数, 中文占有效字符比例)。"""
    value = text or ""
    count = len(CJK_RE.findall(value))
    effective = len(EFFECTIVE_RE.findall(value))
    ratio = count / effective if effective else 0.0
    return count, round(ratio, 4)


def is_chinese(text: str) -> bool:
    count, ratio = chinese_stats(text)
    if count >= CHINESE_STRONG_CHARS:
        return True
    return count >= CHINESE_MIN_CHARS and ratio >= CHINESE_MIN_RATIO


def clean_title(text: str) -> str:
    """去掉 HTML 标签、实体和源前缀，压掉多余空白。"""
    value = html.unescape(str(text or ""))
    value = TAG_RE.sub("", value)
    value = PREFIX_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(text: str) -> str:
    """标题归一化：清 HTML、去前缀、去标点空白、小写。用于去重与合并。"""
    value = clean_title(text).lower()
    return PUNCT_RE.sub("", value)


def _is_tracking(key: str) -> bool:
    name = (key or "").lower()
    if name in TRACKING_EXACT:
        return True
    return any(name == p or name.startswith(p + "_") or name.startswith(p + "-")
               for p in TRACKING_PREFIX)


def normalize_url(url: str) -> str:
    """链接归一化（治重复推送的第一道防线）。

    - 去掉 #片段
    - 去掉尾斜杠
    - 去掉追踪参数（utm_*、ref、spm、from、share_source 等）
    - 域名小写、去掉 www.、去掉默认端口
    - http 与 https 视为同一个
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        p = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (p.netloc or "").lower()
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not _is_tracking(k)]
    query.sort()
    path = re.sub(r"/+$", "", p.path) or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def similarity(left: str, right: str) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _matching_blocks(a: str, b: str):
    return [(m.a, m.b, m.size) for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size]


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def shared_topic_len(left: str, right: str) -> int:
    """去掉两端共同前缀（实体名）后，最长的一段共同实词有多长。

    用来判断「同一份文件/同一件事被两家各报一次」：
    「国家能源局：探索流域水风光一体化与绿色氢氨醇协同发展」和
    「国家能源局印发《关于加快推动流域水风光一体化发展的指导意见》」，
    共同前缀是「国家能源局」，剩下共同的「流域水风光一体化」（8 字）就是同一件事。
    """
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0
    blocks = _matching_blocks(a, b)
    if not blocks:
        return 0
    non_prefix = [blk for blk in blocks if not (blk[0] == 0 and blk[1] == 0)]
    candidates = non_prefix or blocks
    return max(blk[2] for blk in candidates)


def opposing(left: str, right: str) -> bool:
    """两个标题里有相反词（加息/降息、增产/减产…）→ 不是同一件事。"""
    a, b = normalize_title(left), normalize_title(right)
    for x, y in OPPOSING_PAIRS:
        if (x in a and y in b) or (y in a and x in b):
            return True
    return False


def discriminators(text: str) -> dict:
    """标题里的区分性信息：数字、季度/半年、地区。"""
    value = normalize_title(text)
    return {
        "num": set(NUM_TOKEN_RE.findall(value)),
        "period": set(QUARTER_TOKEN_RE.findall(value)),
        "region": set(region for region in REGION_TOKENS if region in value),
    }


def conflicting(left: str, right: str) -> bool:
    """两边都带了同一类区分信息、但内容不同 → 不是同一件事。

    一边有数字/地区、另一边没写，不算冲突（只是详略不同）。
    """
    dl, dr = discriminators(left), discriminators(right)
    for key in dl:
        if dl[key] and dr[key] and dl[key] != dr[key]:
            return True
    return False


def minimal_pair(left: str, right: str) -> bool:
    """归一化后长度相同、只差 1~3 个字：多半是相反或并列的不同事实（加息/降息、参议院/众议院）。"""
    a, b = normalize_title(left), normalize_title(right)
    if not a or len(a) != len(b):
        return False
    diff = sum(1 for x, y in zip(a, b) if x != y)
    return 1 <= diff <= 3


def bigrams(text: str) -> set:
    """中文按相邻两字切二元组，英文按单词切并去停用词，数字与百分比整体保留。"""
    tokens = set()
    for match in TOKEN_RE.finditer((text or "").lower()):
        token = match.group(0)
        if "\u4e00" <= token[0] <= "\u9fff":
            if len(token) == 1:
                tokens.add(token)
            else:
                for i in range(len(token) - 1):
                    tokens.add(token[i:i + 2])
        elif token[0].isdigit():
            tokens.add(token)
        elif token not in STOPWORDS:
            tokens.add(token)
    return tokens


def bigram_similarity(left: str, right: str) -> float:
    """共同二元组数 / 较短一方总数。"""
    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def day_key(ms: int) -> str:
    """毫秒时间戳 → 北京时间日期（YYYY-MM-DD），用于「同一天才算同一件事」。"""
    if not ms:
        return ""
    try:
        stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CST)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.strftime("%Y-%m-%d")


def same_title(left: str, right: str) -> bool:
    """标题层面的同事件判定（不含二元组规则，由 rules.same_event 统一编排）。

    先挡掉“看似相似但不是同一件事”的三种情况：
    - 相反词（加息/降息、增产/减产）；
    - 数字不同（3.1% vs 3.2%）；
    - 季度或地区不同（第三季度 vs 第四季度、法国 vs 德国）。
    剩下的再按：完全相同 / 浓缩版 / 高度相似 / 共同实体前缀 + 共同主题词判断。
    """
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if opposing(left, right):
        return False
    if conflicting(left, right):
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # 一条是另一条的浓缩版（同一件事不同写法）
    if len(shorter) >= 7 and shorter in longer:
        return True
    # 去标点后整体高度相似
    if abs(len(a) - len(b)) <= max(len(a), len(b)) * 0.35:
        if SequenceMatcher(None, a, b).ratio() >= SAME_TITLE_RATIO:
            return True
    prefix, topic = common_prefix_len(a, b), shared_topic_len(a, b)
    # 同一个实体/主体，且共同主题词够长（同一份文件被两家各报一次）
    if prefix >= 4 and topic >= SAME_TITLE_TOPIC_LEN:
        return True
    # 主题词本身够长，即使主体前缀不同也当作同一件事（如「求豁免欧盟混动汽车进口限制」）
    return topic >= 10
