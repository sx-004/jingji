"""跨轮状态：已推条目去重 + 源健康 + 上次运行时间 + 推送失败待重推队列。

状态写在 state 分支（不走 Actions cache，缓存逐出会丢历史导致重复推送）。
干跑不写「已推」，用户还没确认推送前不能把内容标记成推过。
推送失败的条目进 pending，下轮先重推，成功才清除——不能只靠「不写 seen」，
否则它下一轮会落在「只搜上次之后」的窗口外，等于静默丢失。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import lang, rules

UTC = timezone.utc
MAX_SEEN = 300
MAX_PENDING = 20


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class State:
    def __init__(self, path: str = "state/state.json"):
        self.path = Path(path)
        self.seen: List[dict] = []
        self.source_failures: Dict[str, dict] = {}
        self.source_states: Dict[str, str] = {}
        self.runs: List[dict] = []
        self.pending: List[dict] = []
        self.last_run: str = ""
        self.updated_at: str = ""

    def load(self) -> "State":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件损坏，拒绝静默跳过（会造成重复推送）: {exc}") from exc
        self.seen = list(raw.get("seen") or [])
        self.source_failures = dict(raw.get("source_failures") or {})
        self.source_states = dict(raw.get("source_states") or {})
        self.runs = list(raw.get("runs") or [])[-48:]
        self.pending = list(raw.get("pending") or [])[-MAX_PENDING:]
        self.last_run = str(raw.get("last_run") or "")
        return self

    def save(self) -> None:
        self.updated_at = _now()
        payload = {
            "schema_version": "1.0.0",
            "updated_at": self.updated_at,
            "last_run": self.last_run,
            "seen": self.seen[-MAX_SEEN:],
            "source_failures": self.source_failures,
            "source_states": self.source_states,
            "runs": self.runs[-48:],
            "pending": self.pending[-MAX_PENDING:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_run(self, started_at: str) -> float:
        prior = _parse(self.last_run)
        current = _parse(started_at)
        gap = round((current - prior).total_seconds() / 60, 1) if prior and current else -1
        self.runs.append({"at": started_at, "gap_minutes": gap})
        self.last_run = started_at
        return gap

    def last_run_ms(self) -> int:
        stamp = _parse(self.last_run)
        return int(stamp.timestamp() * 1000) if stamp else 0

    def add_source_status(self, name: str, ok: bool, error_type: str = "",
                          error: str = "") -> None:
        """单源失败只记状态，静默隔离；全源失败由编排层单独告警。"""
        if ok:
            self.source_failures.pop(name, None)
            return
        entry = dict(self.source_failures.get(name) or {})
        self.source_failures[name] = {
            "count": int(entry.get("count") or 0) + 1,
            "error_type": error_type, "last_error": error[:200], "at": _now(),
        }

    def set_source_states(self, states: Dict[str, str]) -> None:
        """记下每个源最近一轮的状态：ok / empty / failure（用来区分「没内容」和「源哑了」）。"""
        self.source_states = dict(states or {})

    def known_event(self, item) -> bool:
        """链接归一 + 标题归一/相似 + 时间桶：推过的同一条不再推。"""
        cutoff = datetime.now(UTC) - timedelta(days=rules.DEDUP_RETENTION_DAYS)
        url_key = lang.normalize_url(getattr(item, "url", ""))
        for record in self.seen:
            stamp = _parse(str(record.get("at") or ""))
            if stamp is not None and stamp < cutoff:
                continue
            if url_key and lang.normalize_url(str(record.get("url") or "")) == url_key:
                return True
            if lang.same_title(str(record.get("title") or ""), str(getattr(item, "title", ""))):
                return True
        return False

    def mark_event(self, item, status: str = "已推送") -> None:
        self.seen.append({
            "title": str(getattr(item, "title", ""))[:300],
            "url": str(getattr(item, "url", ""))[:500],
            "category": str(getattr(item, "category", "")),
            "rule": str(getattr(item, "rule", "")),
            "status": status, "at": _now(),
        })
        if len(self.seen) > MAX_SEEN:
            self.seen = self.seen[-MAX_SEEN:]

    def add_pending(self, message: str, note: str = "") -> None:
        self.pending.append({"message": message, "note": note[:200], "at": _now()})
        if len(self.pending) > MAX_PENDING:
            self.pending = self.pending[-MAX_PENDING:]

    def prune(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=rules.DEDUP_RETENTION_DAYS)
        kept, removed = [], 0
        for record in self.seen:
            stamp = _parse(str(record.get("at") or ""))
            if stamp is not None and stamp < cutoff:
                removed += 1
                continue
            kept.append(record)
        self.seen = kept
        return removed
