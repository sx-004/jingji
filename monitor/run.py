"""编排：抓源 → 时间窗 → 四类判定 → 挡杂项 → 跨源合并 → 去重 → 一条消息 → 干跑/推送 → 状态。

退出码：0=正常/无新增静默，1=部分源失败已隔离，2=全源失败（单独告警），5=配置或推送问题。
工作流里 >1 才失败；源单实例挂了自动换下一个，个别路由挂了静默隔离，不刷屏。
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import lang, quotes, rules, rsshub, sources, state as state_mod, telegram

EXIT_OK, EXIT_PARTIAL, EXIT_ALL_FAILED, EXIT_CONFIG = 0, 1, 2, 5


@dataclass
class Decision:
    item: rsshub.NewsItem
    category: str
    rule: str
    sources: List[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.item.title

    @property
    def url(self) -> str:
        return self.item.url

    def to_dict(self) -> dict:
        payload = self.item.to_dict()
        payload.update({"category": self.category, "rule": self.rule, "sources": self.sources})
        return payload


def _now_ms() -> int:
    return int(time.time() * 1000)


def fetch_all(since_ms: int, now_ms: int) -> List[rsshub.RouteResult]:
    """每个财经路由只拉一次，路由内部按实例顺序回退。"""
    active = sources.active_routes()
    with ThreadPoolExecutor(max_workers=rules.MAX_WORKERS) as pool:
        return list(pool.map(
            lambda route: rsshub.fetch_route(
                route, since_ms=since_ms, now_ms=now_ms,
                timeout=rules.FETCH_TIMEOUT, retries=rules.FETCH_RETRIES,
            ),
            active,
        ))


def _fixture_quote(row: dict) -> quotes.Quote:
    price = float(row.get("price") or 0)
    previous = float(row.get("previous_close") or 0)
    pct = (price - previous) / previous * 100.0 if previous else 0.0
    return quotes.Quote(
        group=str(row.get("group") or "美股"), name=str(row.get("name") or "?"),
        code=str(row.get("code") or "?"), price=price, previous_close=previous,
        change_pct=pct, ok=bool(row.get("ok", True)),
        error=str(row.get("error") or ""),
    )


def _load_fixtures(path: str) -> Tuple[List[rsshub.RouteResult], List[quotes.Quote]]:
    """离线样例：{"routes":[{source,label,items:[{title,url,published_ms}]}],"quotes":[...]}"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    results: List[rsshub.RouteResult] = []
    for row in raw.get("routes", []):
        route = sources.RssRoute(str(row.get("source", "fixture")),
                                 str(row.get("path", "/fixture")),
                                 str(row.get("label", "样例")))
        items = [
            rsshub.NewsItem(
                source=route.id, label=route.label,
                title=lang.clean_title(str(entry.get("title") or "")),
                url=str(entry.get("url") or ""),
                published_ms=int(entry.get("published_ms") or 0),
                summary=str(entry.get("summary") or ""),
            )
            for entry in row.get("items", [])
        ]
        results.append(rsshub.RouteResult(name=route.id, label=route.label, ok=True,
                                          status="fixture", items=items, instance="fixture"))
    board = [_fixture_quote(row) for row in raw.get("quotes", [])]
    return results, board


def _merge_same_event(decisions: List[Decision], stats: Dict[str, int]) -> List[Decision]:
    """两道防线的同事件去重。

    第一道：归一化后的 URL 相同 → 同一篇，只留信息最全的一条。
    第二道：标题相似度（同一天 + 中文二元组阈值）→ 同一件事，只留信息最全的一条。
    """
    merged: List[Decision] = []
    by_url: Dict[str, Decision] = {}

    # 第一道：URL 归一化后相同
    for decision in decisions:
        key = lang.normalize_url(decision.url)
        if key and key in by_url:
            previous = by_url[key]
            keep = previous if rules.more_complete(previous, decision) else decision
            drop = decision if keep is previous else previous
            if drop.item.label not in keep.sources:
                keep.sources.append(drop.item.label)
            stats["duplicated_url"] = stats.get("duplicated_url", 0) + 1
            by_url[key] = keep
            merged = [keep if item is previous else item for item in merged]
        else:
            merged.append(decision)
            if key:
                by_url[key] = decision

    # 第二道：标题相似度
    result: List[Decision] = []
    for decision in merged:
        hit = None
        for candidate in result:
            if rules.same_event(candidate.title, candidate.item.published_ms,
                                decision.title, decision.item.published_ms):
                hit = candidate
                break
        if hit is None:
            result.append(decision)
            continue
        keep = hit if rules.more_complete(hit, decision) else decision
        drop = decision if keep is hit else hit
        if drop.item.label not in keep.sources:
            keep.sources.append(drop.item.label)
        stats["duplicated"] = stats.get("duplicated", 0) + 1
        result = [keep if item is hit else item for item in result]
    return result


