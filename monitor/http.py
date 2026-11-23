"""全球经济新闻监控公共底座：零第三方依赖的公网 HTTP 抓取。

只允许公网 HTTP(S)，禁止私网/回环/元数据地址；跟随跳转时同样校验。
响应体硬上限，避免异常响应撑爆 Actions 内存。
失败分类型记下，交给编排层决定是隔离、换实例还是告警。
"""
from __future__ import annotations

import ipaddress
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS: Dict[str, str] = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/rss+xml, application/xml, text/xml, application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class FetchFailure(Exception):
    def __init__(self, message: str, error_type: str = "provider_error", status: int = 0):
        super().__init__(message)
        self.error_type = error_type
        self.status = status


@dataclass
class FetchResult:
    url: str
    data: bytes = b""
    status: int = 0
    error_type: str = ""
    error: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error_type

    def text(self, encoding: str = "utf-8") -> str:
        return self.data.decode(encoding, "replace")


def _proxy_configured() -> bool:
    """是否配置了代理。走代理时域名由代理解析，本地 DNS 结果不作安全依据。"""
    return bool(_proxy_handler().proxies)


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise FetchFailure(f"协议不允许: {parsed.scheme}", "config_error")
    if parsed.username or parsed.password:
        raise FetchFailure("URL 不允许带用户名或密码", "config_error")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or host in ("localhost", "metadata.google.internal"):
        raise FetchFailure(f"主机名不允许: {host}", "config_error")
    if host.endswith((".local", ".localhost", ".internal")):
        raise FetchFailure(f"主机名不允许: {host}", "config_error")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise FetchFailure(f"非公网 IP 被拒绝: {literal}", "config_error")
    if _proxy_configured():
        # 走代理时由代理解析域名：本地 DNS 可能是 Fake-IP 或被污染的结果，不能据此判断目标安全。
        # 字面 IP、私网主机名等检查在上面已经做过，仍然生效。
        return
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchFailure(f"DNS 解析失败: {host}: {exc}", "dns_error") from exc
    ips = {str(a[4][0]).split("%", 1)[0] for a in answers if a[4]}
    if not ips:
        raise FetchFailure(f"DNS 无可用地址: {host}", "dns_error")
    parsed_ips = []
    for raw in ips:
        try:
            parsed_ips.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise FetchFailure(f"DNS 返回非法地址: {raw}", "dns_error") from exc
    # 198.18.0.0/15 是本机 VPN 的 Fake-IP 段，不是真正的内网服务，放行
    fake_ip = ipaddress.ip_network("198.18.0.0/15")
    if not all(addr.is_global or addr in fake_ip for addr in parsed_ips):
        raise FetchFailure(f"DNS 含非公网地址，已拒绝: {host}", "dns_error")


def _proxy_handler() -> urllib.request.ProxyHandler:
    """代理开关：只读显式设置的环境变量，未设置时不走代理。

    NAS 上靠 HTTPS_PROXY/HTTP_PROXY 走本机代理才能访问 Telegram 和部分交易所；
    GitHub Actions 上这些变量不存在，行为与此前「不走代理」完全一致。
    NO_PROXY 由 urllib 的 proxy_bypass 自动处理，不需要在这里判断。
    """
    proxies: Dict[str, str] = {}
    for scheme in ("https", "http"):
        for key in (scheme + "_proxy", scheme.upper() + "_PROXY"):
            value = os.environ.get(key)
            if value:
                proxies[scheme] = value
                break
    if not proxies:
        for key in ("all_proxy", "ALL_PROXY"):
            value = os.environ.get(key)
            if value:
                return urllib.request.ProxyHandler({"https": value, "http": value})
    return urllib.request.ProxyHandler(proxies)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _error_type(exc: Exception, status: int = 0) -> str:
    if status in (401, 403, 407):
        return "blocked"
    if status in (404, 410):
        return "missing"
    if isinstance(exc, urllib.error.HTTPError):
        if status == 429 or 500 <= status <= 599:
            return "provider_error"
        return "blocked" if status in (400, 401, 403) else "provider_error"
    return "provider_error"


def fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT, retries: int = 1,
          headers: Optional[Dict[str, str]] = None, max_bytes: int = MAX_BYTES) -> FetchResult:
    started = time.time()
    try:
        validate_url(url)
    except FetchFailure as exc:
        return FetchResult(url, error_type=exc.error_type, error=str(exc),
                           duration_ms=int((time.time() - started) * 1000))
    request_headers = dict(HEADERS)
    request_headers.update(headers or {})
    opener = urllib.request.build_opener(_SafeRedirect(), _proxy_handler())
    last: Optional[Exception] = None
    last_status = 0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=request_headers, method="GET")
            with opener.open(req, timeout=timeout) as resp:
                last_status = int(getattr(resp, "status", 0))
                declared = resp.headers.get("Content-Length", "")
                try:
                    if declared and int(declared) > max_bytes:
                        raise FetchFailure(f"响应超过上限 {max_bytes} 字节", "too_large", last_status)
                except ValueError:
                    pass
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise FetchFailure(f"响应超过上限 {max_bytes} 字节", "too_large", last_status)
                if not data:
                    raise FetchFailure("响应体为空", "missing", last_status)
                return FetchResult(url, data, last_status,
                                   duration_ms=int((time.time() - started) * 1000))
        except FetchFailure as exc:
            last = exc
            if exc.error_type in ("blocked", "missing", "too_large", "config_error", "dns_error"):
                break
        except urllib.error.HTTPError as exc:
            last = exc
            last_status = exc.code
            if last_status in (400, 401, 403, 404, 405, 407, 410, 429):
                break
        except Exception as exc:  # noqa: BLE001 网络异常种类多，统一按可重试处理
            last = exc
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    error = str(last) if last else "未知抓取失败"
    kind = last.error_type if isinstance(last, FetchFailure) else _error_type(last, last_status)  # type: ignore[union-attr]
    return FetchResult(url, status=last_status, error_type=kind or "provider_error",
                       error=error[:500], duration_ms=int((time.time() - started) * 1000))
