"""RSSHub 财经路由：RSS2/Atom 解析、多实例回退、按上次运行时间过滤。

只取「上一轮之后」的新内容；起点往前留约 30 分钟重叠吸收 cron 延迟
（重叠由 run.py 计算 since_ms 后传进来）。
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

from . import http, lang, sources

ATOM_NS = "{http://www.w3.org/2005/Atom}"
TS_RE = re.compile(r"^\d{10,13}$")
CN_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")


@dataclass
class NewsItem:
    source: str
    label: str
    title: str
    url: str
    published_ms: int = 0
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source, "label": self.label, "title": self.title,
            "url": self.url, "published_ms": self.published_ms, "summary": self.summary[:200],
        }


@dataclass
class RouteResult:
    name: str
    label: str
    ok: bool
    status: str
    error: str = ""
    items: List[NewsItem] = field(default_factory=list)
    instance: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "ok": self.ok,
            "state": route_state(self),
            "status": self.status, "error": self.error[:300], "instance": self.instance,
            "item_count": len(self.items), "duration_ms": self.duration_ms,
        }


def route_state(result) -> str:
    """源状态：ok=有数据，empty=请求成功但 0 条（正常，不算故障），failure=抓取失败。"""
    if not result.ok:
        return "failure"
    return "ok" if result.items else "empty"


def parse_pubdate(value: Optional[str]) -> int:
    """各种 pubDate 形式 → 毫秒时间戳；解析不出来返回 0（视为无时间，会被时间窗挡掉）。"""
    raw = str(value or "").strip()
    if not raw:
        return 0
    if TS_RE.match(raw):
        number = int(raw)
        return number if number > 10_000_000_000 else number * 1000
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        pass
    match = CN_TIME_RE.match(raw)
    if match:
        try:
            from datetime import timedelta
            parts = [int(x or 0) for x in match.groups()]
            cst = timezone(timedelta(hours=8))
            dt = datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=cst)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def _node_text(node, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text
    return ""


def parse_feed(xml_text: str, route: sources.RssRoute) -> List[NewsItem]:
    """解析 RSS2/Atom。结构整体坏了抛 ValueError，交给上层换实例。"""
    if not xml_text or "<" not in xml_text:
        raise ValueError("RSS 响应不是 XML")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"RSS XML 解析失败: {exc}") from exc

    nodes = root.findall(".//item") or root.findall(f".//{ATOM_NS}entry")
    if not nodes:
        # 有的实例返回 rss 但没有 item（例如限流页面），当成空结果，不当成功
        raise ValueError("RSS 无 item 节点（可能是限流或空 feed）")

    items: List[NewsItem] = []
    for node in nodes:
        title = lang.clean_title(_node_text(node, "title", f"{ATOM_NS}title"))
        link = _node_text(node, "link", f"{ATOM_NS}link")
        if not link:
            link_el = node.find(f"{ATOM_NS}link")
            if link_el is not None:
                link = str(link_el.get("href") or "")
        link = str(link).strip()
        if not title or not link:
            continue
        pub = _node_text(node, "pubDate", "pubdate", f"{ATOM_NS}published", f"{ATOM_NS}updated", "date")
        summary = lang.clean_title(_node_text(node, "description", f"{ATOM_NS}summary"))
        items.append(NewsItem(
            source=route.id, label=route.label, title=title, url=link,
            published_ms=parse_pubdate(pub), summary=summary,
        ))
    return items


def fetch_route(route: sources.RssRoute, *, since_ms: int, now_ms: Optional[int] = None,
                timeout: float = 25.0, retries: int = 1,
                instances: Optional[List[str]] = None) -> RouteResult:
    """逐个公共实例尝试；第一个解析成功的实例胜出。全挂才返回 ok=False。"""
    started = time.time()
    stamp = now_ms if now_ms is not None else int(time.time() * 1000)
    tried: List[str] = []
    last_status = ""
    last_error = ""
    for base in (instances or sources.RSS_INSTANCES):
        base = base.rstrip("/")
        tried.append(base)
        url = f"{base}{route.path}"
        result = http.fetch(url, timeout=timeout, retries=retries)
        if not result.ok:
            last_status = result.error_type or "provider_error"
            last_error = f"{base}: {result.error[:160]}"
            continue
        try:
            parsed = parse_feed(result.text("utf-8"), route)
        except ValueError as exc:
            last_status = "bad_payload"
            last_error = f"{base}: {exc}"
            continue
        fresh = [it for it in parsed if it.published_ms and since_ms <= it.published_ms <= stamp + 5 * 60_000]
        return RouteResult(
            name=route.id, label=route.label, ok=True, status="ok",
            items=fresh, instance=base, duration_ms=int((time.time() - started) * 1000),
        )
    return RouteResult(
        name=route.id, label=route.label, ok=False, status=last_status or "all_instances_failed",
        error=last_error or "全部实例失败：" + "、".join(tried),
        duration_ms=int((time.time() - started) * 1000),
    )