def _collect(reports: List[rsshub.RouteResult], since_ms: int, stamp: int,
             stats: Dict[str, int]) -> List[Decision]:
    pool: List[Decision] = []
    for report in reports:
        if not report.ok:
            continue
        for item in report.items:
            stats["raw_items"] += 1
            if not item.published_ms:
                stats["no_timestamp"] += 1
                continue
            if item.published_ms < since_ms:
                stats["stale"] += 1
                continue
            if item.published_ms > stamp + 5 * 60_000:
                stats["future"] += 1
                continue
            reason = rules.filter_reason(item.title)
            if reason:
                stats[reason] = stats.get(reason, 0) + 1
                continue
            category, rule_name = rules.classify(item.title)
            if not category:
                stats["unclassified"] += 1
                continue
            pool.append(Decision(item=item, category=category, rule=rule_name,
                                 sources=[item.label]))
    return _merge_same_event(pool, stats)


def _push(token: str, chat_id: str, message: str, selected: List[Decision],
          st: state_mod.State) -> bool:
    """先重推 pending，再推本轮。任何一步失败都进 pending，不静默丢条目。"""
    if st.pending:
        remaining = []
        for entry in st.pending:
            try:
                telegram.send_message(token, chat_id, str(entry.get("message") or ""))
            except telegram.PushError as exc:
                print(f"待重推消息仍失败：{exc}", file=sys.stderr)
                remaining.append(entry)
        st.pending = remaining
        if remaining:
            if selected:
                st.add_pending(message, "本轮新内容，等旧消息重推成功后再发")
            return False
    try:
        telegram.send_message(token, chat_id, message)
    except telegram.PushError as exc:
        print(f"Telegram 推送失败：{exc}", file=sys.stderr)
        print("本轮不记为已推，已进 pending 待下轮重推。", file=sys.stderr)
        st.add_pending(message, "本轮推送失败")
        return False
    for decision in selected:
        st.mark_event(decision)
    return True


def _print_candidates(stats: Dict[str, int], decisions: List[Decision],
                      fresh: List[Decision], selected: List[Decision]) -> None:
    print("\n=== 四类新闻候选（已跨源合并）===")
    if not decisions:
        print("（无）")
    for decision in decisions:
        mark = "★推送" if decision in selected else ("·见过" if decision not in fresh else "·未入选")
        print(f"[{decision.category}/{decision.rule}] {mark} {decision.title}")
        print(f"    {'/'.join(decision.sources)}｜{decision.url}")
    print("\n=== 挡掉统计 ===")
    print("  " + "｜".join(f"{key}={value}" for key, value in sorted(stats.items()) if value))


