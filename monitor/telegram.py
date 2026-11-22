"""Telegram 推送：一轮合成一条消息，关掉链接预览；只有 Bot API 返回 ok 才算成功。

token 与 chat id 只读环境变量（GitHub Secrets），不写文件、不进日志。
用户尚未确认接推送前，本仓工作流只跑 --dry-run，不会调用这个模块。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

API = "https://api.telegram.org/bot{token}/sendMessage"
API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_CHARS = 4000


class PushError(Exception):
    pass


def credentials() -> tuple:
    return (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send_message(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> bool:
    if not token or not chat_id:
        raise PushError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    body_text = (text or "").strip()
    if not body_text:
        raise PushError("消息为空")
    if len(body_text) > MAX_CHARS:
        raise PushError(f"消息超过 {MAX_CHARS} 字上限，暂停推送，不截断内容")
    payload = json.dumps({
        "chat_id": chat_id,
        "text": body_text,
        "disable_web_page_preview": True,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API.format(token=token), data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        if exc.code == 401:
            raise PushError(f"Telegram 401：token 无效或已被吊销（{detail}）") from exc
        if exc.code == 403:
            raise PushError(f"Telegram 403：该聊天还没启动过这个 bot（{detail}）") from exc
        raise PushError(f"Telegram HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PushError(f"Telegram 请求失败: {type(exc).__name__}") from exc
    if not body.get("ok"):
        raise PushError(f"Telegram 返回失败: {str(body.get('description') or '')[:160]}")
    return True


def _api_call(token: str, method: str, params: Optional[dict] = None) -> tuple:
    """调一个 Bot API 方法，返回 (是否成功, 数据)。不打印 token。"""
    url = API_BASE.format(token=token, method=method)
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, {"error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # noqa: BLE001
        return False, {"error": f"{type(exc).__name__}"}
    if not body.get("ok"):
        return False, {"error": str(body.get("description") or "未知错误")[:200]}
    return True, body.get("result") or {}


def check_credentials(token: str, chat_id: str) -> dict:
    """只读自查：token 属于哪个 bot、chat 能不能找到。不回显 token 与 chat id。"""
    report = {"token_ok": False, "bot_username": "", "bot_id": 0,
              "chat_ok": False, "chat_type": "", "chat_title": "", "error": ""}
    if not token:
        report["error"] = "缺少 TELEGRAM_BOT_TOKEN"
        return report
    if not chat_id:
        report["error"] = "缺少 TELEGRAM_CHAT_ID"
        return report
    ok, data = _api_call(token, "getMe")
    if not ok:
        report["error"] = f"getMe 失败：{data.get('error')}"
        return report
    report["token_ok"] = True
    report["bot_username"] = str(data.get("username") or "")
    report["bot_id"] = data.get("id") or 0
    ok, data = _api_call(token, "getChat", {"chat_id": chat_id})
    if ok:
        report["chat_ok"] = True
        report["chat_type"] = str(data.get("type") or "")
        report["chat_title"] = str(data.get("title") or data.get("first_name") or "")
    else:
        report["error"] = f"getChat 失败：{data.get('error')}"
    return report