def run(*, fixtures: str = "", state_path: str = "state/state.json", report_path: str = "",
        send: bool = False, now_ms: Optional[int] = None,
        print_candidates: bool = False) -> int:
    started_dt = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    stamp = now_ms if now_ms is not None else _now_ms()
    st = state_mod.State(state_path).load()
    # 先记下上一轮时间再写本轮，时间窗只认上一轮之后的新内容
    prior = st.last_run_ms()
    gap = st.record_run(started_dt)

    since_ms = int(prior - rules.OVERLAP_MINUTES * 60_000) if prior else int(
        stamp - rules.NEWS_WINDOW_HOURS * 3_600_000)

    if fixtures:
        reports, board_quotes = _load_fixtures(fixtures)
        print(f"使用本地样例：{len(reports)} 个源、{len(board_quotes)} 个行情标的")
    else:
        reports = fetch_all(since_ms, stamp)
        board_quotes, _ = quotes.fetch_board(timeout=rules.FETCH_TIMEOUT,
                                             retries=rules.FETCH_RETRIES)
    board_failed = [q for q in board_quotes if not q.ok]

    for report in reports:
        st.add_source_status(report.name, report.ok, report.status, report.error)
    failed = [r for r in reports if not r.ok]
    empty = [r for r in reports if r.ok and not r.items]
    # 源状态：ok=有数据、empty=成功但 0 条（正常，不算故障）、
    # partial_failure=部分挂、failure=全挂（要告警）
    if reports and len(failed) == len(reports):
        source_state = "failure"
    elif failed:
        source_state = "partial_failure"
    elif reports and len(empty) == len(reports):
        source_state = "empty"
    else:
        source_state = "ok"
    st.set_source_states({r.name: rsshub.route_state(r) for r in reports})
    if failed:
        print(f"路由失效 {len(failed)} 个（静默隔离/换实例后仍失败）："
              + "、".join(f"{r.name}:{r.status}" for r in failed))
    if empty:
        print(f"路由返回 0 条 {len(empty)} 个（源正常无内容，不算故障）："
              + "、".join(r.name for r in empty))
    print(f"源状态：{source_state}")
    all_sources_failed = source_state == "failure" and not fixtures
    if all_sources_failed:
        # 全源失败：不推 Telegram 刷屏，但单独告警（退出码 2 让 Actions 失败），
        # 不能被 continue-on-error 吞掉。
        print("【告警】全部 RSSHub 路由失败，本轮不出内容。", file=sys.stderr)

    stats: Dict[str, int] = {
        "raw_items": 0, "stale": 0, "future": 0, "no_timestamp": 0,
        "too_short": 0, "not_chinese": 0, "crypto": 0, "market_quote": 0,
        "non_economic": 0, "unclassified": 0, "duplicated": 0, "duplicated_url": 0,
        "earnings_detail": 0, "survey": 0,
    }
    decisions = _collect(reports, since_ms, stamp, stats)

    fresh: List[Decision] = []
    skipped_seen = 0
    for decision in decisions:
        if st.known_event(decision):
            skipped_seen += 1
            continue
        fresh.append(decision)
    # 按类别轮流取，避免一个热门话题把 10 条全占满；每类内部按时间从新到旧
    fresh.sort(key=lambda d: d.item.published_ms, reverse=True)
    buckets = {cat: [d for d in fresh if d.category == cat] for cat in rules.CATEGORIES}
    selected: List[Decision] = []
    while len(selected) < rules.MAX_ITEMS and any(buckets.values()):
        for cat in rules.CATEGORIES:
            if buckets[cat] and len(selected) < rules.MAX_ITEMS:
                selected.append(buckets[cat].pop(0))

    board_lines = quotes.format_board(board_quotes)
    message = rules.build_message(selected, board_lines) if selected else ""

    pushed = False
    if send:
        if not selected and not st.pending:
            print("本轮无新增，静默不推送。")
        else:
            token, chat_id = telegram.credentials()
            if not token or not chat_id:
                print("缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，拒绝推送。", file=sys.stderr)
                st.save()
                return EXIT_CONFIG
            pushed = _push(token, chat_id, message, selected, st)
            if not pushed:
                st.save()
                return EXIT_CONFIG
    elif selected:
        print("\n--- 干跑：不会发送 Telegram ---\n" + message + "\n")
    else:
        print("本轮无新增，静默（干跑也不输出消息）。")
        print("\n--- 行情看板（仅本轮展示，不推送）---\n" + "\n".join(board_lines) + "\n")

    if print_candidates:
        _print_candidates(stats, decisions, fresh, selected)

    st.prune()
    st.save()

    report = {
        "started_at": started_dt, "mode": "send" if send else "dry-run",
        "gap_minutes": gap, "since_ms": since_ms, "now_ms": stamp,
        "overlap_minutes": rules.OVERLAP_MINUTES, "max_items": rules.MAX_ITEMS,
        "sources": [r.to_dict() for r in reports],
        "source_state": source_state,
        "source_states": {r.name: rsshub.route_state(r) for r in reports},
        "source_failures": st.source_failures,
        "all_sources_failed": all_sources_failed,
        "stats": stats, "skipped_seen": skipped_seen,
        "selected": [d.to_dict() for d in selected],
        "message": message, "telegram_sent": pushed,
        "pending_count": len(st.pending),
        "board": {
            "quotes": [q.to_dict() for q in board_quotes],
            "failed": [q.to_dict() for q in board_failed],
            "lines": board_lines,
        },
    }
    if report_path:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if all_sources_failed:
        return EXIT_ALL_FAILED
    if source_state == "partial_failure":
        return EXIT_PARTIAL
    # 行情看板全挂才当异常，单个标的不影响本轮
    if board_quotes and len(board_failed) == len(board_quotes):
        return EXIT_PARTIAL
    return EXIT_OK
